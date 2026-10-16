"""In-memory conversation state, keyed by an opaque session cookie.

Only redacted text is stored. Replace SessionStore with a shared store (e.g. Redis)
if the app ever runs with multiple worker processes.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path

MAX_PENDING_REQUEST_CHARS = 3000


@dataclass
class Conversation:
    max_messages: int
    history: deque[tuple[str, str]] = field(init=False)
    # User text of turns that ended with a clarifying question; together with the next
    # message it forms the "current request" whose figures tools and answers may use.
    pending_request: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_seen: float = field(default_factory=time.monotonic)
    turn_count: int = 0
    log_path: Path | None = None   # this conversation's transcript file, created on the first turn
    chat_id: str = ""              # client-chosen id when one browser keeps several chats

    def __post_init__(self) -> None:
        self.history = deque(maxlen=self.max_messages)

    def add_exchange(self, user_text: str, assistant_text: str) -> None:
        self.turn_count += 1
        self.history.append(("user", user_text))
        self.history.append(("model", assistant_text))

    def user_text(self) -> str:
        return "\n".join(text for role, text in self.history if role == "user")

    def request_text(self, text: str) -> str:
        """The current request: `text` plus any earlier messages that led to a clarifying question."""
        return f"{self.pending_request}\n{text}".strip()

    def track_request(self, user_text: str, kind: str) -> None:
        """Carry a request across clarifying questions; any other reply closes it."""
        self.pending_request = self.request_text(user_text)[-MAX_PENDING_REQUEST_CHARS:] if kind == "clarification" else ""


class SessionStore:
    def __init__(self, ttl_seconds: int, max_sessions: int, max_messages: int):
        self._ttl = ttl_seconds
        self._max_sessions = max_sessions
        self._max_messages = max_messages
        self._sessions: OrderedDict[str, Conversation] = OrderedDict()

    def __len__(self) -> int:
        return len(self._sessions)

    def get(self, session_id: str) -> Conversation:
        self._prune()
        conversation = self._sessions.get(session_id)
        if conversation is None:
            conversation = Conversation(max_messages=self._max_messages)
            self._sessions[session_id] = conversation
        conversation.last_seen = time.monotonic()
        self._sessions.move_to_end(session_id)
        return conversation

    def pop(self, session_id: str) -> Conversation | None:
        """Remove and return a conversation (used when the user starts a new chat)."""
        return self._sessions.pop(session_id, None)

    def _prune(self) -> None:
        cutoff = time.monotonic() - self._ttl
        while self._sessions:
            oldest_id, oldest = next(iter(self._sessions.items()))
            if oldest.last_seen >= cutoff and len(self._sessions) < self._max_sessions:
                break
            if oldest.lock.locked():  # never evict a conversation mid-turn
                break
            self._sessions.popitem(last=False)
