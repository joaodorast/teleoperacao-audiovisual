"""Testes unitários: autenticação, configuração, buffers, captura e reprodução."""

from __future__ import annotations

import asyncio
import time

import av
import numpy as np
import pytest

from fakes import SyntheticCamera, recording_output_factory, sine_input_factory
from teleop.auth import Authenticator, LoginRateLimiter, hash_password, verify_password
from teleop.capture.audio import MicrophoneCapture
from teleop.capture.buffers import BoundedAudioQueue, LatestSlot
from teleop.capture.video import CameraCapture
from teleop.config import ConfigError, load_settings
from teleop.playback.speaker import JitterBuffer, SpeakerPlayback
from teleop.signaling import ClientMessage, origin_allowed
from teleop.transport.tracks import MicrophoneTrack


# ---------------------------------------------------------------- autenticação
def test_password_hash_roundtrip():
    h = hash_password("segredo-forte", iterations=1000)
    assert h.startswith("pbkdf2_sha256$1000$")
    assert verify_password("segredo-forte", h)
    assert not verify_password("errado", h)
    assert not verify_password("x", "lixo")


def test_token_lifecycle():
    auth = Authenticator("op", "s" * 40, password_hash=hash_password("pw", 1000), token_ttl_s=60)
    assert auth.check_credentials("op", "pw")
    assert not auth.check_credentials("op", "outra")
    assert not auth.check_credentials("outro", "pw")

    token = auth.issue_token(now=1000)
    claims = auth.verify_token(token, now=1010)
    assert claims and claims.sub == "op"
    assert auth.verify_token(token, now=1061) is None  # expirado
    body, sig = token.split(".")
    assert auth.verify_token(body + "." + sig[:-2] + "AA", now=1010) is None  # assinatura adulterada
    other = Authenticator("op", "t" * 40, password_plain="pw")
    assert other.verify_token(token, now=1010) is None  # outra chave
    auth.revoke(claims, now=1010)
    assert auth.verify_token(token, now=1010) is None  # logout


def test_rate_limiter():
    rl = LoginRateLimiter(max_failures=3, window_s=10)
    for _ in range(3):
        assert rl.blocked_for("ip", now=0) == 0
        rl.record_failure("ip", now=0)
    assert rl.blocked_for("ip", now=1) > 0
    assert rl.blocked_for("ip", now=11) == 0


# ---------------------------------------------------------------- configuração
def test_settings_require_credentials():
    with pytest.raises(ConfigError):
        load_settings({})
    with pytest.raises(ConfigError):
        load_settings({"TELEOP_USERNAME": "op"})
    with pytest.raises(ConfigError):
        load_settings({"TELEOP_USERNAME": "op", "TELEOP_PASSWORD": "x", "TELEOP_SECRET_KEY": "curta"})


def test_settings_defaults_and_parsing():
    s = load_settings({"TELEOP_USERNAME": "op", "TELEOP_PASSWORD": "pw", "TELEOP_CAMERA": "/dev/video2"})
    assert s.video_fps == 5.0  # RF01
    assert s.camera == "/dev/video2"
    assert len(s.secret_key) >= 32  # gerada quando ausente
    assert "pw" not in repr(s)  # segredo fora do repr/logs
    s2 = load_settings({"TELEOP_USERNAME": "op", "TELEOP_PASSWORD": "pw", "TELEOP_CAMERA": "1"})
    assert s2.camera == 1
    with pytest.raises(ConfigError):
        load_settings({"TELEOP_USERNAME": "op", "TELEOP_PASSWORD": "pw", "TELEOP_VIDEO_CODEC": "av1"})


# ---------------------------------------------------------------- sinalização
def test_message_validation():
    assert ClientMessage.validate_json('{"type":"ping","id":1,"t":2.5}').id == 1
    for bad in ('{"type":"exec","cmd":"rm"}', '{"type":"ping","id":-1,"t":0}',
                '{"type":"session.stop","extra":1}', "não json"):
        with pytest.raises(Exception):
            ClientMessage.validate_json(bad)


def test_origin_check():
    assert origin_allowed("https://10.0.0.5:8443", "10.0.0.5:8443", ())
    assert not origin_allowed("https://evil.example", "10.0.0.5:8443", ())
    assert not origin_allowed(None, "10.0.0.5:8443", ())
    assert origin_allowed("https://painel.lab", "10.0.0.5:8443", ("https://painel.lab",))


# ---------------------------------------------------------------- buffers
@pytest.mark.asyncio
async def test_latest_slot_drops_old_frames():
    slot: LatestSlot[int] = LatestSlot()
    for i in range(5):
        slot.put(i)
    seq, item = await slot.get_newer(0, timeout=0.1)
    assert item == 4 and seq == 5  # só o mais recente
    assert slot.dropped == 4
    with pytest.raises(asyncio.TimeoutError):
        await slot.get_newer(seq, timeout=0.05)


