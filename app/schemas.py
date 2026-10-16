"""HTTP request/response models."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.config import MAX_MESSAGE_CHARS

CHAT_ID_PATTERN = r"^[A-Za-z0-9_-]{8,80}$"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    # Optional: lets one browser keep several separate chats. Without it the cookie's single chat is used.
    chat_id: str | None = Field(default=None, pattern=CHAT_ID_PATTERN)

    @field_validator("message")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class ChatResponse(BaseModel):
    reply: str
    kind: str
    tools_used: list[str] = []
    notices: list[str] = []


class ChatSummary(BaseModel):
    id: str
    title: str
    started: str      # ISO 8601
    updated: str      # ISO 8601
    turns: int


class ChatMessage(BaseModel):
    role: str         # user | assistant
    text: str
    kind: str = ""
    tools_used: list[str] = []
    notices: list[str] = []
    time: str = ""


class ChatHistory(BaseModel):
    id: str
    title: str
    started: str
    messages: list[ChatMessage]


class ServiceStatus(BaseModel):
    state: str        # ready | degraded | connecting | offline | off
    label: str
    detail: str = ""


class StatusResponse(BaseModel):
    llm: ServiceStatus
    mcp: ServiceStatus
