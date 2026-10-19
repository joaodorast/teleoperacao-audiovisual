"""Dublês de dispositivos para testes sem hardware.

Usados SOMENTE pelos testes: a aplicação real sempre usa a webcam (OpenCV) e o PortAudio.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

import numpy as np

from teleop.capture.audio import MicrophoneCapture
from teleop.capture.video import CameraCapture
from teleop.playback.speaker import SpeakerPlayback
from teleop.session import DeviceFactories


class SyntheticCamera:
    """Imita um driver de webcam a ~30 FPS: ``grab()`` bloqueia até o próximo quadro."""

    def __init__(self, width: int = 320, height: int = 240, fps: float = 30.0, fail_open: bool = False) -> None:
        self.width, self.height, self.period = width, height, 1.0 / fps
        self._opened = not fail_open
        self._n = 0
        self._next = time.monotonic()
        self.released = False

    def isOpened(self) -> bool:
        return self._opened

    def grab(self) -> bool:
        delay = self._next - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._next = max(self._next + self.period, time.monotonic())
        self._n += 1
        return True

    def retrieve(self) -> tuple[bool, Any]:
        img = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        img[:, : (self._n * 4) % self.width] = (180, 120, 230)
        return True, img

    def release(self) -> None:
        self.released = True
        self._opened = False


class _ThreadStream:
    def __init__(self, fn: Callable[[], None], period: float) -> None:
        self._fn, self._period = fn, period
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self.active = False
        self.closed = False

    def start(self) -> None:
        self.active = True
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self) -> None:
        nxt = time.monotonic()
        while not self._stop.is_set():
            self._fn()
            nxt += self._period
            self._stop.wait(max(0.0, nxt - time.monotonic()))

    def stop(self) -> None:
        self._stop.set()
        if self._t:
            self._t.join(1)
        self.active = False

    def close(self) -> None:
        self.closed = True


def sine_input_factory(freq: float = 440.0):
    streams: list[_ThreadStream] = []

    def factory(callback, sample_rate, blocksize, device):  # noqa: ANN001
        phase = [0]

        def tick() -> None:
            n = np.arange(phase[0], phase[0] + blocksize)
            phase[0] += blocksize
            data = (np.sin(2 * np.pi * freq * n / sample_rate) * 8000).astype(np.int16).reshape(-1, 1)
            callback(data)

        s = _ThreadStream(tick, blocksize / sample_rate)
        streams.append(s)
        return s

    factory.streams = streams  # type: ignore[attr-defined]
    return factory


def recording_output_factory():
    recorded = bytearray()
    streams: list[_ThreadStream] = []

    def factory(pull, sample_rate, blocksize, device):  # noqa: ANN001
        s = _ThreadStream(lambda: recorded.extend(pull(blocksize)), blocksize / sample_rate)
        streams.append(s)
        return s

    factory.recorded = recorded  # type: ignore[attr-defined]
    factory.streams = streams  # type: ignore[attr-defined]
    return factory


def fake_factories(camera_fail: bool = False) -> tuple[DeviceFactories, dict[str, Any]]:
    handles: dict[str, Any] = {"cameras": [], "mic": sine_input_factory(), "spk": recording_output_factory()}

    def open_cam(source, w, h, fps):  # noqa: ANN001
        cam = SyntheticCamera(fail_open=camera_fail)
        handles["cameras"].append(cam)
        return cam

    factories = DeviceFactories(
        camera=lambda cb: CameraCapture("synthetic", 5.0, 320, 240, opener=open_cam, on_status=cb),
        microphone=lambda cb: MicrophoneCapture(48_000, None, stream_factory=handles["mic"], on_status=cb),
        speaker=lambda cb: SpeakerPlayback(48_000, None, stream_factory=handles["spk"], on_status=cb),
    )
    return factories, handles
