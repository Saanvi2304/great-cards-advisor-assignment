"""FastAPI entry point: wiring, lifecycle and HTTP routes."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.chat.pipeline import ChatPipeline
from app.chat.session import SessionStore
from app.chat.transcript import TranscriptLogger
from app.config import (
    APP_DIR, APP_LOG_FILE, COOKIE_MAX_AGE_SECONDS, HISTORY_MESSAGES, LOG_DIR, MAX_LISTED_CHATS, SESSION_LOG_DIR,
    MAX_MESSAGE_CHARS, MAX_SESSIONS, MCP_TIMEOUT_SECONDS, SESSION_TTL_SECONDS, get_settings,
)
from app.llm.gemini import GeminiClient
from app.logging_config import configure_logging
from app.schemas import (
    CHAT_ID_PATTERN, ChatHistory, ChatMessage, ChatRequest, ChatResponse, ChatSummary, ServiceStatus, StatusResponse,
)
from app.tools.client import McpToolGateway

logger = logging.getLogger(__name__)

SESSION_COOKIE = "gc_session"
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
MCP_RETRY_SECONDS = 30   # how often a status poll may retry an unreachable MCP server
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
    app.state.pipeline = ChatPipeline(sessions, llm, tools, transcripts)
    app.state.transcripts = transcripts
    app.state.llm = llm
    app.state.tools = tools
    app.state.retry_task = None
    app.state.configured = not problems
    try:
        yield
    finally:
        for task in (warm_up, app.state.retry_task):
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
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
            secure=request.url.scheme == "https", max_age=COOKIE_MAX_AGE_SECONDS,
        )
    return session_id


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    if any("chat_id" in error.get("loc", ()) for error in exc.errors()):
        return JSONResponse(status_code=422, content={"detail": "This chat couldn't be identified. Please start a new chat."})
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
        reply = await pipeline.handle(session_id, payload.message, f"{request.method} {request.url.path}",
                                      payload.chat_id)
    except Exception:
        logger.exception("Unexpected error while handling a chat turn")
        return ChatResponse(reply="Something went wrong on my side. Please try again.", kind="error")
    return ChatResponse(**reply.__dict__)


@app.post("/api/reset", status_code=204)
async def reset(request: Request) -> Response:
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
    chat = await request.app.state.transcripts.load_chat(_session_id(request, response), chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    messages: list[ChatMessage] = []
    for turn in chat.turns:
        messages.append(ChatMessage(role="user", text=turn.user, time=turn.time))
        messages.append(ChatMessage(role="assistant", text=turn.reply, kind=turn.kind, tools_used=turn.tools,
                                    notices=turn.notices, time=turn.time))
    return ChatHistory(id=chat.id, title=chat.title, started=chat.started, messages=messages)


@app.get("/api/status", response_model=StatusResponse)
async def status(request: Request) -> StatusResponse:
    """Gemini and MCP health from the outcome of real calls; no extra Gemini requests are made."""
    state = request.app.state
    llm: GeminiClient | None = state.llm
    tools: McpToolGateway | None = state.tools
    if llm is None:
        llm_status = ServiceStatus(state="off", label="Not configured", detail="Set the Gemini values in .env")
    elif llm.last_error:
        llm_status = ServiceStatus(state="degraded", label="Last call failed", detail=llm.last_error)
    else:
        llm_status = ServiceStatus(state="ready", label="Ready", detail=llm.model)

    if tools is None:
        mcp_status = ServiceStatus(state="off", label="Not configured", detail="Set MCP_URL in .env")
    elif tools.loading or tools.reachable is None:
        mcp_status = ServiceStatus(state="connecting", label="Connecting", detail="Loading Great.cards tools")
    elif tools.reachable:
        mcp_status = ServiceStatus(state="ready", label="Online", detail=f"{tools.tool_count} tools available")
    else:
        mcp_status = ServiceStatus(state="offline", label="Unreachable", detail="Retrying automatically")
        if tools.retry_due(MCP_RETRY_SECONDS):
            state.retry_task = asyncio.create_task(tools.recheck())
    return StatusResponse(llm=llm_status, mcp=mcp_status)


@app.get("/health")
async def health(request: Request) -> dict[str, object]:
    return {"status": "ok", "configured": request.app.state.configured}
