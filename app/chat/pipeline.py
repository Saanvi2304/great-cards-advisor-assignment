"""The chat pipeline for one user turn.

message -> sensitive-input redaction -> local intents -> input guardrails
-> Gemini selects MCP tools -> schema + grounding validation -> live MCP call
-> Gemini answers from tool results -> output guardrails (figures, approval, tax).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from google.genai import types

from app.chat.intents import match_small_talk
from app.chat.session import Conversation, SessionStore
from app.config import MAX_TOOL_STEPS
from app.llm import gemini
from app.llm.gemini import GeminiClient, LLMError
from app.llm.prompts import CLARIFY_DECLARATION, CLARIFY_TOOL, REPAIR_PROMPT, build_system_prompt
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
WITHHELD_HISTORY_TEXT = "[message withheld by safety filter]"


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
    def __init__(self, sessions: SessionStore, llm: GeminiClient | None, tools: McpToolGateway | None):
        self._sessions = sessions
        self._llm = llm
        self._tools = tools

    async def handle(self, session_id: str, message: str) -> ChatReply:
        redaction = redact(_clean_input(message))
        logger.info("Turn received: %d chars, redacted=%s", len(redaction.text), list(redaction.findings) or "none")

        conversation = self._sessions.get(session_id)
        async with conversation.lock:
            reply, history_text = await self._respond(conversation, redaction.text, redaction.critical)
            if redaction.findings:
                reply.notices.insert(0, PRIVACY_NOTICE.format(", ".join(redaction.findings)))
            conversation.add_exchange(history_text, reply.reply)
        logger.info("Turn answered: kind=%s tools=%s", reply.kind, reply.tools_used or "none")
        return reply

    async def _respond(self, conversation: Conversation, text: str, critical: bool) -> tuple[ChatReply, str]:
        """Return the reply and the (redacted) user text to keep in history."""
        if critical and not has_meaningful_content(text):
            return ChatReply(SENSITIVE_ONLY_REPLY, "local"), text

        local_reply = match_small_talk(text)
        if local_reply:
            return ChatReply(local_reply, "local"), text

        check = check_input(text)
        if check.blocked:
            logger.info("Input blocked locally: %s", check.reason)
            history_text = text if check.reason in ("off_topic", "tax_advice") else WITHHELD_HISTORY_TEXT
            return ChatReply(check.reply, "blocked"), history_text

        if self._llm is None or self._tools is None:
            return ChatReply(NOT_CONFIGURED_REPLY, "error"), text

        try:
            return await self._run_agent(conversation, text, check), text
        except LLMError as exc:
            return ChatReply(exc.user_message, "error"), text
        except ToolServiceError:
            logger.warning("MCP server unavailable for this turn")
            return ChatReply(TOOLS_UNAVAILABLE_REPLY, "error"), text

    async def _run_agent(self, conversation: Conversation, text: str, check: InputCheck) -> ChatReply:
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

        tools_used: list[str] = []
        draft = ""
        async with self._tools.connection() as conn:
            for step in range(MAX_TOOL_STEPS + 1):
                response = await self._llm.generate(
                    contents, system_instruction=system, tool=tool,
                    allow_tool_calls=step < MAX_TOOL_STEPS,
                )
                calls = gemini.function_calls(response)
                if not calls:
                    draft = gemini.response_text(response)
                    break
                contents.append(gemini.response_content(response))
                parts = []
                for call in calls:
                    if call.name == CLARIFY_TOOL:
                        question = str((call.args or {}).get("question", "")).strip()
                        if question:
                            return self._finish(question, "clarification", tools_used, check,
                                                self._allowed_figures(conversation, user_text))
                    outcome = await self._execute(conn, catalog, call, user_text)
                    if outcome.called:
                        tools_used.append(call.name)
                        conversation.add_evidence(numbers_in(outcome.evidence_text))
                    parts.append(gemini.function_response_part(call, outcome.payload))
                contents.append(gemini.function_responses_content(parts))

        if not draft:
            return ChatReply(NO_ANSWER_REPLY, "error", tools_used)

        allowed = self._allowed_figures(conversation, user_text)
        unsupported = unsupported_amounts(draft, allowed)
        if unsupported:
            logger.warning("Draft had %d unsupported rupee figure(s); requesting a grounded rewrite", len(unsupported))
            contents += [gemini.model_content(draft),
                         gemini.user_content(REPAIR_PROMPT.format(figures=", ".join(unsupported)))]
            try:
                revised = gemini.response_text(await self._llm.generate(
                    contents, system_instruction=system, tool=tool, allow_tool_calls=False))
                draft = revised or draft
            except LLMError:
                pass
        return self._finish(draft, "answer", tools_used, check, allowed)

    @staticmethod
    def _allowed_figures(conversation: Conversation, user_text: str) -> set[float]:
        """Numbers an answer may quote: MCP results this session plus the user's own figures."""
        return expand_evidence(conversation.evidence | numbers_in(user_text))

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
                       user_text: str) -> _ToolOutcome:
        name = call.name or ""
        spec = catalog.get(name)
        if spec is None:
            logger.info("Rejected call to unavailable tool %r", name)
            return _ToolOutcome({"error": f"Tool '{name}' is not available. Use only the declared tools."})

        args, errors = prepare_arguments(spec, dict(call.args or {}))
        if errors:
            logger.info("Rejected %s: %d schema error(s)", name, len(errors))
            return _ToolOutcome({"error": "Invalid arguments: " + " | ".join(errors)
                                 + " Fix them, or ask the user for the missing information."})

        problem = grounding_problem(name, args, user_text)
        if problem:
            logger.info("Rejected %s: arguments not grounded in user input", name)
            return _ToolOutcome({"error": problem})

        logger.info("Calling MCP tool %s with fields %s", name, sorted(args))
        try:
            result = await conn.call(name, args)
        except ToolServiceError as exc:
            logger.warning("%s", exc)
            return _ToolOutcome({"error": "The Great.cards service failed for this request. Tell the user the "
                                          "data is temporarily unavailable; do not invent any card data."})
        if not result.ok:
            return _ToolOutcome({"error": result.data}, called=True)
        return _ToolOutcome({"result": result.data}, called=True, evidence_text=result.text)
