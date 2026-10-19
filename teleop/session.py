"""RF06 — Controle da sessão de teleoperação.

Há no máximo uma sessão por robô. A sessão possui:

* os dispositivos do robô (webcam, microfone, alto-falante), abertos ao iniciar e
  **liberados ao encerrar**;
* a conexão WebRTC atual — que pode ser renegociada sem fechar os dispositivos quando
  a rede falha (reconexão);
* o cliente WebSocket "dono" da sessão. Se ele cair, a sessão fica órfã por um período de
  tolerância (``session_grace_s``) para que o navegador se reconecte e a retome; esgotado
  o prazo, a sessão é encerrada e os recursos liberados.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .capture.audio import MicrophoneCapture
from .capture.video import CameraCapture
from .config import Settings
from .devices import StatusListener
from .playback.speaker import SpeakerPlayback
from .transport.peer import NegotiationError, TeleopPeer

log = logging.getLogger(__name__)


class Client(Protocol):
    id: str

    def post(self, message: dict[str, Any]) -> None: ...

    @property
    def connected(self) -> bool: ...


class SessionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class DeviceFactories:
    camera: Callable[[StatusListener], CameraCapture]
    microphone: Callable[[StatusListener], MicrophoneCapture]
    speaker: Callable[[StatusListener], SpeakerPlayback]

    @classmethod
    def from_settings(cls, s: Settings) -> "DeviceFactories":
        return cls(
            camera=lambda cb: CameraCapture(s.camera, s.video_fps, s.video_width, s.video_height, on_status=cb),
            microphone=lambda cb: MicrophoneCapture(s.audio_sample_rate, s.audio_input_device, on_status=cb),
            speaker=lambda cb: SpeakerPlayback(s.audio_sample_rate, s.audio_output_device, on_status=cb),
        )


class SessionManager:
    STATS_INTERVAL_S = 1.0

    def __init__(self, settings: Settings, factories: DeviceFactories | None = None) -> None:
        self.settings = settings
        self.factories = factories or DeviceFactories.from_settings(settings)
        self.state: str = "idle"  # idle | starting | active | stopping
        self.session_id: str | None = None
        self.started_at_ms: int | None = None
        self.owner: Client | None = None
        self.peer: TeleopPeer | None = None
        self.camera: CameraCapture | None = None
        self.microphone: MicrophoneCapture | None = None
        self.speaker: SpeakerPlayback | None = None
        self.transport_state = "new"
        self._orphan_since: float | None = None
        self._transport_bad_since: float | None = None
        self._video_clock: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._watchdog: asyncio.Task[None] | None = None
        self._devices_dirty = False

    # ---- ciclo de vida do serviço -------------------------------------
    async def startup(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._watchdog = asyncio.create_task(self._watchdog_loop(), name="session-watchdog")

    async def shutdown(self) -> None:
        if self._watchdog:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except asyncio.CancelledError:
                pass
        await self._teardown("server_shutdown")

    # ---- snapshot para a interface ------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "session_id": self.session_id,
            "started_at_ms": self.started_at_ms,
            "transport": self.transport_state,
        }

    def devices(self) -> dict[str, Any]:
        def st(dev: Any) -> dict[str, Any]:
            return dev.status().to_dict() if dev else {"state": "idle", "message": None, "metrics": {}}

        cam = st(self.camera)
        cam["metrics"].setdefault("configured_fps", self.settings.video_fps)
        return {"camera": cam, "microphone": st(self.microphone), "speaker": st(self.speaker)}

    def _post(self, message: dict[str, Any]) -> None:
        if self.owner is not None and self.owner.connected:
            self.owner.post(message)

    def _post_state(self, reason: str | None = None) -> None:
        self._post({"type": "session.state", **self.snapshot(), "reason": reason})

    def _device_status_changed(self) -> None:
        """Chamado pelas threads dos dispositivos; repassa ao laço assíncrono."""
        loop = self._loop
        if loop is None or self._devices_dirty:
            return
        self._devices_dirty = True

        def flush() -> None:
            self._devices_dirty = False
            self._post({"type": "devices", **self.devices()})

        try:
            loop.call_soon_threadsafe(flush)
        except RuntimeError:
            pass

    def _on_video_clock(self, rtp_origin: int, epoch_ms: float) -> None:
        self._video_clock = {"type": "video.clock", "rtp_origin": rtp_origin, "epoch_ms": epoch_ms}
        self._post(self._video_clock)

    def _on_transport_state(self, peer: TeleopPeer, state: str) -> None:
        if peer is not self.peer:
            return
        self.transport_state = state
        now = time.monotonic()
        if state == "connected":
            self._transport_bad_since = None
        elif self._transport_bad_since is None:
            self._transport_bad_since = now
        self._post({"type": "transport.state", "state": state})

    # ---- operações -----------------------------------------------------
    async def start(self, client: Client, offer_sdp: str) -> str:
        """Inicia a sessão ou renegocia a conexão WebRTC da sessão existente."""
        async with self._lock:
            if self.state in ("starting", "stopping"):
                raise SessionError("busy", "Operação em andamento. Aguarde.")
            if self.state == "active" and self.owner is not client:
                owner_alive = self.owner is not None and self.owner.connected
                if owner_alive:
                    raise SessionError("session_in_use", "A sessão já está ativa em outra janela.")
                log.info("Sessão órfã assumida por nova conexão do operador")
                self.owner = client
                self._orphan_since = None

            new_session = self.state == "idle"
            if new_session:
                self.state = "starting"
                self.owner = client
                self.session_id = secrets.token_urlsafe(12)
                self._post_state()
                self._open_devices()

            old_peer = self.peer
            peer = TeleopPeer(
                self.camera,
                self.microphone,
                self.speaker,
                video_codec=self.settings.video_codec,
                on_video_clock=self._on_video_clock,
            )
            peer._on_state = lambda st, p=peer: self._on_transport_state(p, st)
            self.peer = peer
            self._video_clock = None
            self.transport_state = "new"
            self._transport_bad_since = time.monotonic()
            if old_peer:
                await old_peer.close()
            try:
                answer = await peer.answer(offer_sdp)
            except NegotiationError as exc:
                await peer.close()
                self.peer = None
                if new_session:
                    await self._teardown_locked("negotiation_failed")
                raise SessionError("negotiation_failed", "Não foi possível negociar a conexão de mídia.") from exc

            if new_session:
                self.started_at_ms = int(time.time() * 1000)
                self.state = "active"
                log.info("Sessão %s iniciada", self.session_id)
            self._post_state("renegotiated" if not new_session else "started")
            self._post({"type": "devices", **self.devices()})
            return answer

    async def stop(self, client: Client, reason: str = "operator") -> None:
        async with self._lock:
            if self.state == "idle":
                return
            if self.owner is not client:
                raise SessionError("not_owner", "Esta janela não controla a sessão ativa.")
            await self._teardown_locked(reason)

    def resume(self, client: Client, session_id: str) -> bool:
        if self.state != "active" or not session_id or session_id != self.session_id:
            return False
        if self.owner is not None and self.owner is not client and self.owner.connected:
            return False
        self.owner = client
        self._orphan_since = None
        log.info("Sessão %s retomada após reconexão da sinalização", self.session_id)
        self._post_state("resumed")
        self._post({"type": "transport.state", "state": self.transport_state})
        self._post({"type": "devices", **self.devices()})
        if self._video_clock:
            self._post(self._video_clock)
        return True

    def client_disconnected(self, client: Client) -> None:
        if self.owner is client and self.state == "active":
            self._orphan_since = time.monotonic()
            log.info("Sinalização do operador caiu; aguardando reconexão por %.0f s", self.settings.session_grace_s)

    # ---- internos ------------------------------------------------------
    def _open_devices(self) -> None:
        cb = self._device_status_changed
        self.camera = self.factories.camera(cb)
        self.microphone = self.factories.microphone(cb)
        self.speaker = self.factories.speaker(cb)
        for dev in (self.camera, self.microphone, self.speaker):
            dev.start()

    async def _teardown(self, reason: str) -> None:
        async with self._lock:
            await self._teardown_locked(reason)

    async def _teardown_locked(self, reason: str, notify: bool = True) -> None:
        if self.state == "idle" and self.peer is None and self.camera is None:
            return
        self.state = "stopping"
        if notify:
            self._post_state(reason)
        peer, self.peer = self.peer, None
        if peer:
            await peer.close()
        devices = [d for d in (self.camera, self.microphone, self.speaker) if d]
        self.camera = self.microphone = self.speaker = None
        # join() das threads é bloqueante: executado fora do laço assíncrono.
        await asyncio.gather(*(asyncio.to_thread(d.stop) for d in devices))
        log.info("Sessão %s encerrada (%s); dispositivos liberados", self.session_id, reason)
        self.state = "idle"
        self.session_id = None
        self.started_at_ms = None
        self.transport_state = "new"
        self._orphan_since = None
        self._transport_bad_since = None
        self._video_clock = None
        if notify:
            self._post_state(reason)
            self._post({"type": "devices", **self.devices()})

    async def _watchdog_loop(self) -> None:
        grace = self.settings.session_grace_s
        while True:
            await asyncio.sleep(self.STATS_INTERVAL_S)
            try:
                if self.state != "active":
                    continue
                now = time.monotonic()
                if self._orphan_since is not None and now - self._orphan_since > grace:
                    await self._teardown("signaling_lost")
                    continue
                if self._transport_bad_since is not None and now - self._transport_bad_since > grace:
                    await self._teardown("connection_lost")
                    continue
                if self.owner is not None and self.owner.connected and self.peer is not None:
                    stats = await self.peer.stats()
                    self._post({"type": "robot.stats", "stats": stats, **{"devices": self.devices()}})
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Erro no supervisor de sessão")
