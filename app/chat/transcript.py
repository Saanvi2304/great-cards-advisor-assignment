"""Per-session chat transcripts written to logs/sessions/.

Each conversation gets its own log file containing every turn: the user's message
(after redaction), the chatbot's reply, the HTTP endpoint used, Gemini calls and every
MCP tool call with its arguments, status, raw (pre-cleaning) arguments
and error text or a truncated result. Only redacted text is ever written.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.chat.session import Conversation

logger = logging.getLogger(__name__)

MAX_DETAIL_CHARS = 2_000   # per MCP call: tool result or error text kept in the transcript


@dataclass
class TurnTrace:
    """What happened during one turn, collected by the pipeline."""

    endpoint: str
    redacted: tuple[str, ...] = ()
    llm_calls: int = 0
    mcp_calls: list[dict[str, Any]] = field(default_factory=list)
    blocked_reason: str = ""

    def record_mcp(self, tool: str, arguments: dict[str, Any], status: str,
                   raw_arguments: dict[str, Any] | None = None, detail: str = "") -> None:
        """`raw_arguments` is what Gemini sent before cleaning; `detail` is the error text or tool result."""
        self.mcp_calls.append({"tool": tool, "arguments": arguments, "status": status,
                               "raw_arguments": raw_arguments, "detail": detail})


def _now() -> datetime:
    return datetime.now().astimezone()


def _indent(text: str) -> str:
    return "\n".join("    " + line for line in text.splitlines()) or "    (empty)"


class TranscriptLogger:
    def __init__(self, directory: Path, llm_model: str = ""):
        self._dir = directory
        self._llm_model = llm_model

    def ensure_directory(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    def _open_file(self, conversation: Conversation, session_id: str) -> Path:
        """Create the session's log file on its first turn."""
        if conversation.log_path is None:
            started = _now()
            # The cookie value is a credential, so only a short one-way hash appears on disk.
            tag = hashlib.sha256(session_id.encode()).hexdigest()[:12]
            path = self._dir / f"session_{started:%Y%m%d-%H%M%S}_{tag}_{id(conversation) % 10_000:04d}.log"
            header = (
                "==== Great.cards Credit Card Advisor - chat session ====\n"
                f"Session : {tag}\n"
                f"Started : {started:%Y-%m-%d %H:%M:%S %z}\n"
                f"Model   : {self._llm_model or 'not configured'}\n"
                "Note    : user messages are stored after sensitive-data redaction.\n"
            )
            self.ensure_directory()
            path.write_text(header, encoding="utf-8")
            conversation.log_path = path
        return conversation.log_path

    async def _append(self, path: Path, text: str) -> None:
        def write() -> None:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(text)

        try:
            await asyncio.to_thread(write)
        except OSError:
            logger.warning("Could not write session transcript %s", path.name)

    async def log_turn(self, conversation: Conversation, session_id: str, trace: TurnTrace, user_text: str,
                       reply: Any, duration_ms: float) -> None:
        try:
            path = await asyncio.to_thread(self._open_file, conversation, session_id)
        except OSError:
            logger.warning("Could not create a session transcript file")
            return
        lines = [
            "",
            f"---- Turn {conversation.turn_count} | {_now():%Y-%m-%d %H:%M:%S} | {trace.endpoint} "
            f"| {duration_ms:.0f} ms ----",
            "USER:",
            _indent(user_text),
        ]
        if trace.redacted:
            lines.append(f"Redacted before processing: {', '.join(trace.redacted)}")
        if trace.blocked_reason:
            lines.append(f"Guardrail: blocked locally ({trace.blocked_reason})")
        if trace.llm_calls:
            lines.append(f"Gemini API: {trace.llm_calls} generateContent call(s) [{self._llm_model}]")
        else:
            lines.append("Gemini API: not called")
        if trace.mcp_calls:
            lines.append("MCP tools:")
            for call in trace.mcp_calls:
                arguments = json.dumps(call["arguments"], ensure_ascii=False)
                lines.append(f"    - {call['tool']} {arguments} -> {call['status']}")
                if call["raw_arguments"] is not None and call["raw_arguments"] != call["arguments"]:
                    raw = json.dumps(call["raw_arguments"], ensure_ascii=False, default=str)
                    lines.append(f"        raw arguments (before cleaning): {raw}")
                if call["detail"]:
                    detail = call["detail"]
                    if len(detail) > MAX_DETAIL_CHARS:
                        detail = detail[:MAX_DETAIL_CHARS] + f" ...[{len(call['detail']) - MAX_DETAIL_CHARS} more chars]"
                    lines.append(f"        detail: {detail}")
        else:
            lines.append("MCP tools: none")
        lines.append(f"ASSISTANT [{reply.kind}]:")
        lines.append(_indent(reply.reply))
        for notice in reply.notices:
            lines.append(f"Notice: {notice}")
        await self._append(path, "\n".join(lines) + "\n")

    async def log_event(self, conversation: Conversation, endpoint: str, message: str) -> None:
        if conversation.log_path is None:
            return
        await self._append(conversation.log_path,
                           f"\n---- {_now():%Y-%m-%d %H:%M:%S} | {endpoint} ----\n{message}\n")