@pytest.mark.asyncio
async def test_bounded_audio_queue():
    q = BoundedAudioQueue(3)
    for i in range(5):
        q.put(bytes([i]))
    assert len(q) == 3 and q.dropped == 2
    assert await q.get(0.01) == b"\x02"
    q.clear()
    assert await q.get(0.02) is None


# ---------------------------------------------------------------- captura de vídeo
def test_camera_capture_rate_and_release():
    cams: list[SyntheticCamera] = []

    def opener(*_):
        cams.append(SyntheticCamera())
        return cams[-1]

    cap = CameraCapture(0, 5.0, 320, 240, opener=opener)
    cap.start()
    time.sleep(2.3)
    st = cap.status()
    cap.stop()
    assert st.state == "active"
    assert 4.3 <= st.metrics["measured_fps"] <= 5.7
    assert 9 <= st.metrics["frames"] <= 13
    _, frame = cap.slot.peek()
    assert frame is None  # buffer limpo ao encerrar
    assert cams[0].released and not cap.running


def test_camera_unavailable_retries():
    attempts = []

    def opener(*_):
        attempts.append(1)
        return SyntheticCamera(fail_open=True)

    cap = CameraCapture(0, 5.0, 320, 240, opener=opener)
    cap.REOPEN_INTERVAL_S = 0.2
    cap.start()
    time.sleep(0.9)
    st = cap.status()
    cap.stop()
    assert st.state == "unavailable" and "webcam" in st.message
    assert len(attempts) >= 3


def test_camera_frames_are_resized_and_yuv():
    cap = CameraCapture(0, 5.0, 160, 120, opener=lambda *_: SyntheticCamera(320, 240))
    cap.start()
    time.sleep(0.6)
    _, captured = cap.slot.peek()
    cap.stop()
    assert captured.frame.width == 160 and captured.frame.format.name == "yuv420p"


# ---------------------------------------------------------------- áudio
@pytest.mark.asyncio
async def test_microphone_track_continuity():
    mic = MicrophoneCapture(48_000, stream_factory=sine_input_factory())
    mic.start()
    track = MicrophoneTrack(mic)
    frames = [await track.recv() for _ in range(25)]
    mic.stop()
    assert all(f.samples == 960 and f.sample_rate == 48_000 for f in frames)
    assert [f.pts for f in frames] == [i * 960 for i in range(25)]
    assert np.abs(frames[-1].to_ndarray()).max() > 1000
    # Microfone parado: a trilha continua entregando silêncio no ritmo certo
    t0 = time.monotonic()
    silent = [await track.recv() for _ in range(5)]
    assert all(np.abs(f.to_ndarray()).max() == 0 for f in silent)
    assert time.monotonic() - t0 < 0.5
    track.stop()


def test_microphone_backend_missing_is_reported():
    def broken(*_):
        raise OSError("no device")

    mic = MicrophoneCapture(stream_factory=broken)
    mic.REOPEN_INTERVAL_S = 0.1
    mic.start()
    time.sleep(0.3)
    st = mic.status()
    mic.stop()
    assert st.state == "unavailable"


def test_jitter_buffer_bounds_delay():
    jb = JitterBuffer(48_000, prebuffer_ms=40, max_ms=160, target_ms=60)
    assert jb.read(480) == bytes(960)  # antes do pré-buffer: silêncio
    jb.write(bytes(48 * 2 * 500))  # 500 ms de uma vez (rajada)
    assert jb.buffered_ms(48_000) == pytest.approx(60, abs=1)  # atraso cortado
    assert jb.dropped_bytes > 0
    jb.read(48 * 60)
    jb.read(480)
    assert jb.underruns == 1


def test_speaker_plays_fed_audio():
    factory = recording_output_factory()
    spk = SpeakerPlayback(48_000, stream_factory=factory)
    spk.start()
    time.sleep(0.2)
    for i in range(30):
        data = (np.sin(np.arange(960) / 10) * 9000).astype(np.int16).reshape(1, -1)
        f = av.AudioFrame.from_ndarray(data, format="s16", layout="mono")
        f.sample_rate = 48_000
        f.pts = i * 960
        spk.feed(f)
        time.sleep(0.02)
    time.sleep(0.15)
    assert spk.status().metrics["receiving_voice"]
    spk.stop()
    rec = np.frombuffer(bytes(factory.recorded), dtype=np.int16)
    assert np.abs(rec).max() > 5000
    assert all(not s.active for s in factory.streams)
