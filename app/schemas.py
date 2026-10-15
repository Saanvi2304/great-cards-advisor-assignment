"""HTTP request/response models."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.config import MAX_MESSAGE_CHARS


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)

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
