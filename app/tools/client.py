"""Async client for the live Great.cards MCP server.

Connections are short-lived and scoped to a single chat turn: a turn opens one MCP
session lazily on its first tool call and closes it when the turn ends. This keeps
anyio cancel scopes in the task that created them and avoids stale long-lived sessions.
The tool catalog (live schemas) and advisor context are cached and refreshed on a TTL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, AsyncIterator

import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from app.tools.registry import CONTEXT_TOOL, CONTEXT_TOOL_ARGS, ToolCatalog, ToolSpec, prepare_arguments

logger = logging.getLogger(__name__)

CATALOG_TTL_SECONDS = 30 * 60
MAX_RESULT_CHARS = 15_000
MAX_CONTEXT_CHARS = 60_000


class ToolServiceError(Exception):
    """The MCP server could not be reached or failed to answer."""


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: Any      # what Gemini sees
    text: str      # serialised form, used as grounding evidence
    raw: str       # the full serialised response, never truncated (kept in transcripts for auditing)

    @property
    def truncated(self) -> bool:
        return self.text != self.raw


def _to_result(result: Any, max_chars: int = MAX_RESULT_CHARS) -> ToolResult:
    if result.structuredContent is not None:
        data: Any = result.structuredContent
    else:
        raw = "\n".join(c.text for c in result.content if getattr(c, "type", None) == "text")
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            data = raw
    raw = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    text = raw
    if len(text) > max_chars:
        text = text[:max_chars] + " ...[truncated]"
        data = text
    return ToolResult(ok=not result.isError, data=data, text=text, raw=raw)


class ToolConnection:
    """One lazily opened MCP session, used for all tool calls within a chat turn."""

    def __init__(self, gateway: "McpToolGateway"):
        self._gateway = gateway
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        try:
            if self._session is None:
                self._stack = AsyncExitStack()
                self._session = await self._stack.enter_async_context(self._gateway.open_session())
            result = await self._session.call_tool(
                name, arguments, read_timeout_seconds=timedelta(seconds=self._gateway.timeout)
            )
        except Exception as exc:  # transport errors arrive as many types, incl. ExceptionGroup
            await self.aclose()
            self._gateway.reachable = False
            raise ToolServiceError(f"MCP call to {name} failed ({type(exc).__name__})") from exc
        self._gateway.reachable = True
        return _to_result(result)

    async def aclose(self) -> None:
        stack, self._stack, self._session = self._stack, None, None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                logger.debug("Ignoring error while closing MCP session", exc_info=True)


class McpToolGateway:
    def __init__(self, url: str, timeout: float):
        self._url = url
        self.timeout = timeout
        self._catalog: ToolCatalog | None = None
        self._advisor_context = ""
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()
        self.reachable: bool | None = None   # outcome of the latest connection attempt (None = not tried yet)

    @property
    def tool_count(self) -> int:
        return len(self._catalog.exposed()) if self._catalog else 0

    @property
    def loading(self) -> bool:
        return self._lock.locked()

    @property
    def advisor_context(self) -> str:
        return self._advisor_context

    @asynccontextmanager
    async def open_session(self) -> AsyncIterator[ClientSession]:
        async with AsyncExitStack() as stack:
            if self._url.rstrip("/").endswith("/sse"):
                read, write = await stack.enter_async_context(
                    sse_client(self._url, timeout=self.timeout, sse_read_timeout=self.timeout)
                )
            else:
                http_client = await stack.enter_async_context(
                    create_mcp_http_client(timeout=httpx.Timeout(self.timeout))
                )
                read, write, _ = await stack.enter_async_context(
                    streamable_http_client(self._url, http_client=http_client)
                )
            session = await stack.enter_async_context(
                ClientSession(read, write, read_timeout_seconds=timedelta(seconds=self.timeout))
            )
            await session.initialize()
            yield session

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[ToolConnection]:
        conn = ToolConnection(self)
        try:
            yield conn
        finally:
            await conn.aclose()

    async def catalog(self) -> ToolCatalog:
        """Live tool catalog, fetched on first use and refreshed after the TTL."""
        fresh = self._catalog is not None and time.monotonic() - self._loaded_at < CATALOG_TTL_SECONDS
        if fresh:
            return self._catalog  # type: ignore[return-value]
        async with self._lock:
            if self._catalog is None or time.monotonic() - self._loaded_at >= CATALOG_TTL_SECONDS:
                try:
                    await self._refresh()
                    self.reachable = True
                except Exception as exc:
                    self.reachable = False
                    if self._catalog is None:
                        raise ToolServiceError(f"Could not load MCP tools ({type(exc).__name__})") from exc
                    logger.warning("MCP catalog refresh failed; keeping the cached catalog")
            return self._catalog  # type: ignore[return-value]

    async def _refresh(self) -> None:
        async with self.open_session() as session:
            specs: dict[str, ToolSpec] = {}
            cursor: str | None = None
            while True:
                page = await session.list_tools(cursor)
                for tool in page.tools:
                    specs[tool.name] = ToolSpec.from_mcp(tool)
                cursor = page.nextCursor
                if not cursor:
                    break
            context = await self._load_advisor_context(session, specs.get(CONTEXT_TOOL))
        self._catalog = ToolCatalog(specs)
        self._advisor_context = context
        self._loaded_at = time.monotonic()
        logger.info("Loaded %d MCP tools (advisor context: %d chars)", len(specs), len(context))

    async def _load_advisor_context(self, session: ClientSession, spec: ToolSpec | None) -> str:
        if spec is None:
            return ""
        args, errors = prepare_arguments(spec, CONTEXT_TOOL_ARGS)
        if errors:
            args = {}
        try:
            result = _to_result(await session.call_tool(CONTEXT_TOOL, args), MAX_CONTEXT_CHARS)
        except Exception:
            logger.warning("Advisor context could not be loaded; continuing without it")
            return ""
        if not result.ok:
            return ""
        data = result.data
        if isinstance(data, dict) and isinstance(data.get("context"), str):
            data = data["context"]
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        return text[:MAX_CONTEXT_CHARS]

    async def ping(self, timeout: float) -> None:
        """Lightweight health check: open a session (MCP initialize) and send an MCP ping; no tool runs."""
        try:
            async with asyncio.timeout(timeout):
                async with self.open_session() as session:
                    await session.send_ping()
        except Exception as exc:  # timeouts and transport errors (incl. ExceptionGroup)
            self.reachable = False
            raise ToolServiceError(f"MCP ping failed ({type(exc).__name__})") from exc
        self.reachable = True

    async def warm_up(self) -> None:
        try:
            await self.catalog()
        except ToolServiceError:
            logger.warning("MCP server not reachable at startup; will retry on the first question")
