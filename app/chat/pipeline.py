"""The chat pipeline for one user turn.

message -> sensitive-input redaction -> local intents -> input guardrails
-> Gemini selects MCP tools -> schema + grounding validation -> live MCP call
-> Gemini answers from tool results -> output guardrails (figures, approval, tax).
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from google.genai import types

from app.chat.intents import match_small_talk
from app.chat.session import Conversation, SessionStore
from app.chat.transcript import TranscriptLogger, TurnTrace
from app.config import MAX_TOOL_STEPS
from app.llm import gemini
from app.llm.gemini import GeminiClient, LLMError
from app.llm.prompts import (CLARIFY_DECLARATION, CLARIFY_TOOL, REPAIR_PROMPT, TOOLS_REQUIRED_PROMPT,
                             build_system_prompt)
from app.safety.grounding import expand_evidence, numbers_in, strip_unsupported_amounts, unsupported_amounts
from app.safety.guardrails import InputCheck, check_input, enforce_output
from app.safety.redaction import has_meaningful_content, redact
from app.tools.client import McpToolGateway, ToolConnection, ToolServiceError
from app.tools.registry import ToolCatalog, grounding_problem, prepare_arguments

logger = logging.getLogger(__name__)

SENSITIVE_ONLY_REPLY = (
    "For your safety I've removed the sensitive details you shared and haven't stored or forwarded them. "
    "Please never share card numbers, CVV, OTP, PINs or passwords with anyone, including me. "
    "I don't need them to recommend or compare cards. What would you like help with?"
)
PRIVACY_NOTICE = "Removed sensitive details before processing: {}."
NOT_CONFIGURED_REPLY = "The advisor isn't configured yet. Please set the required values in the .env file and restart."
TOOLS_UNAVAILABLE_REPLY = (
    "I couldn't reach the Great.cards card database just now, so I can't give reliable card information. "
    "Please try again in a moment."
)
NO_ANSWER_REPLY = "Sorry, I couldn't put together a reliable answer for that. Could you rephrase or add a bit more detail?"
UNVERIFIED_FACTS_REPLY = (
    "I couldn't confirm those card details from the Great.cards database for this question, so I won't guess. "
    "Could you tell me which card(s) you'd like me to look up, or share your main monthly spends?"
)
WITHHELD_HISTORY_TEXT = "[message withheld by safety filter]"

# Card facts in a draft: rupee amounts, percentages (rates, markups) or a named bank/co-brand card.
_CARD_FACT_RE = re.compile(
    r"₹\s*\d|\bRs\.?\s*\d|\bINR\s*\d|\d\s*%"
    r"|\b(?:HDFC|SBI|ICICI|Axis|Kotak|AU|IDFC|IndusInd|Yes Bank|RBL|HSBC|Amex|American Express|"
    r"Standard Chartered|Federal|IDBI|Bank of Baroda|BOB|OneCard|Scapia|Flipkart|Amazon|Swiggy|Airtel|"
    r"Tata|Diners|Magnus|Millennia|Regalia|Infinia)\b[\w\s&+'.-]{0,40}?\bCard\b"
)


def _states_card_facts(text: str) -> bool:
    return bool(_CARD_FACT_RE.search(text))


@dataclass
class ChatReply:
    reply: str
    kind: str  # local | clarification | answer | blocked | error
    tools_used: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


@dataclass
class _ToolOutcome:
    payload: dict[str, Any]
    called: bool = False
    evidence_text: str = ""


def _clean_input(message: str) -> str:
    text = unicodedata.normalize("NFKC", message)
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    return re.sub(r"[ \t]+", " ", text).strip()


class ChatPipeline:
    def __init__(self, sessions: SessionStore, llm: GeminiClient | None, tools: McpToolGateway | None,
                 transcripts: TranscriptLogger | None = None):
        self._sessions = sessions
        self._llm = llm
        self._tools = tools
        self._transcripts = transcripts

    async def handle(self, session_id: str, message: str, endpoint: str = "POST /api/chat") -> ChatReply:
        started = time.perf_counter()
        redaction = redact(_clean_input(message))
        logger.info("Turn received: %d chars, redacted=%s", len(redaction.text), list(redaction.findings) or "none")
        trace = TurnTrace(endpoint=endpoint, redacted=redaction.findings)

        conversation = self._sessions.get(session_id)
        async with conversation.lock:
            reply, history_text = await self._respond(conversation, redaction.text, redaction.critical, trace)
            if redaction.findings:
                reply.notices.insert(0, PRIVACY_NOTICE.format(", ".join(redaction.findings)))
            conversation.track_request(history_text, reply.kind)
            conversation.add_exchange(history_text, reply.reply)
            if self._transcripts:
                elapsed_ms = (time.perf_counter() - started) * 1000
                await self._transcripts.log_turn(conversation, session_id, trace, redaction.text, reply, elapsed_ms)
        logger.info("Turn answered: kind=%s tools=%s", reply.kind, reply.tools_used or "none")
        return reply

    async def reset(self, session_id: str, endpoint: str = "POST /api/reset") -> None:
        """Start a new conversation; the old transcript is closed and the next turn opens a new file."""
        conversation = self._sessions.pop(session_id)
        if conversation and self._transcripts:
            async with conversation.lock:
                await self._transcripts.log_event(
                    conversation, endpoint, "User started a new chat. This session log is closed.")

    async def _respond(self, conversation: Conversation, text: str, critical: bool,
                       trace: TurnTrace) -> tuple[ChatReply, str]:
        """Return the reply and the (redacted) user text to keep in history."""
        if critical and not has_meaningful_content(text):
            return ChatReply(SENSITIVE_ONLY_REPLY, "local"), text

        local_reply = match_small_talk(text)
        if local_reply:
            return ChatReply(local_reply, "local"), text

        check = check_input(text)
        if check.blocked:
            logger.info("Input blocked locally: %s", check.reason)
            trace.blocked_reason = check.reason
            history_text = text if check.reason in ("off_topic", "tax_advice") else WITHHELD_HISTORY_TEXT
            return ChatReply(check.reply, "blocked"), history_text

        if self._llm is None or self._tools is None:
            return ChatReply(NOT_CONFIGURED_REPLY, "error"), text

        try:
            return await self._run_agent(conversation, text, check, trace), text
        except LLMError as exc:
            return ChatReply(exc.user_message, "error"), text
        except ToolServiceError:
            logger.warning("MCP server unavailable for this turn")
            return ChatReply(TOOLS_UNAVAILABLE_REPLY, "error"), text

    async def _run_agent(self, conversation: Conversation, text: str, check: InputCheck,
                         trace: TurnTrace) -> ChatReply:
        assert self._llm is not None and self._tools is not None
        catalog = await self._tools.catalog()
        flags = [name for name in ("approval_question", "mentions_tax") if getattr(check, name)]
        system = build_system_prompt(self._tools.advisor_context, flags)
        tool = gemini.build_tool(
            [{"name": s.name, "description": s.description, "parameters": s.declared_schema()}
             for s in catalog.exposed()] + [CLARIFY_DECLARATION]
        )

        contents: list[types.Content] = [
            gemini.user_content(t) if role == "user" else gemini.model_content(t)
            for role, t in conversation.history
        ]
        contents.append(gemini.user_content(text))
        user_text = conversation.user_text() + "\n" + text
        request_text = conversation.request_text(text)
        evidence: set[float] = set()   # numbers from this turn's tool results only

        tools_used: list[str] = []
        draft = ""
        nudged = False       # a draft stated card facts without any tool result; re-prompted once
        force_tool = False   # the next Gemini call must call a tool (or ask_clarification)
        async with self._tools.connection() as conn:
            for step in range(MAX_TOOL_STEPS + 1):
                trace.llm_calls += 1
                response = await self._llm.generate(
                    contents, system_instruction=system, tool=tool,
                    allow_tool_calls=step < MAX_TOOL_STEPS, require_tool_call=force_tool,
                )
                force_tool = False
                calls = gemini.function_calls(response)
                if not calls:
                    draft = gemini.response_text(response)
                    if not tools_used and not nudged and step + 1 < MAX_TOOL_STEPS and _states_card_facts(draft):
                        logger.warning("Draft stated card facts without a tool result; requiring a tool call")
                        nudged = force_tool = True
                        contents += [gemini.model_content(draft), gemini.user_content(TOOLS_REQUIRED_PROMPT)]
                        draft = ""
                        continue
                    break
                contents.append(gemini.response_content(response))
                parts = []
                for call in calls:
                    if call.name == CLARIFY_TOOL:
                        question = str((call.args or {}).get("question", "")).strip()
                        if question:
                            return self._finish(question, "clarification", tools_used, check,
                                                self._allowed_figures(evidence, request_text))
                    outcome = await self._execute(conn, catalog, call, user_text, request_text, trace)
                    if outcome.called:
                        tools_used.append(call.name)
                        evidence |= numbers_in(outcome.evidence_text)
                    parts.append(gemini.function_response_part(call, outcome.payload))
                contents.append(gemini.function_responses_content(parts))

        if not draft:
            return ChatReply(NO_ANSWER_REPLY, "error", tools_used)
        if not tools_used and _states_card_facts(draft):
            logger.warning("Answer still stated card facts without a tool result; withholding it")
            return ChatReply(UNVERIFIED_FACTS_REPLY, "answer", tools_used)

        allowed = self._allowed_figures(evidence, request_text)
        unsupported = unsupported_amounts(draft, allowed)
        if unsupported:
            logger.warning("Draft had %d unsupported rupee figure(s); requesting a grounded rewrite", len(unsupported))
            contents += [gemini.model_content(draft),
                         gemini.user_content(REPAIR_PROMPT.format(figures=", ".join(unsupported)))]
            try:
                trace.llm_calls += 1
                revised = gemini.response_text(await self._llm.generate(
                    contents, system_instruction=system, tool=tool, allow_tool_calls=False))
                draft = revised or draft
            except LLMError:
                pass
        return self._finish(draft, "answer", tools_used, check, allowed)

    @staticmethod
    def _allowed_figures(evidence: set[float], request_text: str) -> set[float]:
        """Numbers an answer may quote: this turn's MCP results as returned, plus the user's figures
        in the current request (and their monthly/annual conversions)."""
        return evidence | expand_evidence(numbers_in(request_text))

    @staticmethod
    def _finish(text: str, kind: str, tools_used: list[str], check: InputCheck,
                allowed: set[float]) -> ChatReply:
        text = strip_unsupported_amounts(text, allowed)
        text = enforce_output(
            text,
            approval_context=check.approval_question or "check_eligibility" in tools_used,
            tax_context=check.mentions_tax,
        )
        return ChatReply(text or NO_ANSWER_REPLY, kind, tools_used)

    async def _execute(self, conn: ToolConnection, catalog: ToolCatalog, call: types.FunctionCall,
                       user_text: str, request_text: str, trace: TurnTrace) -> _ToolOutcome:
        name = call.name or ""
        spec = catalog.get(name)
        if spec is None:
            logger.info("Rejected call to unavailable tool %r", name)
            trace.record_mcp(name, {}, "rejected: tool not exposed")
            return _ToolOutcome({"error": f"Tool '{name}' is not available. Use only the declared tools."})

        args, errors = prepare_arguments(spec, dict(call.args or {}))
        if errors:
            logger.info("Rejected %s: %d schema error(s)", name, len(errors))
            trace.record_mcp(name, args, "rejected: schema validation failed")
            return _ToolOutcome({"error": "Invalid arguments: " + " | ".join(errors)
                                 + " Fix them, or ask the user for the missing information."})

        problem = grounding_problem(name, args, user_text, request_text)
        if problem:
            logger.info("Rejected %s: arguments not grounded in user input", name)
            trace.record_mcp(name, args, "rejected: values not provided by the user")
            return _ToolOutcome({"error": problem})

        logger.info("Calling MCP tool %s with fields %s", name, sorted(args))
        try:
            result = await conn.call(name, args)
        except ToolServiceError as exc:
            logger.warning("%s", exc)
            trace.record_mcp(name, args, "failed: MCP server unavailable")
            return _ToolOutcome({"error": "The Great.cards service failed for this request. Tell the user the "
                                          "data is temporarily unavailable; do not invent any card data."})
        if not result.ok:
            trace.record_mcp(name, args, "error returned by tool")
            return _ToolOutcome({"error": result.data}, called=True)
        trace.record_mcp(name, args, "ok")
        return _ToolOutcome({"result": result.data}, called=True, evidence_text=result.text)
