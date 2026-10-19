"""Integração: negociação WebRTC real (aiortc ↔ aiortc) com dispositivos simulados.

Um cliente aiortc faz o papel do navegador: oferece vídeo ``recvonly`` e áudio
``sendrecv`` (um tom de 440 Hz no lugar da voz do operador). Verifica-se:

* vídeo recebido a ~5 FPS (RF01/RF03);
* áudio do robô recebido continuamente (RF02/RF03);
* voz do operador chegando ao alto-falante do robô (RF04);
* mapa de relógio de vídeo correto, permitindo medir a latência fim a fim (RF05);
* encerramento controlado liberando webcam, microfone e alto-falante (RF06).
"""

from __future__ import annotations

import asyncio
import fractions
import time

import av
import numpy as np
import pytest
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack

from fakes import fake_factories
from teleop.session import SessionError, SessionManager


class FakeClient:
    def __init__(self) -> None:
        self.id = "c1"
        self.messages: list[dict] = []
        self._connected = True

    @property
    def connected(self) -> bool:
        return self._connected

    def post(self, message: dict) -> None:
        self.messages.append(message)

    def of(self, t: str) -> list[dict]:
        return [m for m in self.messages if m["type"] == t]


class ToneTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._pts = 0
        self._start: float | None = None

    async def recv(self) -> av.AudioFrame:
        if self._start is None:
            self._start = time.monotonic()
        wait = self._start + self._pts / 48000 - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        n = np.arange(self._pts, self._pts + 960)
        data = (np.sin(2 * np.pi * 440 * n / 48000) * 9000).astype(np.int16).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(data, format="s16", layout="mono")
        frame.sample_rate = 48000
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, 48000)
        self._pts += 960
        return frame


async def _operator_offer(pc: RTCPeerConnection) -> str:
    pc.addTransceiver("video", direction="recvonly")
    pc.addTrack(ToneTrack())
    await pc.setLocalDescription(await pc.createOffer())
    return pc.localDescription.sdp


@pytest.mark.asyncio
async def test_full_bidirectional_session(settings, monkeypatch):
    # O receptor do aiortc normaliza o timestamp RTP para começar em zero; o navegador
    # (requestVideoFrameCallback.rtpTimestamp) expõe o valor bruto. Para reproduzir o que
    # o navegador vê, o mapeamento é desativado no cliente de teste.
    import aiortc.rtcrtpreceiver as receiver_mod

    monkeypatch.setattr(receiver_mod.TimestampMapper, "map", lambda self, ts: ts)
    factories, handles = fake_factories()
    manager = SessionManager(settings, factories)
    await manager.startup()
    client = FakeClient()
    pc = RTCPeerConnection()
    received: dict[str, list] = {"video": [], "audio": []}

    @pc.on("track")
    def on_track(track):  # noqa: ANN001
        async def pump():
            try:
                while True:
                    frame = await track.recv()
                    received[track.kind].append((time.time() * 1000, frame))
            except Exception:
                pass

        asyncio.ensure_future(pump())

    try:
        answer = await manager.start(client, await _operator_offer(pc))
        await pc.setRemoteDescription(RTCSessionDescription(answer, "answer"))
        assert manager.state == "active"

        await asyncio.sleep(4.0)
        assert manager.transport_state == "connected"

        # RF01/RF03 — ~5 FPS recebidos (descarta o 1º segundo de estabelecimento)
        vids = received["video"]
        assert len(vids) >= 10
        recent = [t for t, _ in vids if t > vids[-1][0] - 2000]
        fps = (len(recent) - 1) / ((recent[-1] - recent[0]) / 1000)
        assert 3.5 <= fps <= 6.5, fps
        cam_status = manager.camera.status().metrics
        assert cam_status["configured_fps"] == 5.0
        assert 4.0 <= cam_status["measured_fps"] <= 6.0

        # RF02/RF03 — áudio do robô contínuo (blocos de 20 ms ≈ 50/s)
        assert len(received["audio"]) >= 100
        pcm = received["audio"][-1][1].to_ndarray()
        assert np.abs(pcm).max() > 1000  # tom do microfone simulado, não silêncio

        # RF04 — voz do operador reproduzida no alto-falante do robô
        rec = np.frombuffer(bytes(handles["spk"].recorded), dtype=np.int16)
        assert rec.size > 48000  # > 1 s reproduzido
        assert np.abs(rec[-48000:]).max() > 2000

        # RF05 — mapa de relógio de vídeo: captura → recepção fim a fim
        clocks = client.of("video.clock")
        assert clocks, "origem do timestamp RTP não descoberta"
        origin, epoch = clocks[-1]["rtp_origin"], clocks[-1]["epoch_ms"]
        latencies = []
        for t_recv, frame in vids[-5:]:
            capture_ms = epoch + ((frame.pts - origin) & 0xFFFFFFFF) / 90
            latencies.append(t_recv - capture_ms)
        assert all(0 <= lat < 300 for lat in latencies), latencies

        stats = await manager.peer.stats()
        assert "operator_audio_in" in stats
        assert stats["operator_audio_in"]["packets_received"] > 50
    finally:
        await manager.stop(client)
        await pc.close()

    # RF06 — encerramento controlado: tudo liberado
    assert manager.state == "idle"
    assert manager.camera is None and manager.peer is None
    assert all(cam.released for cam in handles["cameras"])
    assert all(not s.active for s in handles["mic"].streams)
    assert all(not s.active for s in handles["spk"].streams)
    states = [m["state"] for m in client.of("session.state")]
    assert states[0] == "starting" and "active" in states and states[-1] == "idle"
    await manager.shutdown()


