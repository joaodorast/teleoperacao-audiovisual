"""Trilhas WebRTC que leem dos buffers de captura.

As trilhas não capturam nada: só retiram o item mais recente (vídeo) ou o próximo bloco
(áudio) das filas preenchidas pelas threads de captura — separação entre captura e
transmissão. A compressão (VP8 / Opus) é feita pelo aiortc, em executor.
"""

from __future__ import annotations

import asyncio
import fractions
import logging
from typing import Any, Callable

import av
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack

from ..capture.audio import MicrophoneCapture
from ..capture.video import CameraCapture

log = logging.getLogger(__name__)

VIDEO_CLOCK_RATE = 90_000
VIDEO_TIME_BASE = fractions.Fraction(1, VIDEO_CLOCK_RATE)

VideoClockListener = Callable[[int, float], None]


class CameraTrack(MediaStreamTrack):
    """Vídeo do robô: entrega sempre o quadro mais recente da webcam (RF01)."""

    kind = "video"

    def __init__(self, capture: CameraCapture, on_video_clock: VideoClockListener | None = None) -> None:
        super().__init__()
        self._capture = capture
        self._last_seq = 0
        self._last_pts: int | None = None
        self._epoch_wall_ms: float | None = None
        self._sender: Any = None
        self._sender_packets = 0
        self._rtp_origin: int | None = None
        self._on_video_clock = on_video_clock
        self.frames_sent = 0

    def bind_sender(self, sender: Any) -> None:
        """Associa o ``RTCRtpSender`` para descobrir a origem do timestamp RTP.

        O aiortc soma uma origem aleatória ao pts de cada quadro para formar o timestamp
        RTP, e não a expõe publicamente. Ela é deduzida lendo o último timestamp RTP
        enviado (atributo interno usado nos Sender Reports) logo após o envio do quadro
        anterior. Com ela, o navegador consegue associar cada quadro exibido ao instante
        exato em que foi capturado, permitindo medir a latência de vídeo fim a fim.
        Se o atributo não existir nesta versão do aiortc, a métrica fica indisponível.
        """
        self._sender = sender

    @property
    def rtp_origin(self) -> int | None:
        return self._rtp_origin

    def _probe_rtp_origin(self) -> None:
        if self._sender is None or self._last_pts is None:
            return
        rtp_ts = getattr(self._sender, "_RTCRtpSender__rtp_timestamp", None)
        packets = getattr(self._sender, "_RTCRtpSender__packet_count", None)
        if not isinstance(rtp_ts, int) or not isinstance(packets, int) or packets <= self._sender_packets:
            return
        self._sender_packets = packets
        origin = (rtp_ts - self._last_pts) & 0xFFFFFFFF
        if self._epoch_wall_ms is None:
            return
        # Arredondamentos na conversão de base de tempo produzem variação de ±1 tick
        # (11 µs); só anuncia uma nova origem se ela mudar mais que 1 ms.
        if self._rtp_origin is not None:
            diff = (origin - self._rtp_origin) & 0xFFFFFFFF
            if min(diff, 0x100000000 - diff) <= VIDEO_CLOCK_RATE // 1000:
                return
        self._rtp_origin = origin
        if self._on_video_clock:
            self._on_video_clock(origin, self._epoch_wall_ms)

    async def recv(self) -> av.VideoFrame:
        if self.readyState != "live":
            raise MediaStreamError
        self._probe_rtp_origin()
        seq, captured = await self._capture.slot.get_newer(self._last_seq)
        self._last_seq = seq

        epoch = self._capture.epoch_mono_ns
        pts = (captured.capture_mono_ns - epoch) * VIDEO_CLOCK_RATE // 1_000_000_000
        if self._last_pts is not None and pts <= self._last_pts:
            pts = self._last_pts + 1
        if self._epoch_wall_ms is None:
            self._epoch_wall_ms = (captured.capture_wall_ns - (captured.capture_mono_ns - epoch)) / 1e6
        self._last_pts = pts

        frame = captured.frame
        frame.pts = pts
        frame.time_base = VIDEO_TIME_BASE
        self.frames_sent += 1
        return frame


class MicrophoneTrack(MediaStreamTrack):
    """Áudio do ambiente do robô em blocos de 20 ms (RF02).

    Prioriza continuidade: se o microfone atrasar um bloco, envia silêncio no tempo certo
    em vez de interromper o fluxo; se blocos se acumularem (atraso), descarta os mais
    antigos até restarem poucos.
    """

    kind = "audio"
    MAX_BACKLOG_BLOCKS = 4
    KEEP_BLOCKS = 2

    def __init__(self, capture: MicrophoneCapture) -> None:
        super().__init__()
        self._capture = capture
        self._rate = capture.sample_rate
        self._samples = capture.block_samples
        self._time_base = fractions.Fraction(1, self._rate)
        self._pts = 0
        self._start: float | None = None
        self.silence_blocks = 0
        self.skipped_blocks = 0

    async def recv(self) -> av.AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError
        queue = self._capture.queue
        loop = asyncio.get_running_loop()
        if self._start is None:
            self._start = loop.time()

        backlog = len(queue)
        if backlog > self.MAX_BACKLOG_BLOCKS:
            for _ in range(backlog - self.KEEP_BLOCKS):
                queue.get_nowait()
                self.skipped_blocks += 1

        deadline = self._start + (self._pts + self._samples) / self._rate + 0.02
        block = await queue.get(timeout=max(0.0, deadline - loop.time()))
        if block is None or len(block) != self._samples * 2:
            block = bytes(self._samples * 2)
            self.silence_blocks += 1
            # Mantém o relógio alinhado ao tempo real ao gerar silêncio.
            behind = loop.time() - (self._start + self._pts / self._rate)
            if behind > 0.2:
                self._start = loop.time() - self._pts / self._rate

        frame = av.AudioFrame(format="s16", layout="mono", samples=self._samples)
        frame.planes[0].update(block)
        frame.sample_rate = self._rate
        frame.pts = self._pts
        frame.time_base = self._time_base
        self._pts += self._samples
        return frame


async def consume_remote_audio(track: MediaStreamTrack, sink: Callable[[av.AudioFrame], None]) -> None:
    """Lê a voz do operador recebida por WebRTC e entrega ao alto-falante (RF04)."""
    try:
        while True:
            frame = await track.recv()
            sink(frame)
    except MediaStreamError:
        pass
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Falha ao reproduzir o áudio do operador")
