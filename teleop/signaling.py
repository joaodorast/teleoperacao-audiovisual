"""Canal WebSocket de sinalização e controle (não transporta mídia).

Mensagens do navegador (validadas com pydantic):

``ping``            ``{id, t}`` — medição do tempo de ida e volta da sinalização e
                    sincronização de relógio para a medição de latência de vídeo.
``session.start``   ``{sdp}`` — oferta WebRTC; inicia a sessão ou renegocia a conexão.
``session.resume``  ``{session_id}`` — retoma a sessão após queda da sinalização.
``session.stop``    encerra a sessão de forma controlada.

Mensagens do robô: ``hello``, ``pong``, ``session.answer``, ``session.state``,
``transport.state``, ``devices``, ``robot.stats``, ``video.clock`` e ``error``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from typing import Annotated, Any, Literal, Union

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from .auth import COOKIE_NAME, Authenticator
from .session import SessionError, SessionManager

log = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 64 * 1024
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN_ORIGIN = 4403


class _Msg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PingMsg(_Msg):
    type: Literal["ping"]
    id: int = Field(ge=0)
    t: float


class StartMsg(_Msg):
    type: Literal["session.start"]
    sdp: str = Field(min_length=10, max_length=MAX_MESSAGE_BYTES)


class ResumeMsg(_Msg):
    type: Literal["session.resume"]
    session_id: str = Field(min_length=1, max_length=64)


class StopMsg(_Msg):
    type: Literal["session.stop"]


ClientMessage = TypeAdapter(
    Annotated[Union[PingMsg, StartMsg, ResumeMsg, StopMsg], Field(discriminator="type")]
)


def origin_allowed(origin: str | None, host: str | None, allowed: tuple[str, ...]) -> bool:
    if not origin:
        return False  # navegadores sempre enviam Origin no WebSocket
    origin = origin.rstrip("/")
    if origin in allowed:
        return True
    if not host:
        return False
    return origin in (f"http://{host}", f"https://{host}")


class WsClient:
    """Fila de saída por conexão: o laço da sessão nunca espera pela rede."""

    def __init__(self, ws: WebSocket) -> None:
        self.id = secrets.token_hex(6)
        self._ws = ws
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=256)
        self._connected = True

    @property
    def connected(self) -> bool:
        return self._connected

    def post(self, message: dict[str, Any]) -> None:
        if not self._connected:
            return
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            log.warning("Fila de saída cheia; mensagem descartada (%s)", message.get("type"))

    async def writer(self) -> None:
        while True:
            msg = await self._queue.get()
            if msg is None:
                return
            await self._ws.send_text(json.dumps(msg, separators=(",", ":")))

    def close(self) -> None:
        self._connected = False
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass


def build_router(auth: Authenticator, sessions: SessionManager, allowed_origins: tuple[str, ...]) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws")
    async def signaling(ws: WebSocket) -> None:
        if not origin_allowed(ws.headers.get("origin"), ws.headers.get("host"), allowed_origins):
            await ws.close(code=CLOSE_FORBIDDEN_ORIGIN)
            return
        claims = auth.verify_token(ws.cookies.get(COOKIE_NAME))
        await ws.accept()
        if claims is None:
            await ws.close(code=CLOSE_UNAUTHORIZED)
            return

        client = WsClient(ws)
        writer = asyncio.create_task(client.writer())
        s = sessions.settings
        client.post(
            {
                "type": "hello",
                "server_time": time.time() * 1000,
                "config": {
                    "video_fps": s.video_fps,
                    "video_width": s.video_width,
                    "video_height": s.video_height,
                    "video_codec": s.video_codec,
                    "latency_target_ms": 300,
                    "session_grace_s": s.session_grace_s,
                },
                "session": sessions.snapshot(),
                "session_owned": sessions.owner is not None and sessions.owner.connected,
                "devices": sessions.devices(),
            }
        )
        try:
            while True:
                raw = await ws.receive_text()
                if len(raw) > MAX_MESSAGE_BYTES:
                    client.post({"type": "error", "code": "too_large", "message": "Mensagem muito grande."})
                    continue
                if auth.verify_token(ws.cookies.get(COOKIE_NAME)) is None:
                    await ws.close(code=CLOSE_UNAUTHORIZED)
                    break
                try:
                    msg = ClientMessage.validate_json(raw)
                except ValidationError:
                    client.post({"type": "error", "code": "invalid_message", "message": "Mensagem inválida."})
                    continue
                await _handle(msg, client, sessions)
        except WebSocketDisconnect:
            pass
        except RuntimeError:
            pass  # conexão fechada durante o envio
        finally:
            client.close()
            sessions.client_disconnected(client)
            writer.cancel()
            try:
                await writer
            except (asyncio.CancelledError, Exception):
                pass

    return router


async def _handle(msg: Any, client: WsClient, sessions: SessionManager) -> None:
    if isinstance(msg, PingMsg):
        client.post({"type": "pong", "id": msg.id, "t": msg.t, "server_time": time.time() * 1000})
    elif isinstance(msg, StartMsg):
        try:
            answer = await sessions.start(client, msg.sdp)
        except SessionError as exc:
            client.post({"type": "error", "code": exc.code, "message": exc.message})
            return
        client.post({"type": "session.answer", "sdp": answer, "session_id": sessions.session_id})
    elif isinstance(msg, ResumeMsg):
        if not sessions.resume(client, msg.session_id):
            client.post({"type": "session.state", **sessions.snapshot(), "reason": "resume_rejected"})
    elif isinstance(msg, StopMsg):
        try:
            await sessions.stop(client)
        except SessionError as exc:
            client.post({"type": "error", "code": exc.code, "message": exc.message})
