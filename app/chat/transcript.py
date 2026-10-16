"""Per-session chat transcripts written to logs/sessions/.

Each conversation gets its own log file containing every turn: the user's message
(after redaction), the chatbot's reply, the HTTP endpoint used, Gemini calls and every
MCP tool call with its arguments, status, raw (pre-cleaning) arguments
and error text or a truncated result. Only redacted text is ever written.

The same files are read back to list a browser's past chats in the sidebar and to show
or resume a chat's full history; only files tagged with the caller's session hash are read.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
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
    guardrails: bool = True

    def record_mcp(self, tool: str, arguments: dict[str, Any], status: str,
                   raw_arguments: dict[str, Any] | None = None, detail: str = "") -> None:
        """`raw_arguments` is what Gemini sent before cleaning; `detail` is the error text or tool result."""
        self.mcp_calls.append({"tool": tool, "arguments": arguments, "status": status,
                               "raw_arguments": raw_arguments, "detail": detail})


def _now() -> datetime:
    return datetime.now().astimezone()


def session_tag(session_id: str) -> str:
    """Short one-way hash of the session cookie: the cookie value itself is a credential."""
    return hashlib.sha256(session_id.encode()).hexdigest()[:12]


def _indent(text: str) -> str:
    return "\n".join("    " + line for line in text.splitlines()) or "    (empty)"


class TranscriptLogger:
    def __init__(self, directory: Path, llm_model: str = ""):
        self._dir = directory
        self._llm_model = llm_model

    def ensure_directory(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    def set_model(self, llm_model: str) -> None:
        """Model name written in the header of transcripts started from now on."""
        self._llm_model = llm_model

    def _open_file(self, conversation: Conversation, session_id: str) -> Path:
        """Create the session's log file on its first turn."""
        if conversation.log_path is None:
            started = _now()
            tag = session_tag(session_id)
            path = self._dir / f"session_{started:%Y%m%d-%H%M%S}_{tag}_{id(conversation) % 10_000:04d}.log"
            header = (
                "==== Great.cards Credit Card Advisor - chat session ====\n"
                f"Session : {tag}\n"
                + (f"Chat    : {conversation.chat_id}\n" if conversation.chat_id else "")
                + f"Started : {started:%Y-%m-%d %H:%M:%S %z}\n"
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
        if not trace.guardrails:
            lines.append("Guardrails: OFF (input and output guardrails skipped; redaction and grounding still on)")
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

    # ---- Reading transcripts back (sidebar history and resuming chats) ----

    def _files(self, session_id: str) -> list[Path]:
        """This browser's transcript files, newest first."""
        files = self._dir.glob(f"session_*_{session_tag(session_id)}_*.log")
        return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)

    async def list_chats(self, session_id: str, limit: int) -> list[LoggedChat]:
        def collect() -> list[LoggedChat]:
            chats = []
            for path in self._files(session_id)[:limit]:
                chat = parse_transcript(path)
                if chat and chat.turns:
                    chats.append(chat)
            return chats

        try:
            return await asyncio.to_thread(collect)
        except OSError:
            logger.warning("Could not list session transcripts")
            return []

    # ---- Admin console: every browser's transcripts ----

    def _all_files(self) -> list[Path]:
        if not self._dir.is_dir():
            return []
        return sorted(self._dir.glob("session_*.log"), key=lambda path: path.stat().st_mtime, reverse=True)

    async def list_all(self, limit: int) -> list[LoggedChat]:
        def collect() -> list[LoggedChat]:
            return [chat for path in self._all_files()[:limit] if (chat := parse_transcript(path))]

        try:
            return await asyncio.to_thread(collect)
        except OSError:
            logger.warning("Could not list session transcripts")
            return []

    async def load_file(self, name: str) -> tuple[LoggedChat, str] | None:
        """A transcript by file name, matched against the directory listing (never a path built from input)."""
        def find() -> tuple[LoggedChat, str] | None:
            for path in self._all_files():
                if path.stem == name:
                    chat = parse_transcript(path)
                    return (chat, path.read_text(encoding="utf-8", errors="replace")) if chat else None
            return None

        try:
            return await asyncio.to_thread(find)
        except OSError:
            logger.warning("Could not read a session transcript")
            return None

    async def load_chat(self, session_id: str, chat_id: str) -> LoggedChat | None:
        """The transcript of one of this browser's chats, matched by chat id (or file name for older logs)."""
        def find() -> LoggedChat | None:
            for path in self._files(session_id):
                if _chat_id(path, _read_header(path)) == chat_id:
                    return parse_transcript(path)
            return None

        try:
            return await asyncio.to_thread(find)
        except OSError:
            logger.warning("Could not read a session transcript")
            return None


