"""FastAPI entry point: wiring, lifecycle and HTTP routes."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.chat.pipeline import ChatPipeline
from app.chat.session import SessionStore
from app.config import (
    APP_DIR, HISTORY_MESSAGES, MAX_MESSAGE_CHARS, MAX_SESSIONS, MCP_TIMEOUT_SECONDS, SESSION_TTL_SECONDS,
    get_settings,
)
from app.llm.gemini import GeminiClient
from app.logging_config import configure_logging
from app.schemas import ChatRequest, ChatResponse
from app.tools.client import McpToolGateway

logger = logging.getLogger(__name__)

SESSION_COOKIE = "gc_session"
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.secrets)
    llm: GeminiClient | None = None
    tools: McpToolGateway | None = None
    warm_up: asyncio.Task | None = None

    problems = settings.problems()
    if problems:
        logger.warning("Advisor not configured; check .env for: %s", ", ".join(problems))
    else:
        llm = GeminiClient(settings.gemini_api_key, settings.llm_model, settings.llm_timeout_seconds)
        tools = McpToolGateway(settings.mcp_url, MCP_TIMEOUT_SECONDS)
        warm_up = asyncio.create_task(tools.warm_up())  # load live tool schemas without blocking startup

    sessions = SessionStore(SESSION_TTL_SECONDS, MAX_SESSIONS, HISTORY_MESSAGES)
    app.state.pipeline = ChatPipeline(sessions, llm, tools)
    app.state.sessions = sessions
    app.state.configured = not problems
    try:
        yield
    finally:
        if warm_up and not warm_up.done():
            warm_up.cancel()
            with suppress(asyncio.CancelledError):
                await warm_up
        if llm:
            await llm.aclose()


app = FastAPI(title="Great.cards Credit Card Advisor", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    for header, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


def _session_id(request: Request, response: Response) -> str:
    session_id = request.cookies.get(SESSION_COOKIE, "")
    if not _SESSION_ID_RE.fullmatch(session_id):
        session_id = secrets.token_urlsafe(32)
        response.set_cookie(
            SESSION_COOKIE, session_id, httponly=True, samesite="lax",
            secure=request.url.scheme == "https", max_age=SESSION_TTL_SECONDS,
        )
    return session_id


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": f"Please enter a message between 1 and {MAX_MESSAGE_CHARS} characters."},
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    response = templates.TemplateResponse(
        request, "index.html",
        {"max_chars": MAX_MESSAGE_CHARS, "configured": request.app.state.configured},
    )
    _session_id(request, response)
    return response


@app.post("/api/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest, request: Request, response: Response) -> ChatResponse:
    session_id = _session_id(request, response)
    pipeline: ChatPipeline = request.app.state.pipeline
    try:
        reply = await pipeline.handle(session_id, payload.message)
    except Exception:
        logger.exception("Unexpected error while handling a chat turn")
        return ChatResponse(reply="Something went wrong on my side. Please try again.", kind="error")
    return ChatResponse(**reply.__dict__)


@app.post("/api/reset", status_code=204)
async def reset(request: Request) -> Response:
    response = Response(status_code=204)
    request.app.state.sessions.reset(_session_id(request, response))
    return response


@app.get("/health")
async def health(request: Request) -> dict[str, object]:
    return {"status": "ok", "configured": request.app.state.configured}
