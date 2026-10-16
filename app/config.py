"""Configuration: environment values from `.env` plus fixed application limits."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
BASE_DIR = APP_DIR.parent
LOG_DIR = BASE_DIR / "logs"                  # created on startup; git-ignored
SESSION_LOG_DIR = LOG_DIR / "sessions"       # one transcript file per chat session
APP_LOG_FILE = LOG_DIR / "app.log"           # application log (rotating)

# Fixed limits. These are product decisions, not deployment settings.
MAX_MESSAGE_CHARS = 1000
MAX_TOOL_STEPS = 4            # MCP tool calls Gemini may chain within one turn
HISTORY_MESSAGES = 12         # user + assistant messages replayed to Gemini
SESSION_TTL_SECONDS = 60 * 60       # in-memory conversation state
COOKIE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60   # browser identity, so past chats stay listed in the sidebar
MAX_LISTED_CHATS = 50         # most recent session logs shown in the sidebar
MAX_SESSIONS = 1000
MCP_TIMEOUT_SECONDS = 45.0
HEALTH_CACHE_SECONDS = 30     # /health reuses a check result for this long
HEALTH_TIMEOUT_SECONDS = 10.0 # each lightweight health check gives up after this

_PLACEHOLDER_MARKER = "PASTE YOUR"


def _env(name: str) -> str:
    value = os.getenv(name, "").strip().strip('"').strip("'").strip()
    # Treat the untouched .env.example placeholders as "not configured".
    return "" if _PLACEHOLDER_MARKER in value.upper() else value


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(_env(name) or default)
    except ValueError:
        value = default
    return min(max(value, low), high)


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name).lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return default


@dataclass(frozen=True)
class Settings:
    mcp_url: str = field(repr=False)
    llm_provider: str
    llm_model: str
    gemini_api_key: str = field(repr=False)
    llm_timeout_seconds: float
    guardrails_enabled: bool = True
    admin_token: str = field(default="", repr=False)   # optional: allows /admin from other machines

    def problems(self) -> list[str]:
        """Names of missing or invalid variables (never their values)."""
        problems = []
        if not self.mcp_url:
            problems.append("MCP_URL")
        if self.llm_provider != "gemini":
            problems.append("LLM_PROVIDER (only 'gemini' is supported)")
        if not self.llm_model:
            problems.append("LLM_MODEL")
        if not self.gemini_api_key:
            problems.append("GEMINI_API_KEY")
        return problems

    @property
    def secrets(self) -> list[str]:
        """Values that must never appear in logs."""
        return [value for value in (self.gemini_api_key, self.mcp_url, self.admin_token) if value]


@lru_cache
def get_settings() -> Settings:
    # Never override variables already set in the real environment.
    load_dotenv(BASE_DIR / ".env", override=False)
    return Settings(
        mcp_url=_env("MCP_URL"),
        llm_provider=(_env("LLM_PROVIDER") or "gemini").lower(),
        llm_model=_env("LLM_MODEL") or "gemini-2.5-flash-lite",
        gemini_api_key=_env("GEMINI_API_KEY"),
        llm_timeout_seconds=_env_float("LLM_TIMEOUT_SECONDS", 30.0, 5.0, 120.0),
        guardrails_enabled=_env_bool("GUARDRAILS_ENABLED", True),
        admin_token=_env("ADMIN_TOKEN"),
    )
