"""RF02 — Captura contínua do microfone do robô (PortAudio via ``sounddevice``).

O callback do PortAudio roda em thread própria e apenas copia blocos PCM de 20 ms para
uma fila limitada (``BoundedAudioQueue``). Uma thread supervisora detecta a perda do
dispositivo e tenta reabri-lo.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Protocol

import numpy as np

from ..devices import DeviceStatus, StatusListener, audio_backend_error
from .buffers import BoundedAudioQueue

log = logging.getLogger(__name__)

BLOCK_MS = 20


class AudioStream(Protocol):
    active: bool

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...


InputCallback = Callable[[np.ndarray], None]
InputStreamFactory = Callable[[InputCallback, int, int, Any], AudioStream]


def sounddevice_input_stream(callback: InputCallback, sample_rate: int, blocksize: int, device: Any) -> AudioStream:
    import sounddevice as sd

    def _cb(indata, frames, time_info, status):  # noqa: ANN001 - assinatura do PortAudio
        callback(indata)

    return sd.InputStream(
        samplerate=sample_rate,
        blocksize=blocksize,
        channels=1,
        dtype="int16",
        latency="low",
        device=device,
        callback=_cb,
    )


class MicrophoneCapture:
    REOPEN_INTERVAL_S = 2.0
    STALL_TIMEOUT_S = 2.0

    def __init__(
        self,
        sample_rate: int = 48_000,
        device: Any = None,
        stream_factory: InputStreamFactory | None = None,
        on_status: StatusListener | None = None,
        max_blocks: int = 8,
    ) -> None:
        self.sample_rate = sample_rate
        self.block_samples = sample_rate * BLOCK_MS // 1000
        self.device = device
        self.queue = BoundedAudioQueue(max_blocks)
        self._factory = stream_factory
        self._on_status = on_status
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._status = DeviceStatus("idle")
        self._last_block = 0.0
        self._level = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._set_status("starting", "Abrindo o microfone do robô…")
        self._thread = threading.Thread(target=self._run, name="mic-capture", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
        self._thread = None
        self.queue.clear()
        self._set_status("idle", None)

    def status(self) -> DeviceStatus:
        with self._lock:
            s = DeviceStatus(self._status.state, self._status.message, dict(self._status.metrics))
        s.metrics.update(level=round(self._level, 3), dropped_blocks=self.queue.dropped)
        return s

    def _set_status(self, state: str, message: str | None) -> None:
        with self._lock:
            changed = (self._status.state, self._status.message) != (state, message)
            self._status = DeviceStatus(state, message)  # type: ignore[arg-type]
        if changed and self._on_status:
            try:
                self._on_status()
            except Exception:
                log.exception("Erro no ouvinte de estado do microfone")

    def _on_block(self, indata: np.ndarray) -> None:
        block = np.ascontiguousarray(indata.reshape(-1), dtype=np.int16)
        self.queue.put(block.tobytes())
        self._last_block = time.monotonic()
        peak = float(np.abs(block).max(initial=0)) / 32768.0
        self._level = max(peak, self._level * 0.85)

    def _run(self) -> None:
        factory = self._factory
        if factory is None:
            err = audio_backend_error()
            if err:
                self._set_status("unavailable", "Microfone do robô indisponível: " + err)
                return
            factory = sounddevice_input_stream

        while not self._stop.is_set():
            stream = None
            try:
                stream = factory(self._on_block, self.sample_rate, self.block_samples, self.device)
                stream.start()
            except Exception as exc:
                if self._status.state != "unavailable":
                    log.warning("Microfone do robô indisponível (%s)", type(exc).__name__)
                self._close(stream)
                self._set_status("unavailable", "Microfone do robô indisponível. Nova tentativa em instantes.")
                self._stop.wait(self.REOPEN_INTERVAL_S)
                continue

            self._last_block = time.monotonic()
            self._set_status("active", None)
            while not self._stop.wait(0.25):
                if not getattr(stream, "active", True) or time.monotonic() - self._last_block > self.STALL_TIMEOUT_S:
                    log.warning("Microfone do robô parou de enviar áudio; reabrindo")
                    self._set_status("unavailable", "O microfone do robô parou de responder. Reconectando…")
                    break
            self._close(stream)

    @staticmethod
    def _close(stream: AudioStream | None) -> None:
        if stream is None:
            return
        for op in (stream.stop, stream.close):
            try:
                op()
            except Exception:
                pass
