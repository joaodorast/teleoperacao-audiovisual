"""Autenticação do operador.

- Senha verificada contra um hash PBKDF2-SHA256 (``python -m teleop.hash_password``).
- Após o login o servidor emite um token assinado (HMAC-SHA256) com validade limitada,
  entregue em cookie ``HttpOnly`` / ``SameSite=Strict``. O mesmo cookie autentica a
  conexão WebSocket de sinalização.
- Tentativas de login malsucedidas são limitadas por endereço IP.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import deque
from dataclasses import dataclass

COOKIE_NAME = "teleop_session"
PBKDF2_ITERATIONS = 390_000


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), _b64d(salt), int(iterations)
        )
        return hmac.compare_digest(digest, _b64d(expected))
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class TokenClaims:
    sub: str
    iat: int
    exp: int
    jti: str


class Authenticator:
    def __init__(
        self,
        username: str,
        secret_key: str,
        password_hash: str | None = None,
        password_plain: str | None = None,
        token_ttl_s: int = 8 * 3600,
    ) -> None:
        if not password_hash and not password_plain:
            raise ValueError("password_hash ou password_plain é obrigatório")
        self._username = username
        self._key = secret_key.encode("utf-8")
        # Mesmo com senha em texto no ambiente, a comparação usa um hash em memória.
        self._password_hash = password_hash or hash_password(password_plain or "", 50_000)
        self._ttl = token_ttl_s
        self._revoked: dict[str, int] = {}

    @property
    def token_ttl_s(self) -> int:
        return self._ttl

    def check_credentials(self, username: str, password: str) -> bool:
        user_ok = hmac.compare_digest(username.encode("utf-8"), self._username.encode("utf-8"))
        # A senha é sempre verificada para não revelar, pelo tempo de resposta, se o usuário existe.
        pass_ok = verify_password(password, self._password_hash)
        return user_ok and pass_ok

    def _sign(self, payload: bytes) -> str:
        return _b64e(hmac.new(self._key, payload, hashlib.sha256).digest())

    def issue_token(self, now: float | None = None) -> str:
        now_i = int(time.time() if now is None else now)
        claims = {
            "sub": self._username,
            "iat": now_i,
            "exp": now_i + self._ttl,
            "jti": secrets.token_urlsafe(12),
        }
        payload = json.dumps(claims, separators=(",", ":")).encode("utf-8")
        return f"{_b64e(payload)}.{self._sign(payload)}"

    def verify_token(self, token: str | None, now: float | None = None) -> TokenClaims | None:
        if not token or token.count(".") != 1 or len(token) > 1024:
            return None
        body, signature = token.split(".")
        try:
            payload = _b64d(body)
        except (ValueError, TypeError):
            return None
        if not hmac.compare_digest(signature, self._sign(payload)):
            return None
        try:
            data = json.loads(payload)
            claims = TokenClaims(
                sub=str(data["sub"]), iat=int(data["iat"]), exp=int(data["exp"]), jti=str(data["jti"])
            )
        except (ValueError, KeyError, TypeError):
            return None
        now_i = int(time.time() if now is None else now)
        if claims.exp <= now_i or claims.sub != self._username or claims.jti in self._revoked:
            return None
        return claims

    def revoke(self, claims: TokenClaims, now: float | None = None) -> None:
        self._revoked[claims.jti] = claims.exp + 1
        now = int(time.time() if now is None else now)
        for jti, exp in list(self._revoked.items()):
            if exp <= now:
                del self._revoked[jti]


class LoginRateLimiter:
    """Limita falhas de login por cliente numa janela deslizante."""

    def __init__(self, max_failures: int = 5, window_s: float = 60.0) -> None:
        self._max = max_failures
        self._window = window_s
        self._failures: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._failures.setdefault(key, deque())
        while q and now - q[0] > self._window:
            q.popleft()
        return q

    def blocked_for(self, key: str, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        q = self._prune(key, now)
        if len(q) < self._max:
            return 0.0
        return max(0.0, self._window - (now - q[0]))

    def record_failure(self, key: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._prune(key, now).append(now)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
