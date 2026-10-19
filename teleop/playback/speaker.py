"""RF04 — Reprodução da voz do operador no alto-falante conectado ao robô.

Os quadros Opus já decodificados pelo aiortc chegam ao laço assíncrono, são convertidos
para PCM mono 16 bits e entram num buffer de jitter pequeno e limitado. O callback do
PortAudio (thread própria) consome esse buffer:

* pré-buffer de ~40 ms antes de começar a tocar (evita estalos);
* se faltar áudio, toca silêncio (a fala recomeça sem travar o dispositivo);
* se o buffer passar de ~160 ms, os dados mais antigos são descartados para que o atraso
  não aumente indefinidamente.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Protocol

import av
import numpy as np

from ..devices import DeviceStatus, StatusListener, audio_backend_error

log = logging.getLogger(__name__)


class OutputStream(Protocol):
    active: bool

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...


OutputCallback = Callable[[int], bytes]
OutputStreamFactory = Callable[[OutputCallback, int, int, Any], OutputStream]


def sounddevice_output_stream(pull: OutputCallback, sample_rate: int, blocksize: int, device: Any) -> OutputStream:
    import sounddevice as sd

    def _cb(outdata, frames, time_info, status):  # noqa: ANN001
        data = pull(frames)
        outdata[:] = np.frombuffer(data, dtype=np.int16).reshape(-1, 1)

    return sd.OutputStream(
        samplerate=sample_rate,
        blocksize=blocksize,
        channels=1,
        dtype="int16",
        latency="low",
        device=device,
        callback=_cb,
    )


class JitterBuffer:
    """Buffer PCM s16 mono, seguro entre threads, com limite de atraso."""

    def __init__(self, sample_rate: int, prebuffer_ms: int = 40, max_ms: int = 160, target_ms: int = 60) -> None:
        self._lock = threading.Lock()
        self._data = bytearray()
        bps = sample_rate * 2 // 1000  # bytes por ms
        self._prebuffer = prebuffer_ms * bps
        self._max = max_ms * bps
        self._target = target_ms * bps
        self._playing = False
        self.underruns = 0
        self.dropped_bytes = 0

    def write(self, pcm: bytes) -> None:
        with self._lock:
            self._data += pcm
            if len(self._data) > self._max:
                excess = len(self._data) - self._target
                excess -= excess % 2
                del self._data[:excess]
                self.dropped_bytes += excess

    def read(self, n_samples: int) -> bytes:
        n = n_samples * 2
        with self._lock:
            if not self._playing:
                if len(self._data) < self._prebuffer:
                    return bytes(n)
                self._playing = True
            if len(self._data) >= n:
                out = bytes(self._data[:n])
                del self._data[:n]
                return out
            # Falta de dados: entrega o que houver + silêncio e volta ao pré-buffer.
            out = bytes(self._data) + bytes(n - len(self._data))
            self._data.clear()
            self._playing = False
            self.underruns += 1
            return out

    def buffered_ms(self, sample_rate: int) -> float:
        with self._lock:
            return len(self._data) / 2 / sample_rate * 1000

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._playing = False


class SpeakerPlayback:
    REOPEN_INTERVAL_S = 2.0

    def __init__(
        self,
        sample_rate: int = 48_000,
        device: Any = None,
        stream_factory: OutputStreamFactory | None = None,
        on_status: StatusListener | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.device = device
        self.buffer = JitterBuffer(sample_rate)
        self._factory = stream_factory
        self._on_status = on_status
        self._resampler: av.AudioResampler | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._status = DeviceStatus("idle")
        self._last_voice = 0.0
        self._level = 0.0
        self.frames_received = 0

    # ---- ciclo de vida -------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._set_status("starting", "Abrindo o alto-falante do robô…")
        self._thread = threading.Thread(target=self._run, name="speaker-playback", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
        self._thread = None
        self.buffer.clear()
        self._resampler = None
        self._set_status("idle", None)

    def status(self) -> DeviceStatus:
        with self._lock:
            s = DeviceStatus(self._status.state, self._status.message, dict(self._status.metrics))
        s.metrics.update(
            receiving_voice=time.monotonic() - self._last_voice < 0.5,
            level=round(self._level, 3),
            buffered_ms=round(self.buffer.buffered_ms(self.sample_rate), 1),
            underruns=self.buffer.underruns,
        )
        return s

    # ---- entrada vinda do WebRTC ---------------------------------------
    def feed(self, frame: av.AudioFrame) -> None:
        if self._resampler is None:
            self._resampler = av.AudioResampler(format="s16", layout="mono", rate=self.sample_rate)
        for out in self._resampler.resample(frame):
            pcm = out.to_ndarray().reshape(-1).astype(np.int16, copy=False)
            peak = float(np.abs(pcm).max(initial=0)) / 32768.0
            self._level = max(peak, self._level * 0.8)
            if peak > 0.01:
                self._last_voice = time.monotonic()
            if self._status.state == "active":
                self.buffer.write(pcm.tobytes())
        self.frames_received += 1

    # ---- thread do dispositivo -----------------------------------------
    def _set_status(self, state: str, message: str | None) -> None:
        with self._lock:
            changed = (self._status.state, self._status.message) != (state, message)
            self._status = DeviceStatus(state, message)  # type: ignore[arg-type]
        if changed and self._on_status:
            try:
                self._on_status()
            except Exception:
                log.exception("Erro no ouvinte de estado do alto-falante")

    def _run(self) -> None:
        factory = self._factory
        if factory is None:
            err = audio_backend_error()
            if err:
                self._set_status("unavailable", "Alto-falante do robô indisponível: " + err)
                return
            factory = sounddevice_output_stream

        blocksize = self.sample_rate // 100  # 10 ms
        while not self._stop.is_set():
            stream = None
            try:
                stream = factory(self.buffer.read, self.sample_rate, blocksize, self.device)
                stream.start()
            except Exception as exc:
                if self._status.state != "unavailable":
                    log.warning("Alto-falante do robô indisponível (%s)", type(exc).__name__)
                self._close(stream)
                self._set_status("unavailable", "Alto-falante do robô indisponível. Nova tentativa em instantes.")
                self._stop.wait(self.REOPEN_INTERVAL_S)
                continue
            self._set_status("active", None)
            while not self._stop.wait(0.25):
                if not getattr(stream, "active", True):
                    self._set_status("unavailable", "O alto-falante do robô parou de responder. Reconectando…")
                    break
            self._close(stream)
            self.buffer.clear()

    @staticmethod
    def _close(stream: OutputStream | None) -> None:
        if stream is None:
            return
        for op in (stream.stop, stream.close):
            try:
                op()
            except Exception:
                pass
