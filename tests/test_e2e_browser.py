"""Ponta a ponta no Chromium: interface real ↔ backend real ↔ WebRTC real.

Executado apenas se o Playwright e o Chromium estiverem instalados. O microfone do
operador é o dispositivo simulado do próprio Chromium (``--use-fake-device-for-media-stream``).
Capturas de tela são gravadas em ``tests/artifacts/``.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

import numpy as np
import pytest

pw = pytest.importorskip("playwright.sync_api")

from conftest import TEST_PASSWORD  # noqa: E402
from e2e_harness import ServerThread, make_test_video  # noqa: E402

ART = Path(__file__).parent / "artifacts"
CHROME_ARGS = [
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
]

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def video_file(tmp_path_factory):
    return make_test_video(tmp_path_factory.mktemp("video") / "cena.avi")


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch(args=CHROME_ARGS)
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"Chromium indisponível: {exc}")
        yield b
        b.close()


def _shot(page, name):
    ART.mkdir(exist_ok=True)
    page.screenshot(path=str(ART / f"{name}.png"))


def _login(page, url, password=TEST_PASSWORD):
    page.goto(url)
    page.wait_for_selector("#auth-layer:not([hidden])")
    page.fill("#login-user", "operador")
    page.fill("#login-pass", password)
    page.click("#login-submit")


def _wait_live(page, timeout=20000):
    page.wait_for_selector('#stage[data-view="live"]', timeout=timeout)


def test_operator_flow(browser, settings, video_file):
    errors: list[str] = []
    with ServerThread(settings, video_file, audio="fake") as srv:
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, bypass_csp=True)
        ctx.grant_permissions(["microphone"], origin=srv.url)
        page = ctx.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        # Autenticação: senha errada é recusada
        _login(page, srv.url, password="errada")
        page.wait_for_selector("#login-error:not([hidden])")
        assert "inválidos" in page.inner_text("#login-error")
        _shot(page, "01-login-erro")

        page.fill("#login-pass", TEST_PASSWORD)
        page.click("#login-submit")
        page.wait_for_selector("#auth-layer", state="hidden")
        page.wait_for_function("document.querySelector('#pill-connection .pill-text').textContent === 'Robô disponível'")
        assert page.is_enabled("#btn-session")
        assert not page.is_enabled("#btn-mic")
        _shot(page, "02-pronto")

        # RF06 início — RF01/RF03 vídeo recebido
        page.click("#btn-session")
        _wait_live(page)
        time.sleep(5)
        recv = page.inner_text("#hud-recv")
        fps = float(re.sub(r"[^\d,]", "", recv).replace(",", "."))
        assert 3.5 <= fps <= 6.5, recv
        assert "5 FPS configurado" in page.inner_text("#hud-cap")
        assert "Sessão ativa" in page.inner_text("#pill-session")
        assert page.inner_text("#robot-audio-state") == "Recebendo áudio do ambiente"  # RF02/RF03

        # RF05 — métricas reais
        lat = page.inner_text("#lat-main")
        assert lat.isdigit() and int(lat) < 1000, lat
        assert "fim a fim" in page.inner_text("#lat-caption")
        assert page.inner_text("#m-rtt").endswith("ms")
        assert page.inner_text("#m-vloss").endswith("%")

        # RF04 — voz do operador no alto-falante do robô
        page.click("#btn-mic")
        page.wait_for_selector('#btn-mic[aria-pressed="true"]')
        time.sleep(4)
        rec = np.frombuffer(bytes(srv.spk_factory.recorded), dtype=np.int16)
        assert rec.size > 48000 and np.abs(rec[-48000:]).max() > 500, "voz não chegou ao robô"
        assert page.inner_text("#m-oloss").endswith("%")
        _shot(page, "03-sessao-ativa")

        # Falha do canal de controle: o navegador reconecta e retoma a MESMA sessão
        loop = srv.sessions._loop
        sid = srv.sessions.session_id
        asyncio.run_coroutine_threadsafe(srv.sessions.owner._ws.close(code=1011), loop).result(5)
        page.wait_for_function("document.querySelector('#s-signal span:last-child').textContent === 'Reconectando'", timeout=5000)
        page.wait_for_function("document.querySelector('#s-signal span:last-child').textContent === 'Conectado'", timeout=15000)
        time.sleep(1.5)
        assert srv.sessions.session_id == sid and srv.sessions.owner.connected
        _wait_live(page)

        # Falha da mídia: o robô derruba a conexão WebRTC; o navegador renegocia sozinho
        old_peer = srv.sessions.peer
        asyncio.run_coroutine_threadsafe(old_peer.close(), loop).result(5)
        page.wait_for_function("document.querySelector('#s-media span:last-child').textContent === 'Reconectando'", timeout=10000)
        _shot(page, "03b-reconectando")
        page.wait_for_function("document.querySelector('#s-media span:last-child').textContent === 'Conectado'", timeout=20000)
        _wait_live(page)
        assert srv.sessions.peer is not old_peer and srv.sessions.session_id == sid

        # Recarregar a página: sessão fica órfã e pode ser assumida sem reabrir a webcam
        cam_before = srv.sessions.camera
        page.reload()
        page.wait_for_function("document.querySelector('#btn-session .btn-label').textContent === 'Assumir sessão'")
        _shot(page, "04-sessao-sem-operador")
        page.click("#btn-session")
        _wait_live(page)
        assert srv.sessions.camera is cam_before

        # RF06 — encerramento controlado com confirmação
        page.click("#btn-session")
        page.wait_for_selector("#confirm-stop[open]")
        _shot(page, "05-confirmar-encerramento")
        page.click("#confirm-stop button[value='stop']")
        page.wait_for_function("document.querySelector('#pill-session .pill-text').textContent === 'Sessão inativa'")
        for _ in range(50):
            if srv.sessions.state == "idle":
                break
            time.sleep(0.1)
        assert srv.sessions.state == "idle" and srv.sessions.camera is None
        assert srv.mic_factory.streams and all(not s.active for s in srv.mic_factory.streams)
        assert all(not s.active for s in srv.spk_factory.streams)
        _shot(page, "06-sessao-encerrada")

        # Responsivo
        page.set_viewport_size({"width": 390, "height": 844})
        page.click("#btn-session")
        _wait_live(page)
        time.sleep(2)
        page.screenshot(path=str(ART / "07-mobile.png"), full_page=True)
        page.set_viewport_size({"width": 900, "height": 1000})
        page.screenshot(path=str(ART / "08-tablet.png"), full_page=True)
        page.click("#btn-session")
        page.click("#confirm-stop button[value='stop']")
        page.wait_for_function("document.querySelector('#pill-session .pill-text').textContent === 'Sessão inativa'")
        ctx.close()

    # O único erro de recurso esperado é o 401 da tentativa de login com senha errada.
    unexpected = [e for e in errors if "status of 401" not in e]
    assert not unexpected, errors
    assert sum("status of 401" in e for e in errors) == 1, errors


def test_unavailable_robot_audio_devices(browser, settings, video_file):
    """Sem placa de som no robô: vídeo segue funcionando e a interface informa a falha."""
    with ServerThread(settings, video_file, audio="real") as srv:
        # Sem bypass de CSP: garante que a política de segurança não bloqueia a interface.
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        ctx.grant_permissions(["microphone"], origin=srv.url)
        page = ctx.new_page()
        console: list[str] = []
        page.on("console", lambda m: console.append(m.text))
        _login(page, srv.url)
        page.wait_for_selector("#auth-layer", state="hidden")
        page.wait_for_selector("#btn-session:not([disabled])")
        page.click("#btn-session")
        _wait_live(page)
        time.sleep(2)
        assert page.inner_text("#robot-audio-state") == "Microfone do robô indisponível"
        assert page.inner_text("#d-microphone .dev-state") == "Indisponível"
        assert page.inner_text("#d-speaker .dev-state") == "Indisponível"
        _shot(page, "09-audio-robo-indisponivel")
        page.click("#btn-session")
        page.click("#confirm-stop button[value='stop']")
        page.wait_for_selector('#pill-session[data-tone="neutral"]')
        assert not [m for m in console if "Content Security Policy" in m], console
        ctx.close()


def test_unauthenticated_websocket_is_rejected(browser, settings, video_file):
    with ServerThread(settings, video_file) as srv:
        page = browser.new_page(bypass_csp=True)
        page.goto(srv.url)
        code = page.evaluate(
            """() => new Promise(r => { const ws = new WebSocket(`ws://${location.host}/ws`);
                 ws.onclose = e => r(e.code); })"""
        )
        assert code == 4401
        page.close()
