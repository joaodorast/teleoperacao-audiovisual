"""Aplicação FastAPI do computador do robô: autenticação, sinalização e interface web."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .auth import COOKIE_NAME, Authenticator, LoginRateLimiter
from .config import Settings
from .session import DeviceFactories, SessionManager
from .signaling import build_router

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "media-src 'self' blob:; connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
}


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


def create_app(settings: Settings, factories: DeviceFactories | None = None) -> FastAPI:
    auth = Authenticator(
        settings.username,
        settings.secret_key,
        password_hash=settings.password_hash,
        password_plain=settings.password_plain,
        token_ttl_s=settings.token_ttl_s,
    )
    limiter = LoginRateLimiter()
    sessions = SessionManager(settings, factories)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await sessions.startup()
        try:
            yield
        finally:
            await sessions.shutdown()

    app = FastAPI(title="Teleoperação Audiovisual", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sessions = sessions
    app.state.auth = auth

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # noqa: ANN001
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def client_key(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def is_secure(request: Request) -> bool:
        return settings.tls_enabled or request.url.scheme == "https"

    @app.post("/api/login")
    async def login(body: LoginBody, request: Request, response: Response) -> dict[str, object]:
        key = client_key(request)
        wait = limiter.blocked_for(key)
        if wait > 0:
            raise HTTPException(
                status_code=429,
                detail=f"Muitas tentativas. Aguarde {int(wait) + 1} s e tente novamente.",
            )
        if not auth.check_credentials(body.username, body.password):
            limiter.record_failure(key)
            log.info("Falha de login a partir de %s", key)
            raise HTTPException(status_code=401, detail="Usuário ou senha inválidos.")
        limiter.reset(key)
        response.set_cookie(
            COOKIE_NAME,
            auth.issue_token(),
            max_age=auth.token_ttl_s,
            httponly=True,
            secure=is_secure(request),
            samesite="strict",
            path="/",
        )
        log.info("Operador autenticado a partir de %s", key)
        return {"authenticated": True, "username": settings.username}

    @app.post("/api/logout")
    async def logout(request: Request, response: Response) -> dict[str, bool]:
        claims = auth.verify_token(request.cookies.get(COOKIE_NAME))
        if claims:
            auth.revoke(claims)
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"authenticated": False}

    @app.get("/api/me")
    async def me(request: Request) -> JSONResponse:
        claims = auth.verify_token(request.cookies.get(COOKIE_NAME))
        if claims is None:
            return JSONResponse({"authenticated": False})
        return JSONResponse({"authenticated": True, "username": claims.sub, "expires_at": claims.exp})

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(build_router(auth, sessions, settings.allowed_origins))

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    return app
