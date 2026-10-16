"""FastAPI entry point: wiring, lifecycle and HTTP routes."""

from __future__ import annotations

import logging
import re
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.admin import router as admin_router
from app.chat.pipeline import ChatPipeline
from app.chat.session import SessionStore
from app.chat.transcript import TranscriptLogger
from app.config import (
    APP_DIR, APP_LOG_FILE, COOKIE_MAX_AGE_SECONDS, HISTORY_MESSAGES, LOG_DIR, MAX_LISTED_CHATS, SESSION_LOG_DIR,
    MAX_MESSAGE_CHARS, MAX_SESSIONS, SESSION_TTL_SECONDS, get_settings,
)
from app.logging_config import configure_logging
from app.runtime import AdvisorRuntime
from app.schemas import CHAT_ID_PATTERN, ChatHistory, ChatMessage, ChatRequest, ChatResponse, ChatSummary, HealthResponse

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
    LOG_DIR.mkdir(parents=True, exist_ok=True)  # create logs/ if the project doesn't have it yet
    configure_logging(settings.secrets, APP_LOG_FILE)
    transcripts = TranscriptLogger(SESSION_LOG_DIR, settings.llm_model)
    transcripts.ensure_directory()
    sessions = SessionStore(SESSION_TTL_SECONDS, MAX_SESSIONS, HISTORY_MESSAGES)
    pipeline = ChatPipeline(sessions, None, None, transcripts)
    # Builds the Gemini client and MCP gateway from .env (and later from /admin overrides).
    runtime = AdvisorRuntime(settings, pipeline, transcripts)
    runtime.start()

    app.state.pipeline = pipeline
    app.state.sessions = sessions
    app.state.transcripts = transcripts
    app.state.runtime = runtime
    try:
        yield
    finally:
        await runtime.aclose()


app = FastAPI(title="Great.cards Credit Card Advisor", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
app.include_router(admin_router)
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
            secure=request.url.scheme == "https", max_age=COOKIE_MAX_AGE_SECONDS,
        )
    return session_id


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    if request.url.path.startswith("/admin/api"):
        # Field names and messages only: the submitted values (e.g. an API key) are never echoed back.
        problems = [f"{error['loc'][-1]}: {error['msg']}" for error in exc.errors() if error.get("loc")]
        return JSONResponse(status_code=422, content={"detail": "; ".join(problems) or "Invalid request."})
    if any("chat_id" in error.get("loc", ()) for error in exc.errors()):
        return JSONResponse(status_code=422, content={"detail": "This chat couldn't be identified. Please start a new chat."})
    return JSONResponse(
        status_code=422,
        content={"detail": f"Please enter a message between 1 and {MAX_MESSAGE_CHARS} characters."},
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    """Chat dashboard: sidebar with chats, service status and examples, and the chat panel."""
    response = templates.TemplateResponse(
        request, "index.html",
        {"max_chars": MAX_MESSAGE_CHARS, "configured": request.app.state.runtime.configured},
    )
    _session_id(request, response)
    return response


@app.post("/api/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest, request: Request, response: Response) -> ChatResponse:
    """Send one chat message through the advisor pipeline and get the reply."""
    session_id = _session_id(request, response)
    pipeline: ChatPipeline = request.app.state.pipeline
    try:
        reply = await pipeline.handle(session_id, payload.message, f"{request.method} {request.url.path}",
                                      payload.chat_id)
    except Exception:
        logger.exception("Unexpected error while handling a chat turn")
        return ChatResponse(reply="Something went wrong on my side. Please try again.", kind="error")
    return ChatResponse(**reply.__dict__)


@app.post("/api/reset", status_code=204)
async def reset(request: Request) -> Response:
    """Clear the cookie's single conversation and close its session log (API clients without chat ids)."""
    response = Response(status_code=204)
    pipeline: ChatPipeline = request.app.state.pipeline
    await pipeline.reset(_session_id(request, response), f"{request.method} {request.url.path}")
    return response


@app.get("/api/conversations", response_model=list[ChatSummary])
async def conversations(request: Request, response: Response) -> list[ChatSummary]:
    """This browser's past chats, newest first, read from its session transcripts."""
    chats = await request.app.state.transcripts.list_chats(_session_id(request, response), MAX_LISTED_CHATS)
    return [ChatSummary(id=c.id, title=c.title, started=c.started, updated=c.updated, turns=len(c.turns))
            for c in chats]


@app.get("/api/conversations/{chat_id}", response_model=ChatHistory)
async def conversation(request: Request, response: Response,
                       chat_id: str = Path(pattern=CHAT_ID_PATTERN)) -> ChatHistory:
    """Full message history of one of this browser's chats, rebuilt from its session log."""
    chat = await request.app.state.transcripts.load_chat(_session_id(request, response), chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    messages: list[ChatMessage] = []
    for turn in chat.turns:
        messages.append(ChatMessage(role="user", text=turn.user, time=turn.time))
        messages.append(ChatMessage(role="assistant", text=turn.reply, kind=turn.kind, tools_used=turn.tools,
                                    notices=turn.notices, time=turn.time))
    return ChatHistory(id=chat.id, title=chat.title, started=chat.started, messages=messages)


@app.get("/health", response_model=HealthResponse)
async def health(request: Request, refresh: bool = Query(False)) -> HealthResponse:
    """Gemini and MCP connection health from lightweight checks (model metadata lookup, MCP ping),
    cached for 30 seconds; `?refresh=true` checks again (at most every 5 seconds)."""
    return HealthResponse(**await request.app.state.runtime.health(force=refresh))
