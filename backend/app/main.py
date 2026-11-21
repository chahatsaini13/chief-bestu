import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db, llm, slang
from .config import DEFAULT_SYSTEM_PROMPT, Settings

log = logging.getLogger("chief-bestu")

SAFE_REPLY = "Arre bhai, tameez mein! Chal, seedha bol kya scene hai, main madad karta hoon."


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    message: str
    system_prompt: Optional[str] = Field(default=None, max_length=4000)

    @field_validator("message")
    @classmethod
    def _message_len(cls, v: str) -> str:
        v = v.strip()
        if not 1 <= len(v) <= 4000:
            raise ValueError("message must be 1-4000 characters after trimming")
        return v

    @field_validator("system_prompt")
    @classmethod
    def _blank_prompt_is_none(cls, v):
        if v is not None and not v.strip():
            return None
        return v.strip() if v else v


def create_app(settings: Optional[Settings] = None, slang_store: Optional[slang.SlangStore] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if slang_store is None:
        slang_store = slang.SlangStore(
            DEFAULT_SYSTEM_PROMPT,
            live_path=os.path.join(os.path.dirname(os.path.abspath(settings.database_path)), "slang.live.json"),
            refresh_hours=settings.slang_refresh_hours,
            fetch=slang.make_grok_fetch(settings.xai_api_key, settings.slang_model),
        )
    llm.setup_cache()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.init_db(settings.database_path)
        yield

    app = FastAPI(title="Chief Bestu API", lifespan=lifespan)
    app.state.settings = settings
    app.state.slang_store = slang_store

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*", "X-Simulate-Failure", "Content-Type"],
    )

    # ---------- error handlers: every error is JSON with a "detail" ----------
    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        detail = [
            {"loc": list(e.get("loc", [])), "msg": str(e.get("msg", "invalid input")), "type": e.get("type", "error")}
            for e in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": detail})

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(request: Request, exc: StarletteHTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        log.error("unhandled error: %s", type(exc).__name__)  # no stack trace to the client
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    # ---------- endpoints ----------
    @app.get("/health")
    async def health():
        try:
            ok = await db.ping(settings.database_path)
        except Exception:
            ok = False
        if not ok or not settings.api_keys:
            return JSONResponse(
                status_code=503,
                content={"detail": "not ready", "status": "unavailable", "keys_configured": len(settings.api_keys),
                         "cache_enabled": True},
            )
        return {"status": "ok", "keys_configured": len(settings.api_keys), "cache_enabled": True}

    @app.post("/chat")
    async def chat(
        body: ChatRequest,
        x_simulate_failure: Optional[str] = Header(default=None, alias="X-Simulate-Failure"),
    ):
        started = time.perf_counter()
        path = settings.database_path

        # system prompt: request override > stored for this session > default constant
        stored_prompt = await db.get_session_prompt(path, body.session_id)
        history = await db.get_history(path, body.session_id)

        # New conversation -> make sure today's slang list is (being) refreshed. Non-blocking:
        # this request uses the current list; Grok is never on the chat's critical path.
        if not history:
            slang_store.ensure_fresh()
        system_prompt = body.system_prompt or stored_prompt or slang_store.prompt

        messages = (
            [{"role": "system", "content": system_prompt}]
            + history
            + [{"role": "user", "content": body.message}]
        )

        simulation = llm.parse_simulation(x_simulate_failure, settings.enable_failure_simulation)

        try:
            reply, meta = await llm.chat_with_failover(
                settings.model, messages, settings.api_keys, simulation, settings.request_timeout
            )
        except llm.AllProvidersUnavailable:
            # nothing is written to history on failure
            return JSONResponse(status_code=503, content={"detail": "all providers unavailable"})

        if slang.contains_abuse(reply):  # hard guard on top of the prompt rule
            reply = SAFE_REPLY

        meta["latency_ms"] = max(int((time.perf_counter() - started) * 1000), 0)

        # persist ONLY after success
        await db.save_exchange(path, body.session_id, body.system_prompt, body.message, reply, meta)

        return {"session_id": body.session_id, "reply": reply, "metadata": meta}

    @app.get("/sessions/{session_id}/messages")
    async def session_messages(session_id: str):
        msgs = await db.get_messages_for_api(settings.database_path, session_id)
        if msgs is None:
            return JSONResponse(status_code=404, content={"detail": "session not found"})
        return {"session_id": session_id, "messages": msgs}

    return app


app = create_app()