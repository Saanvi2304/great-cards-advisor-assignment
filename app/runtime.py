"""Runtime services: the Gemini and MCP clients built from the current settings, and their health checks.

Settings start from `.env` (see config.get_settings). The admin console may override the model, the
Gemini API key, the MCP URL and the guardrails switch. Overrides live only in this process's memory:
they survive page reloads, are never written to `.env`, and reset when the backend restarts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import datetime
from typing import Any, Coroutine

from app.chat.pipeline import ChatPipeline
from app.chat.transcript import TranscriptLogger
from app.config import HEALTH_CACHE_SECONDS, HEALTH_TIMEOUT_SECONDS, MCP_TIMEOUT_SECONDS, Settings
from app.llm.gemini import GeminiClient, LLMError
from app.logging_config import register_secrets
from app.tools.client import McpToolGateway, ToolServiceError

logger = logging.getLogger(__name__)

CONNECTION_FIELDS = ("llm_model", "gemini_api_key", "mcp_url")
MIN_FORCED_CHECK_SECONDS = 5.0   # "check now" requests closer together than this reuse the last result


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _status(state: str, label: str, detail: str = "", latency_ms: int | None = None) -> dict[str, Any]:
    return {"state": state, "label": label, "detail": detail, "latency_ms": latency_ms, "checked_at": _now()}


class AdvisorRuntime:
    def __init__(self, defaults: Settings, pipeline: ChatPipeline, transcripts: TranscriptLogger):
        self.defaults = defaults
        self.settings = defaults
        self.llm: GeminiClient | None = None
        self.tools: McpToolGateway | None = None
        self.started_at = datetime.now().astimezone()
        self._pipeline = pipeline
        self._transcripts = transcripts
        self._tasks: set[asyncio.Task] = set()
        self._generation = 0          # bumped on every settings change, so stale health results are dropped
        self._health: dict[str, Any] | None = None
        self._health_at = 0.0
        self._health_llm_error = ""   # llm.last_error when the cached result was taken
        self._health_lock = asyncio.Lock()

    # ---- Settings ----

    @property
    def configured(self) -> bool:
        return not self.settings.problems()

    def start(self) -> None:
        self._install(self.defaults)

    def overrides(self) -> dict[str, bool]:
        """Which settings currently differ from the `.env` defaults."""
        fields = CONNECTION_FIELDS + ("guardrails_enabled",)
        return {name: getattr(self.settings, name) != getattr(self.defaults, name) for name in fields}

    def update_connection(self, **values: str) -> list[str]:
        """Apply new connection settings (only non-empty values change). Returns the changed field names."""
        new = replace(self.settings, **{k: v for k, v in values.items() if k in CONNECTION_FIELDS and v})
        changed = [name for name in CONNECTION_FIELDS if getattr(new, name) != getattr(self.settings, name)]
        if changed:
            self._install(new)
        # Field names only: values (keys, URLs) never reach the logs.
        logger.info("Runtime connection settings applied; changed: %s", ", ".join(changed) or "nothing")
        return changed

    def reset_connection(self) -> list[str]:
        values = {name: getattr(self.defaults, name) for name in CONNECTION_FIELDS}
        new = replace(self.settings, **values)
        changed = [name for name in CONNECTION_FIELDS if getattr(new, name) != getattr(self.settings, name)]
        if changed:
            self._install(new)
        logger.info("Runtime connection settings reset to .env defaults; changed: %s", ", ".join(changed) or "nothing")
        return changed

    def set_guardrails(self, enabled: bool) -> None:
        if enabled == self.settings.guardrails_enabled:
            return
        self.settings = replace(self.settings, guardrails_enabled=enabled)
        self._pipeline.guardrails_enabled = enabled
        if enabled:
            logger.info("Guardrails switched ON at runtime")
        else:
            logger.warning("Guardrails switched OFF at runtime (redaction and grounding stay on)")

    def _install(self, settings: Settings) -> None:
        register_secrets(settings.secrets)   # before anything could log them
        previous = self.settings
        old_llm, old_tools = self.llm, self.tools
        if settings.problems():
            llm, tools = None, None
            logger.warning("Advisor not configured; missing: %s", ", ".join(settings.problems()))
        else:
            same_llm = (old_llm is not None and previous.llm_model == settings.llm_model
                        and previous.gemini_api_key == settings.gemini_api_key)
            llm = old_llm if same_llm else GeminiClient(
                settings.gemini_api_key, settings.llm_model, settings.llm_timeout_seconds)
            same_tools = old_tools is not None and previous.mcp_url == settings.mcp_url
            tools = old_tools if same_tools else McpToolGateway(settings.mcp_url, MCP_TIMEOUT_SECONDS)
            if tools is not old_tools:
                self._spawn(tools.warm_up())   # load live tool schemas without blocking
        if old_llm is not None and old_llm is not llm:
            self._spawn(self._close_later(old_llm, settings.llm_timeout_seconds + 5))
        self.settings, self.llm, self.tools = settings, llm, tools
        self._pipeline.configure(llm, tools, settings.guardrails_enabled)
        self._transcripts.set_model(settings.llm_model)
        self._generation += 1
        self._health = None

    async def _close_later(self, llm: GeminiClient, delay: float) -> None:
        await asyncio.sleep(delay)   # let turns that already hold the old client finish
        await llm.aclose()

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.llm:
            await self.llm.aclose()

    # ---- Health checks ----

    async def health(self, force: bool = False) -> dict[str, Any]:
        """Cached lightweight checks of Gemini and MCP (see GeminiClient.ping and McpToolGateway.ping)."""
        max_age = MIN_FORCED_CHECK_SECONDS if force else HEALTH_CACHE_SECONDS
        if self._health_fresh(max_age):
            return self._health
        async with self._health_lock:
            if self._health_fresh(max_age):
                return self._health
            generation = self._generation
            llm_error = self.llm.last_error if self.llm else ""
            result = await self._check(self.llm, self.tools, self.settings)
            if generation == self._generation:
                self._health, self._health_at, self._health_llm_error = result, time.monotonic(), llm_error
            return result

    def _health_fresh(self, max_age: float) -> bool:
        """The cached result is usable unless it's too old or a chat turn has since failed (or recovered)."""
        llm_error = self.llm.last_error if self.llm else ""
        return (self._health is not None and time.monotonic() - self._health_at < max_age
                and llm_error == self._health_llm_error)

    async def test_connection(self, **values: str) -> dict[str, Any]:
        """Check candidate settings with throwaway clients, without applying them."""
        candidate = replace(self.settings, **{k: v for k, v in values.items() if k in CONNECTION_FIELDS and v})
        register_secrets(candidate.secrets)
        llm = tools = None
        if not candidate.problems():
            llm = GeminiClient(candidate.gemini_api_key, candidate.llm_model, candidate.llm_timeout_seconds)
            tools = McpToolGateway(candidate.mcp_url, MCP_TIMEOUT_SECONDS)
        try:
            return await self._check(llm, tools, candidate, warm=False)
        finally:
            if llm:
                await llm.aclose()

    async def _check(self, llm: GeminiClient | None, tools: McpToolGateway | None, settings: Settings,
                     warm: bool = True) -> dict[str, Any]:
        llm_status, mcp_status = await asyncio.gather(
            self._check_llm(llm, settings), self._check_mcp(tools, settings, warm))
        states = {llm_status["state"], mcp_status["state"]}
        overall = "ok" if states == {"ready"} else "down" if states <= {"offline", "off"} else "degraded"
        return {
            "status": overall,
            "configured": not settings.problems(),
            "guardrails_enabled": settings.guardrails_enabled,
            "checked_at": _now(),
            "llm": llm_status,
            "mcp": mcp_status,
        }

    @staticmethod
    async def _check_llm(llm: GeminiClient | None, settings: Settings) -> dict[str, Any]:
        if llm is None:
            missing = [p for p in settings.problems() if not p.startswith("MCP")]
            return _status("off", "Not configured", f"Missing: {', '.join(missing)}" if missing else "Waiting for MCP_URL")
        started = time.perf_counter()
        try:
            await llm.ping(HEALTH_TIMEOUT_SECONDS)
        except LLMError as exc:
            latency = round((time.perf_counter() - started) * 1000)
            message = exc.user_message
            rate_limited = "rate-limited" in message
            label = ("Rate-limited" if rate_limited else "Model not found" if "not found" in message
                     else "Key rejected" if "key" in message.lower() else "Unreachable")
            return _status("degraded" if rate_limited else "offline", label, message, latency)
        latency = round((time.perf_counter() - started) * 1000)
        if llm.last_error:
            return _status("degraded", "Last request failed", f"{llm.model}: {llm.last_error}", latency)
        return _status("ready", "Connected", llm.model, latency)

    def _check_mcp_warm(self, tools: McpToolGateway) -> None:
        if tools.tool_count == 0 and not tools.loading:
            self._spawn(tools.warm_up())

    async def _check_mcp(self, tools: McpToolGateway | None, settings: Settings, warm: bool) -> dict[str, Any]:
        if tools is None:
            missing = [p for p in settings.problems() if p.startswith("MCP")]
            return _status("off", "Not configured", "Missing: MCP_URL" if missing else "Waiting for Gemini settings")
        started = time.perf_counter()
        try:
            await tools.ping(HEALTH_TIMEOUT_SECONDS)
        except ToolServiceError:
            latency = round((time.perf_counter() - started) * 1000)
            return _status("offline", "Unreachable", "The MCP server didn't answer the health check", latency)
        latency = round((time.perf_counter() - started) * 1000)
        if warm:
            self._check_mcp_warm(tools)
        count = tools.tool_count
        return _status("ready", "Online", f"{count} tools available" if count else "Connected", latency)