@dataclass
class LoggedTurn:
    time: str
    user: str = ""
    reply: str = ""
    kind: str = ""
    tools: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    blocked_reason: str = ""
    duration_ms: int = 0
    guardrails_off: bool = False
    mcp_calls: list[dict[str, str]] = field(default_factory=list)   # every call: tool + status


@dataclass
class LoggedChat:
    id: str
    path: Path
    started: str
    updated: str
    turns: list[LoggedTurn]
    tag: str = ""
    model: str = ""

    @property
    def title(self) -> str:
        first = next((t.user for t in self.turns if t.user.strip()), "New chat")
        first = " ".join(first.split())
        return first if len(first) <= 70 else first[:67].rstrip() + "..."


_TURN_RE = re.compile(r"^---- Turn \d+ \| (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| .*?(?:\| (\d+) ms )?----$")
_EVENT_RE = re.compile(r"^---- \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \| .* ----$")
_ASSISTANT_RE = re.compile(r"^ASSISTANT \[(\w+)\]:$")
_MCP_CALL_RE = re.compile(r"^    - (\S+) .* -> (.+)$")
_GUARDRAIL_RE = re.compile(r"^Guardrail: blocked locally \((.+)\)$")
_CALLED_STATUSES = ("ok", "error returned by tool")   # the calls the pipeline reports in tools_used
_HEADER_BYTES = 1024


def _read_header(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        head = handle.read(_HEADER_BYTES)
    header = {}
    for line in head.splitlines():
        if not line.strip():
            break
        key, sep, value = line.partition(":")
        if sep:
            header[key.strip().lower()] = value.strip()
    return header


def _chat_id(path: Path, header: dict[str, str]) -> str:
    return header.get("chat") or path.stem   # logs written before multi-chat support have no Chat line


def _iso(value: str, fmt: str) -> str:
    try:
        return datetime.strptime(value, fmt).isoformat()
    except ValueError:
        return ""


def parse_transcript(path: Path) -> LoggedChat | None:
    """Rebuild the user/assistant turns of a transcript written by TranscriptLogger."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        updated = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat()
    except OSError:
        return None
    header = _read_header(path)
    turns: list[LoggedTurn] = []
    turn: LoggedTurn | None = None
    block = ""             # "user" or "assistant" while reading an indented message body
    body: list[str] = []

    def flush() -> None:
        message = "\n".join(body).rstrip()
        message = "" if message == "(empty)" else message
        if turn is not None:
            if block == "user":
                turn.user = message
            else:
                turn.reply = message
        body.clear()

    for line in text.splitlines():
        if block:
            if line.startswith("    "):
                body.append(line[4:])
                continue
            flush()
            block = ""
        if match := _TURN_RE.match(line):
            turn = LoggedTurn(time=_iso(match.group(1), "%Y-%m-%d %H:%M:%S"), duration_ms=int(match.group(2) or 0))
            turns.append(turn)
        elif _EVENT_RE.match(line):
            turn = None
        elif turn is None:
            continue
        elif line == "USER:":
            block = "user"
        elif match := _ASSISTANT_RE.match(line):
            turn.kind, block = match.group(1), "assistant"
        elif line.startswith("Notice: "):
            turn.notices.append(line[len("Notice: "):])
        elif match := _GUARDRAIL_RE.match(line):
            turn.blocked_reason = match.group(1)
        elif line.startswith("Guardrails: OFF"):
            turn.guardrails_off = True
        elif match := _MCP_CALL_RE.match(line):
            turn.mcp_calls.append({"tool": match.group(1), "status": match.group(2)})
            if match.group(2) in _CALLED_STATUSES:
                turn.tools.append(match.group(1))
    if block:
        flush()
    return LoggedChat(
        id=_chat_id(path, header), path=path,
        started=_iso(header.get("started", ""), "%Y-%m-%d %H:%M:%S %z") or (turns[0].time if turns else ""),
        updated=updated, turns=turns, tag=header.get("session", ""), model=header.get("model", ""),
    )
