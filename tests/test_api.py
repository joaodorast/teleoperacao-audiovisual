"""API HTTP e WebSocket: autenticação, cabeçalhos de segurança e sinalização."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import TEST_PASSWORD
from fakes import fake_factories
from teleop.app import create_app
from teleop.auth import COOKIE_NAME

ORIGIN = {"origin": "http://testserver"}


@pytest.fixture
def client(settings):
    factories, _ = fake_factories()
    with TestClient(create_app(settings, factories)) as c:
        yield c


def _login(c, password=TEST_PASSWORD):
    return c.post("/api/login", json={"username": "operador", "password": password})


def test_index_and_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200 and "Teleoperação Audiovisual" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert client.get("/static/js/app.js").status_code == 200


def test_login_logout(client):
    assert client.get("/api/me").json() == {"authenticated": False}
    r = _login(client, "errada")
    assert r.status_code == 401 and "inválidos" in r.json()["detail"]
    r = _login(client)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert client.get("/api/me").json()["authenticated"] is True
    client.post("/api/logout")
    assert client.get("/api/me").json()["authenticated"] is False


def test_login_rate_limited(client):
    for _ in range(5):
        _login(client, "errada")
    r = _login(client)  # mesmo com a senha certa, bloqueado temporariamente
    assert r.status_code == 429


def test_revoked_token_rejected(client):
    _login(client)
    token = client.cookies.get(COOKIE_NAME)
    client.post("/api/logout")
    client.cookies.set(COOKIE_NAME, token)
    assert client.get("/api/me").json()["authenticated"] is False


def test_ws_requires_auth_and_origin(client):
    with client.websocket_connect("/ws", headers=ORIGIN) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
        assert e.value.code == 4401
    _login(client)
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/ws", headers={"origin": "http://evil.example"}) as ws:
            ws.receive_text()
    assert e.value.code == 4403


def test_ws_hello_ping_and_validation(client):
    _login(client)
    with client.websocket_connect("/ws", headers=ORIGIN) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["config"]["video_fps"] == 5.0
        assert hello["config"]["latency_target_ms"] == 300
        assert hello["session"]["state"] == "idle"
        ws.send_json({"type": "ping", "id": 7, "t": 123.0})
        pong = ws.receive_json()
        assert pong["type"] == "pong" and pong["id"] == 7 and pong["server_time"] > 0
        ws.send_json({"type": "comando_inventado"})
        assert ws.receive_json()["code"] == "invalid_message"
        ws.send_json({"type": "session.start", "sdp": "v=0 sem midia nenhuma"})
        msgs = []
        while not any(m.get("type") == "error" for m in msgs) and len(msgs) < 12:
            msgs.append(ws.receive_json())
        assert msgs[-1]["code"] == "negotiation_failed"
        states = [m["state"] for m in msgs if m["type"] == "session.state"]
        assert states[0] == "starting" and states[-1] == "idle"  # recursos liberados
