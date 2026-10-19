"""Conexão WebRTC entre o robô e o navegador do operador.

O navegador cria a oferta (vídeo ``recvonly`` + áudio ``sendrecv``); o robô responde,
anexando a trilha da webcam e a do microfone, e recebe a voz do operador, que é entregue
ao alto-falante. A sinalização é não-incremental: a oferta já chega com todos os
candidatos ICE (adequado para rede local).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from aiortc import RTCConfiguration, RTCPeerConnection, RTCRtpSender, RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack

from ..capture.audio import MicrophoneCapture
from ..capture.video import CameraCapture
from ..playback.speaker import SpeakerPlayback
from .tracks import CameraTrack, MicrophoneTrack, VideoClockListener, consume_remote_audio

log = logging.getLogger(__name__)


class NegotiationError(Exception):
    """Oferta SDP inválida ou incompatível."""


StateListener = Callable[[str], None]


class TeleopPeer:
    def __init__(
        self,
        camera: CameraCapture | None,
        microphone: MicrophoneCapture | None,
        speaker: SpeakerPlayback | None,
        video_codec: str = "vp8",
        on_state: StateListener | None = None,
        on_video_clock: VideoClockListener | None = None,
    ) -> None:
        self._camera = camera
        self._microphone = microphone
        self._speaker = speaker
        self._video_codec = video_codec
        self._on_state = on_state
        self._on_video_clock = on_video_clock
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.video_track: CameraTrack | None = None
        self.audio_track: MicrophoneTrack | None = None
        self._remote_audio_task: asyncio.Task[None] | None = None
        self._closed = False

        @self.pc.on("connectionstatechange")
        def _on_conn_state() -> None:
            state = self.pc.connectionState
            log.info("WebRTC: estado da conexão = %s", state)
            if self._on_state:
                self._on_state(state)

        @self.pc.on("track")
        def _on_track(track: MediaStreamTrack) -> None:
            if track.kind == "audio" and self._speaker is not None and self._remote_audio_task is None:
                log.info("Recebendo a voz do operador")
                self._remote_audio_task = asyncio.ensure_future(
                    consume_remote_audio(track, self._speaker.feed)
                )

    @property
    def connection_state(self) -> str:
        return self.pc.connectionState

    async def answer(self, offer_sdp: str) -> str:
        if "m=video" not in offer_sdp or "m=audio" not in offer_sdp:
            raise NegotiationError("A oferta precisa conter vídeo e áudio")
        try:
            await self.pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
        except Exception as exc:
            raise NegotiationError("Oferta SDP inválida") from exc

        for transceiver in self.pc.getTransceivers():
            if transceiver.kind == "video":
                self._prefer_video_codec(transceiver)
                if self._camera is not None and self.video_track is None:
                    self.video_track = CameraTrack(self._camera, self._on_video_clock)
                    sender = self.pc.addTrack(self.video_track)
                    self.video_track.bind_sender(sender)
            elif transceiver.kind == "audio":
                if self._microphone is not None and self.audio_track is None:
                    self.audio_track = MicrophoneTrack(self._microphone)
                    self.pc.addTrack(self.audio_track)

        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        return self.pc.localDescription.sdp

    def _prefer_video_codec(self, transceiver: Any) -> None:
        wanted = "video/H264" if self._video_codec == "h264" else "video/VP8"
        codecs = RTCRtpSender.getCapabilities("video").codecs
        preferred = [c for c in codecs if c.mimeType == wanted]
        rest = [c for c in codecs if c.mimeType != wanted]
        try:
            transceiver.setCodecPreferences(preferred + rest)
        except Exception:  # se o navegador não ofereceu o codec, usa a negociação padrão
            log.debug("Preferência de codec de vídeo não aplicada")

    async def stats(self) -> dict[str, Any]:
        """Estatísticas do lado do robô (inclui relatórios RTCP enviados pelo navegador)."""
        if self._closed:
            return {}
        try:
            report = await self.pc.getStats()
        except Exception:
            return {}
        out: dict[str, Any] = {}
        for stat in report.values():
            kind = getattr(stat, "kind", None)
            if stat.type == "remote-inbound-rtp":
                # Como o navegador está recebendo o que o robô envia (RTCP Receiver Report).
                out[f"{kind}_out"] = {
                    "packets_lost": stat.packetsLost,
                    "fraction_lost": round(stat.fractionLost, 4),
                    "rtt_ms": round(stat.roundTripTime * 1000, 1) if stat.roundTripTime else None,
                    "jitter": stat.jitter,
                }
            elif stat.type == "outbound-rtp":
                out.setdefault(f"{kind}_sent", {})["packets_sent"] = stat.packetsSent
            elif stat.type == "inbound-rtp" and kind == "audio":
                total = stat.packetsReceived + max(stat.packetsLost, 0)
                out["operator_audio_in"] = {
                    "packets_received": stat.packetsReceived,
                    "packets_lost": stat.packetsLost,
                    "loss_ratio": round(max(stat.packetsLost, 0) / total, 4) if total else None,
                    # jitter do aiortc está em unidades do relógio RTP (48 kHz no Opus)
                    "jitter_ms": round(stat.jitter / 48, 1),
                }
        return out

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._remote_audio_task:
            self._remote_audio_task.cancel()
            try:
                await self._remote_audio_task
            except (asyncio.CancelledError, Exception):
                pass
        for track in (self.video_track, self.audio_track):
            if track:
                track.stop()
        try:
            await self.pc.close()
        except Exception:
            log.exception("Erro ao fechar a conexão WebRTC")
