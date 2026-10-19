"""RF01 — Captura de vídeo da webcam do robô a 5 FPS (OpenCV), em thread dedicada.

Estratégia de baixa latência:

* A thread lê continuamente o dispositivo com ``grab()`` (barato, sem decodificar), o que
  esvazia o buffer interno do driver; apenas no instante de cada quadro programado
  (a cada 1/FPS s) ela chama ``retrieve()``. Assim o quadro publicado é sempre o mais
  recente que a câmera produziu, e não um quadro antigo acumulado no driver.
* O quadro é convertido para ``yuv420p`` aqui mesmo, fora do laço assíncrono.
* O resultado vai para um ``LatestSlot``: se a transmissão atrasar, o quadro anterior é
  descartado em vez de enfileirado.
* Se a câmera não abrir ou parar de responder, a thread reporta "indisponível" e tenta
  reabrir periodicamente (reconexão do dispositivo).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import av
import numpy as np

from ..devices import DeviceStatus, StatusListener
from .buffers import LatestSlot

log = logging.getLogger(__name__)


class VideoSource(Protocol):
    def isOpened(self) -> bool: ...
    def grab(self) -> bool: ...
    def retrieve(self) -> tuple[bool, Any]: ...
    def release(self) -> None: ...


@dataclass(frozen=True)
class CapturedFrame:
    frame: av.VideoFrame
    index: int
    capture_wall_ns: int  # relógio de parede do robô no instante da captura
    capture_mono_ns: int  # relógio monotônico (para o pts)


def open_opencv_camera(source: int | str, width: int, height: int, fps: float) -> VideoSource:
    import cv2

    try:  # as tentativas de reabertura já são registradas pelo próprio serviço
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
    except AttributeError:
        pass
    cap = cv2.VideoCapture(source)
    if cap.isOpened() and isinstance(source, int):
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    return cap


class CameraCapture:
    REOPEN_INTERVAL_S = 2.0
    STALL_TIMEOUT_S = 2.0

    def __init__(
        self,
        source: int | str,
        fps: float,
        width: int,
        height: int,
        opener: Callable[[int | str, int, int, float], VideoSource] = open_opencv_camera,
        on_status: StatusListener | None = None,
    ) -> None:
        self.source = source
        self.fps = fps
        self.width = width
        self.height = height
        self.slot: LatestSlot[CapturedFrame] = LatestSlot()
        self._opener = opener
        self._on_status = on_status
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._status = DeviceStatus("idle")
        self._publish_times: deque[float] = deque(maxlen=32)
        self._frames = 0
        self._epoch_mono_ns = 0
        # Arquivo de vídeo (útil para ensaios sem webcam): lido no ritmo de 5 FPS e
        # reiniciado ao terminar. Dispositivos e fluxos de rede são drenados continuamente.
        self._file_mode = isinstance(source, str) and os.path.isfile(source)

    # ---- ciclo de vida -------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._epoch_mono_ns = time.monotonic_ns()
        self._set_status("starting", "Abrindo a webcam do robô…")
        self._thread = threading.Thread(target=self._run, name="camera-capture", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.warning("Thread de captura de vídeo não encerrou dentro do prazo")
        self._thread = None
        self.slot.clear()
        self._set_status("idle", None)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def epoch_mono_ns(self) -> int:
        return self._epoch_mono_ns

    # ---- estado --------------------------------------------------------
    def status(self) -> DeviceStatus:
        with self._lock:
            s = DeviceStatus(self._status.state, self._status.message, dict(self._status.metrics))
        s.metrics.update(
            configured_fps=self.fps,
            measured_fps=self.measured_fps(),
            frames=self._frames,
            dropped=self.slot.dropped,
            width=self.width,
            height=self.height,
        )
        return s

    def measured_fps(self) -> float | None:
        times = list(self._publish_times)
        if len(times) < 3 or time.monotonic() - times[-1] > 2.0:
            return None
        span = times[-1] - times[0]
        return round((len(times) - 1) / span, 2) if span > 0 else None

    def _set_status(self, state: str, message: str | None) -> None:
        with self._lock:
            changed = (self._status.state, self._status.message) != (state, message)
            self._status = DeviceStatus(state, message)  # type: ignore[arg-type]
        if changed and self._on_status:
            try:
                self._on_status()
            except Exception:  # o ouvinte nunca deve derrubar a captura
                log.exception("Erro no ouvinte de estado da câmera")

    # ---- laço da thread -----------------------------------------------
    def _run(self) -> None:
        period = 1.0 / self.fps
        while not self._stop.is_set():
            cap = None
            try:
                cap = self._opener(self.source, self.width, self.height, self.fps)
                if not cap.isOpened():
                    raise OSError("camera not opened")
            except Exception as exc:
                if self._status.state != "unavailable":
                    log.warning("Webcam indisponível (%s); tentando novamente a cada %.0f s",
                                type(exc).__name__, self.REOPEN_INTERVAL_S)
                if cap is not None:
                    cap.release()
                self._set_status("unavailable", "Verifique se a webcam está conectada ao computador do robô. Nova tentativa automática em instantes.")
                self._stop.wait(self.REOPEN_INTERVAL_S)
                continue

            log.info("Webcam aberta; capturando a %.1f FPS", self.fps)
            try:
                if self._file_mode:
                    self._file_loop(cap, period)
                else:
                    self._capture_loop(cap, period)
            finally:
                cap.release()
            if self._file_mode:
                continue
            if not self._stop.is_set():
                self._set_status("unavailable", "A webcam parou de responder. Reconectando automaticamente…")
                self._stop.wait(self.REOPEN_INTERVAL_S / 2)

    def _capture_loop(self, cap: VideoSource, period: float) -> None:
        next_due = time.monotonic()
        last_ok = time.monotonic()
        while not self._stop.is_set():
            t0 = time.monotonic()
            ok = cap.grab()
            now = time.monotonic()
            if not ok:
                if now - last_ok > self.STALL_TIMEOUT_S:
                    return
                self._stop.wait(0.02)
                continue
            last_ok = now

            if now >= next_due:
                ok, image = cap.retrieve()
                if ok and image is not None:
                    self._publish(image)
                # Programação por prazo: sem acumular atraso se uma leitura demorar.
                next_due += period
                if next_due < now:
                    next_due = now + period
            elif now - t0 < 0.004:
                # Fonte que não bloqueia em grab() (ex.: arquivo): evita laço ocioso.
                self._stop.wait(min(0.01, max(0.0, next_due - now)))

    def _file_loop(self, cap: VideoSource, period: float) -> None:
        next_due = time.monotonic()
        while not self._stop.is_set():
            delay = next_due - time.monotonic()
            if delay > 0 and self._stop.wait(delay):
                return
            if not cap.grab():
                return  # fim do arquivo: reabre do início
            ok, image = cap.retrieve()
            if ok and image is not None:
                self._publish(image)
            next_due = max(next_due + period, time.monotonic())

    def _publish(self, image: np.ndarray) -> None:
        wall_ns = time.time_ns()
        mono_ns = time.monotonic_ns()
        if image.ndim == 2:
            import cv2

            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        h, w = image.shape[:2]
        if (w, h) != (self.width, self.height):
            import cv2

            image = cv2.resize(image, (self.width, self.height), interpolation=cv2.INTER_AREA)
        frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(image), format="bgr24")
        frame = frame.reformat(format="yuv420p")
        self._frames += 1
        self._publish_times.append(time.monotonic())
        self.slot.put(CapturedFrame(frame, self._frames, wall_ns, mono_ns))
        if self._status.state != "active":
            self._set_status("active", None)