@pytest.mark.asyncio
async def test_second_window_cannot_take_active_session(settings):
    factories, _ = fake_factories()
    manager = SessionManager(settings, factories)
    await manager.startup()
    a, b = FakeClient(), FakeClient()
    pc_a, pc_b = RTCPeerConnection(), RTCPeerConnection()
    try:
        await manager.start(a, await _operator_offer(pc_a))
        with pytest.raises(SessionError) as err:
            await manager.start(b, await _operator_offer(pc_b))
        assert err.value.code == "session_in_use"
        with pytest.raises(SessionError):
            await manager.stop(b)
    finally:
        await manager.stop(a)
        await pc_a.close()
        await pc_b.close()
        await manager.shutdown()


@pytest.mark.asyncio
async def test_orphan_session_resume_and_timeout(settings):
    factories, handles = fake_factories()
    manager = SessionManager(settings, factories)
    await manager.startup()
    a = FakeClient()
    pc = RTCPeerConnection()
    try:
        answer = await manager.start(a, await _operator_offer(pc))
        await pc.setRemoteDescription(RTCSessionDescription(answer, "answer"))
        await asyncio.sleep(1.5)
        sid = manager.session_id

        # queda da sinalização → nova conexão retoma a mesma sessão
        a._connected = False
        manager.client_disconnected(a)
        b = FakeClient()
        assert manager.resume(b, sid)
        assert manager.owner is b and manager.state == "active"
        assert not manager.resume(FakeClient(), "outro-id")

        # sinalização cai e ninguém retoma → sessão encerrada após a tolerância (3 s)
        b._connected = False
        manager.client_disconnected(b)
        for _ in range(60):
            if manager.state == "idle":
                break
            await asyncio.sleep(0.1)
        assert manager.state == "idle"
        assert all(cam.released for cam in handles["cameras"])
    finally:
        await pc.close()
        await manager.shutdown()


@pytest.mark.asyncio
async def test_renegotiation_keeps_devices_open(settings):
    """Reconexão de mídia: nova oferta do mesmo operador troca só a conexão WebRTC."""
    factories, handles = fake_factories()
    manager = SessionManager(settings, factories)
    await manager.startup()
    a = FakeClient()
    pc1, pc2 = RTCPeerConnection(), RTCPeerConnection()
    try:
        ans = await manager.start(a, await _operator_offer(pc1))
        await pc1.setRemoteDescription(RTCSessionDescription(ans, "answer"))
        cam = manager.camera
        first_peer = manager.peer
        ans2 = await manager.start(a, await _operator_offer(pc2))
        await pc2.setRemoteDescription(RTCSessionDescription(ans2, "answer"))
        assert manager.camera is cam and manager.peer is not first_peer
        assert len(handles["cameras"]) == 1
        await asyncio.sleep(2)
        assert manager.transport_state == "connected"
    finally:
        await manager.stop(a)
        await pc1.close()
        await pc2.close()
        await manager.shutdown()


@pytest.mark.asyncio
async def test_invalid_offer_rolls_back(settings):
    factories, handles = fake_factories()
    manager = SessionManager(settings, factories)
    await manager.startup()
    a = FakeClient()
    with pytest.raises(SessionError) as err:
        await manager.start(a, "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\nm=audio garbage\r\n")
    assert err.value.code == "negotiation_failed"
    assert manager.state == "idle" and manager.camera is None
    assert all(c.released for c in handles["cameras"])
    await manager.shutdown()
