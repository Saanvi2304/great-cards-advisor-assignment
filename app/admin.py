"""Admin console (/admin): health, chat transcripts and logs, runtime settings, guardrails and routes.

Access: requests from this machine (loopback) are allowed. Other machines need ADMIN_TOKEN from `.env`,
exchanged once for an HttpOnly cookie. State-changing calls must be same-origin JSON requests (CSRF).
Secrets (API key, MCP URL) are only ever returned masked.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import secrets
import time
from collections import Counter
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from starlette.routing import Mount, Route

from app.config import APP_DIR, APP_LOG_FILE
from app.logging_config import scrub
from app.runtime import AdvisorRuntime

ADMIN_COOKIE = "gc_admin"
_COOKIE_KEY = secrets.token_bytes(32)   # per process: admin cookies stop working after a restart
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
MAX_ADMIN_CHATS = 500
MAX_LOG_BYTES = 512_000
MODEL_SUGGESTIONS = ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro"]

router = APIRouter()
templates = Jinja2Templates(directory=APP_DIR / "templates")


# ---------- Access control ----------

def _runtime(request: Request) -> AdvisorRuntime:
    return request.app.state.runtime


def _is_local(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in _LOOPBACK or host.startswith("127.")


def _cookie_value(token: str) -> str:
    return hmac.new(_COOKIE_KEY, token.encode(), hashlib.sha256).hexdigest()


def _authorized(request: Request) -> bool:
    if _is_local(request):
        return True
    token = _runtime(request).defaults.admin_token
    return bool(token) and hmac.compare_digest(request.cookies.get(ADMIN_COOKIE, ""), _cookie_value(token))


def _same_origin_json(request: Request) -> None:
    """Block cross-site form posts and requests from other origins for state-changing calls."""
    if request.method in ("GET", "HEAD"):
        return
    if not request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(status_code=415, detail="Send JSON.")
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="Cross-origin request blocked.")
    if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
        raise HTTPException(status_code=403, detail="Cross-site request blocked.")


async def require_admin(request: Request) -> None:
    if not _authorized(request):
        if _runtime(request).defaults.admin_token:
            raise HTTPException(status_code=401, detail="Sign in with the admin token.")
        raise HTTPException(status_code=403, detail="The admin console is only available on the machine running the server.")
    _same_origin_json(request)


admin_only = [Depends(require_admin)]


# ---------- Masking ----------

def mask_key(key: str) -> str:
    if not key:
        return ""
    return "•" * 8 + (key[-4:] if len(key) >= 16 else "")


def mask_url(url: str) -> str:
    """Scheme and host only: the path or query may carry a token."""
    if not url:
        return ""
    parts = urlsplit(url)
    host = parts.hostname or "?"
    port = f":{parts.port}" if parts.port else ""
    hidden = "/•••" if (parts.path not in ("", "/") or parts.query) else ""
    return f"{parts.scheme}://{host}{port}{hidden}"


# ---------- Request models ----------

class ConnectionSettings(BaseModel):
    """Empty or missing fields keep their current value."""
    llm_model: str | None = Field(None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
    gemini_api_key: str | None = Field(None, min_length=10, max_length=256, pattern=r"^[\x21-\x7e]+$")
    mcp_url: str | None = Field(None, max_length=2048)

    @field_validator("llm_model", "gemini_api_key", "mcp_url", mode="before")
    @classmethod
    def blank_to_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("mcp_url")
    @classmethod
    def valid_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("MCP URL must be an http(s) URL")
        return value.strip()

    def values(self) -> dict[str, str]:
        return {k: v for k, v in self.model_dump().items() if v}


class GuardrailsUpdate(BaseModel):
    enabled: bool


class LoginRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)


# ---------- Views ----------

def _settings_view(runtime: AdvisorRuntime) -> dict[str, Any]:
    current, defaults, overridden = runtime.settings, runtime.defaults, runtime.overrides()
    return {
        "provider": current.llm_provider,
        "llm_model": {"value": current.llm_model, "default": defaults.llm_model, "overridden": overridden["llm_model"]},
        "gemini_api_key": {"masked": mask_key(current.gemini_api_key), "set": bool(current.gemini_api_key),
                           "overridden": overridden["gemini_api_key"]},
        "mcp_url": {"masked": mask_url(current.mcp_url), "set": bool(current.mcp_url),
                    "overridden": overridden["mcp_url"]},
        "guardrails": {"enabled": current.guardrails_enabled, "default": defaults.guardrails_enabled,
                       "overridden": overridden["guardrails_enabled"]},
        "llm_timeout_seconds": current.llm_timeout_seconds,
        "configured": runtime.configured,
        "problems": current.problems(),
        "model_suggestions": sorted(set(MODEL_SUGGESTIONS) | {current.llm_model, defaults.llm_model} - {""}),
    }


def _chat_summary(chat: Any) -> dict[str, Any]:
    kinds = Counter(t.kind or "unknown" for t in chat.turns)
    return {
        "name": chat.path.stem, "id": chat.id, "tag": chat.tag, "model": chat.model, "title": chat.title,
        "started": chat.started, "updated": chat.updated, "turns": len(chat.turns),
        "kinds": dict(kinds), "blocked": kinds.get("blocked", 0), "errors": kinds.get("error", 0),
        "guardrails_off": sum(t.guardrails_off for t in chat.turns),
        "size_bytes": chat.path.stat().st_size if chat.path.exists() else 0,
    }


def _stats(chats: list[Any]) -> dict[str, Any]:
    turns = [t for chat in chats for t in chat.turns]
    durations = [t.duration_ms for t in turns if t.duration_ms]
    today = datetime.now().date().isoformat()
    return {
        "chats": len(chats),
        "browsers": len({c.tag for c in chats if c.tag}),
        "turns": len(turns),
        "turns_today": sum(t.time.startswith(today) for t in turns),
        "avg_response_ms": round(sum(durations) / len(durations)) if durations else 0,
        "kinds": dict(Counter(t.kind or "unknown" for t in turns).most_common()),
        "tools": dict(Counter(tool for t in turns for tool in t.tools).most_common(8)),
        "blocked": dict(Counter(t.blocked_reason for t in turns if t.blocked_reason).most_common()),
        "mcp_rejected": sum(c["status"].startswith("rejected") for t in turns for c in t.mcp_calls),
        "guardrails_off_turns": sum(t.guardrails_off for t in turns),
    }


def _read_app_log(lines: int, level: str) -> list[str]:
    if not APP_LOG_FILE.exists():
        return []
    with APP_LOG_FILE.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - MAX_LOG_BYTES))
        raw = handle.read().decode("utf-8", errors="replace")
    rows = raw.splitlines()
    if size > MAX_LOG_BYTES and rows:
        rows = rows[1:]   # first line may be cut in half
    # Group traceback/continuation lines with the record they belong to.
    entries: list[list[str]] = []
    for row in rows:
        if row[:4].isdigit() or not entries:
            entries.append([row])
        else:
            entries[-1].append(row)
    if level:
        entries = [e for e in entries if f" {level} " in e[0][:40]]
    flat = [line for entry in entries for line in entry]
    return [scrub(line) for line in flat[-lines:]]   # defence in depth: records were already filtered


def _discover_routes(request: Request) -> list[dict[str, Any]]:
    """Every route the app serves, read from the router itself (so new routes appear automatically)."""
    def walk(routes: list[Any], prefix: str = "") -> list[tuple[str, Any]]:
        flat = []
        for route in routes:
            router = getattr(route, "original_router", None)   # FastAPI >= 0.140 wraps included routers
            if router is not None:
                context = getattr(route, "include_context", None)
                flat += walk(router.routes, prefix + (getattr(context, "prefix", "") or ""))
            else:
                flat.append((prefix, route))
        return flat

    found = []
    for prefix, route in walk(request.app.routes):
        if isinstance(route, Mount):
            found.append({"path": f"{prefix}{route.path}/{{path}}", "methods": ["GET"], "name": route.name or "",
                          "purpose": "Static assets (stylesheets and JavaScript) for the web pages.",
                          "kind": "Static", "access": "Public", "params": ["path"]})
            continue
        if not isinstance(route, (APIRoute, Route)):
            continue
        methods = sorted((route.methods or {"GET"}) - {"HEAD", "OPTIONS"})
        doc = inspect.getdoc(route.endpoint) or ""
        purpose = " ".join(doc.split("\n\n")[0].split()) or route.name.replace("_", " ").capitalize()
        path = prefix + route.path
        if path.startswith("/admin/api"):
            kind = "Admin API"
        elif path.startswith("/api") or path == "/health":
            kind = "API"
        elif path.endswith(".json"):
            kind = "Schema"
        else:
            kind = "Page"
        is_admin = isinstance(route, APIRoute) and any(
            dep.call is require_admin for dep in route.dependant.dependencies)
        params = list(route.param_convertors)
        if isinstance(route, APIRoute):
            params += [f"?{p.name}" for p in route.dependant.query_params]
        found.append({"path": path, "methods": methods, "name": route.name, "purpose": purpose,
                      "kind": kind, "access": "Admin" if is_admin else "Public", "params": params})
    order = {"Page": 0, "API": 1, "Admin API": 2, "Schema": 3, "Static": 4}
    return sorted(found, key=lambda r: (order.get(r["kind"], 9), r["path"]))


# ---------- Routes ----------

@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> Response:
    """Admin console: health, chat history and logs, model and API settings, guardrails and all routes."""
    return templates.TemplateResponse(request, "admin.html", {})


@router.get("/admin/api/session")
async def admin_session(request: Request) -> dict[str, Any]:
    """Whether this browser may use the admin console, and whether a token sign-in is available."""
    return {"authorized": _authorized(request), "local": _is_local(request),
            "token_login": bool(_runtime(request).defaults.admin_token)}


@router.post("/admin/api/login")
async def admin_login(payload: LoginRequest, request: Request, response: Response) -> dict[str, bool]:
    """Exchange ADMIN_TOKEN for an HttpOnly admin cookie (remote access)."""
    _same_origin_json(request)
    token = _runtime(request).defaults.admin_token
    if not token or not hmac.compare_digest(payload.token.encode(), token.encode()):
        await asyncio.sleep(1)   # slow down guessing
        raise HTTPException(status_code=401, detail="That token isn't valid.")
    response.set_cookie(ADMIN_COOKIE, _cookie_value(token), httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", path="/admin", max_age=8 * 3600)
    return {"authorized": True}


@router.post("/admin/api/logout")
async def admin_logout(request: Request, response: Response) -> dict[str, bool]:
    """Remove the admin cookie."""
    _same_origin_json(request)
    response.delete_cookie(ADMIN_COOKIE, path="/admin")
    return {"authorized": _is_local(request)}


@router.get("/admin/api/overview", dependencies=admin_only)
async def admin_overview(request: Request) -> dict[str, Any]:
    """Health, uptime, settings summary and usage statistics from the session logs."""
    runtime = _runtime(request)
    health, chats = await asyncio.gather(runtime.health(), request.app.state.transcripts.list_all(MAX_ADMIN_CHATS))
    return {
        "health": health,
        "started_at": runtime.started_at.isoformat(timespec="seconds"),
        "uptime_seconds": round((datetime.now().astimezone() - runtime.started_at).total_seconds()),
        "settings": _settings_view(runtime),
        "active_conversations": len(request.app.state.sessions),
        "stats": _stats(chats),
        "recent": [_chat_summary(c) for c in chats[:6]],
    }


@router.get("/admin/api/chats", dependencies=admin_only)
async def admin_chats(request: Request, limit: int = Query(200, ge=1, le=MAX_ADMIN_CHATS)) -> list[dict[str, Any]]:
    """All saved chat transcripts (every browser), newest first."""
    chats = await request.app.state.transcripts.list_all(limit)
    return [_chat_summary(c) for c in chats]


@router.get("/admin/api/chats/{name}", dependencies=admin_only)
async def admin_chat(request: Request, name: str = Path(pattern=r"^session_[A-Za-z0-9_-]{1,120}$")) -> dict[str, Any]:
    """One transcript: its parsed turns and the raw (already redacted) log text."""
    found = await request.app.state.transcripts.load_file(name)
    if found is None:
        raise HTTPException(status_code=404, detail="Transcript not found.")
    chat, raw = found
    turns = [{"time": t.time, "user": t.user, "reply": t.reply, "kind": t.kind, "tools": t.tools,
              "notices": t.notices, "blocked_reason": t.blocked_reason, "duration_ms": t.duration_ms,
              "guardrails_off": t.guardrails_off, "mcp_calls": t.mcp_calls} for t in chat.turns]
    return {**_chat_summary(chat), "turns_detail": turns, "raw": scrub(raw)}


@router.get("/admin/api/logs/app", dependencies=admin_only)
async def admin_app_log(lines: int = Query(300, ge=10, le=5000),
                        level: str = Query("", pattern=r"^(|DEBUG|INFO|WARNING|ERROR|CRITICAL)$")) -> dict[str, Any]:
    """The last lines of logs/app.log, optionally filtered by level."""
    rows = await asyncio.to_thread(_read_app_log, lines, level)
    return {"file": APP_LOG_FILE.name, "lines": rows}


@router.get("/admin/api/settings", dependencies=admin_only)
async def admin_get_settings(request: Request) -> dict[str, Any]:
    """Current model, API key (masked), MCP URL (masked) and guardrails, with their .env defaults."""
    return _settings_view(_runtime(request))


@router.put("/admin/api/settings", dependencies=admin_only)
async def admin_update_settings(payload: ConnectionSettings, request: Request) -> dict[str, Any]:
    """Apply a new model, API key and/or MCP URL in memory (never written to .env), then check health."""
    runtime = _runtime(request)
    changed = runtime.update_connection(**payload.values())
    return {"changed": changed, "settings": _settings_view(runtime), "health": await runtime.health(force=True)}


@router.post("/admin/api/settings/test", dependencies=admin_only)
async def admin_test_settings(payload: ConnectionSettings, request: Request) -> dict[str, Any]:
    """Health-check candidate settings with temporary clients, without applying them."""
    return await _runtime(request).test_connection(**payload.values())


@router.post("/admin/api/settings/reset", dependencies=admin_only)
async def admin_reset_settings(request: Request) -> dict[str, Any]:
    """Return the model, API key and MCP URL to their .env values."""
    runtime = _runtime(request)
    changed = runtime.reset_connection()
    return {"changed": changed, "settings": _settings_view(runtime), "health": await runtime.health(force=True)}


@router.get("/admin/api/guardrails", dependencies=admin_only)
async def admin_get_guardrails(request: Request) -> dict[str, Any]:
    """Whether input/output guardrails are on, and their .env default."""
    return _settings_view(_runtime(request))["guardrails"]


@router.put("/admin/api/guardrails", dependencies=admin_only)
async def admin_set_guardrails(payload: GuardrailsUpdate, request: Request) -> dict[str, Any]:
    """Switch the input guardrails on or off for new chat turns (in memory; resets on restart).
    The approval and tax output rules always run."""
    runtime = _runtime(request)
    runtime.set_guardrails(payload.enabled)
    return _settings_view(runtime)["guardrails"]


@router.get("/admin/api/endpoints", dependencies=admin_only)
async def admin_endpoints(request: Request) -> dict[str, Any]:
    """Every page and API route, discovered from the running app."""
    return {"generated_at": time.time(), "routes": _discover_routes(request)}
