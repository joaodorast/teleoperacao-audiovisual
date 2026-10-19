"""Configuração carregada de variáveis de ambiente (opcionalmente de um arquivo ``.env``).

Nenhum segredo possui valor padrão no código: usuário, senha (ou hash) e chave de
assinatura precisam vir do ambiente.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Configuração ausente ou inválida."""


def load_dotenv(path: Path) -> None:
    """Carrega ``KEY=VALUE`` de um arquivo .env sem sobrescrever o ambiente existente."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _int(env: dict[str, str], key: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} deve ser um número inteiro") from exc
    if not lo <= value <= hi:
        raise ConfigError(f"{key} deve estar entre {lo} e {hi}")
    return value


def _float(env: dict[str, str], key: str, default: float, lo: float, hi: float) -> float:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} deve ser numérico") from exc
    if not lo <= value <= hi:
        raise ConfigError(f"{key} deve estar entre {lo} e {hi}")
    return value


def _opt_int_or_str(raw: str | None) -> int | str | None:
    if raw is None or raw.strip() == "":
        return None
    raw = raw.strip()
    return int(raw) if raw.lstrip("-").isdigit() else raw


@dataclass(frozen=True)
class Settings:
    # Autenticação
    username: str
    password_hash: str | None
    password_plain: str | None = field(repr=False, default=None)
    secret_key: str = field(repr=False, default="")
    token_ttl_s: int = 8 * 3600

    # Servidor
    host: str = "0.0.0.0"
    port: int = 8443
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None
    allowed_origins: tuple[str, ...] = ()

    # Vídeo (RF01)
    camera: int | str = 0
    video_fps: float = 5.0
    video_width: int = 640
    video_height: int = 480
    video_codec: str = "vp8"

    # Áudio (RF02 / RF04)
    audio_input_device: int | str | None = None
    audio_output_device: int | str | None = None
    audio_sample_rate: int = 48_000

    # Sessão / reconexão
    session_grace_s: float = 20.0

    @property
    def tls_enabled(self) -> bool:
        return bool(self.ssl_certfile and self.ssl_keyfile)


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)

    username = env.get("TELEOP_USERNAME", "").strip()
    if not username:
        raise ConfigError("Defina TELEOP_USERNAME (veja .env.example)")

    password_hash = env.get("TELEOP_PASSWORD_HASH", "").strip() or None
    password_plain = env.get("TELEOP_PASSWORD") or None
    if not password_hash and not password_plain:
        raise ConfigError(
            "Defina TELEOP_PASSWORD_HASH (recomendado: python -m teleop.hash_password) "
            "ou TELEOP_PASSWORD"
        )

    secret_key = env.get("TELEOP_SECRET_KEY", "").strip()
    if not secret_key:
        secret_key = secrets.token_urlsafe(48)
        log.warning(
            "TELEOP_SECRET_KEY não definida: chave temporária gerada; "
            "os logins serão invalidados ao reiniciar o servidor."
        )
    elif len(secret_key) < 32:
        raise ConfigError("TELEOP_SECRET_KEY deve ter pelo menos 32 caracteres")

    certfile = env.get("TELEOP_SSL_CERTFILE", "").strip() or None
    keyfile = env.get("TELEOP_SSL_KEYFILE", "").strip() or None
    if bool(certfile) != bool(keyfile):
        raise ConfigError("Defina TELEOP_SSL_CERTFILE e TELEOP_SSL_KEYFILE juntos")

    origins = tuple(
        o.strip().rstrip("/") for o in env.get("TELEOP_ALLOWED_ORIGINS", "").split(",") if o.strip()
    )

    camera = _opt_int_or_str(env.get("TELEOP_CAMERA"))
    video_codec = env.get("TELEOP_VIDEO_CODEC", "vp8").strip().lower() or "vp8"
    if video_codec not in ("vp8", "h264"):
        raise ConfigError("TELEOP_VIDEO_CODEC deve ser vp8 ou h264")

    return Settings(
        username=username,
        password_hash=password_hash,
        password_plain=None if password_hash else password_plain,
        secret_key=secret_key,
        token_ttl_s=_int(env, "TELEOP_TOKEN_TTL_S", 8 * 3600, 300, 7 * 24 * 3600),
        host=env.get("TELEOP_HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=_int(env, "TELEOP_PORT", 8443, 1, 65535),
        ssl_certfile=certfile,
        ssl_keyfile=keyfile,
        allowed_origins=origins,
        camera=0 if camera is None else camera,
        video_fps=_float(env, "TELEOP_VIDEO_FPS", 5.0, 1.0, 30.0),
        video_width=_int(env, "TELEOP_VIDEO_WIDTH", 640, 160, 1920),
        video_height=_int(env, "TELEOP_VIDEO_HEIGHT", 480, 120, 1080),
        video_codec=video_codec,
        audio_input_device=_opt_int_or_str(env.get("TELEOP_AUDIO_INPUT_DEVICE")),
        audio_output_device=_opt_int_or_str(env.get("TELEOP_AUDIO_OUTPUT_DEVICE")),
        audio_sample_rate=_int(env, "TELEOP_AUDIO_SAMPLE_RATE", 48_000, 8_000, 96_000),
        session_grace_s=_float(env, "TELEOP_SESSION_GRACE_S", 20.0, 3.0, 300.0),
    )
