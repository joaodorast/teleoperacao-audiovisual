"""Servidor real em thread para testes de ponta a ponta no navegador.

Vídeo: arquivo gerado com OpenCV, lido pela MESMA rotina de captura usada com a webcam
(``TELEOP_CAMERA`` aceita índice de dispositivo ou caminho). Áudio do robô: dublês de
PortAudio (este ambiente não tem placa de som).
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import uvicorn

from fakes import recording_output_factory, sine_input_factory
from teleop.app import create_app
from teleop.capture.audio import MicrophoneCapture
from teleop.capture.video import CameraCapture
from teleop.playback.speaker import SpeakerPlayback
from teleop.session import DeviceFactories


def make_test_video(path: Path, w: int = 640, h: int = 480, seconds: int = 6, fps: int = 30) -> Path:
    if path.exists():
        return path
    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (w, h))
    for i in range(seconds * fps):
        img = np.full((h, w, 3), 236, np.uint8)
        cv2.rectangle(img, (40, 300), (600, 440), (210, 210, 214), -1)
        x = int((i * 7) % (w - 120))
        cv2.circle(img, (60 + x, 200), 46, (193, 164, 233), -1)
        cv2.putText(img, f"quadro {i:04d}", (40, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (60, 60, 70), 2)
        out.write(img)
    out.release()
    return path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerThread:
    def __init__(self, settings, video_path: Path, audio: str = "fake") -> None:
        self.mic_factory = sine_input_factory(330.0)
        self.spk_factory = recording_output_factory()
        if audio == "fake":
            factories = DeviceFactories(
                camera=lambda cb: CameraCapture(str(video_path), settings.video_fps, 640, 480, on_status=cb),
                microphone=lambda cb: MicrophoneCapture(48_000, stream_factory=self.mic_factory, on_status=cb),
                speaker=lambda cb: SpeakerPlayback(48_000, stream_factory=self.spk_factory, on_status=cb),
            )
        else:  # dispositivos de áudio reais (aqui: indisponíveis)
            factories = DeviceFactories(
                camera=lambda cb: CameraCapture(str(video_path), settings.video_fps, 640, 480, on_status=cb),
                microphone=lambda cb: MicrophoneCapture(48_000, on_status=cb),
                speaker=lambda cb: SpeakerPlayback(48_000, on_status=cb),
            )
        self.app = create_app(settings, factories)
        self.port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=lambda: asyncio.run(self.server.serve()), daemon=True)

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}"

    def __enter__(self) -> "ServerThread":
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("servidor não iniciou")

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(10)

    @property
    def sessions(self):
        return self.app.state.sessions
