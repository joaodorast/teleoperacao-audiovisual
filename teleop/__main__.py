"""Inicia o serviço no computador conectado ao robô.

Uso: python -m teleop   (configuração via variáveis de ambiente ou arquivo .env)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import uvicorn

from .app import create_app
from .config import ConfigError, load_dotenv, load_settings


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # Evita poluir o log com cada pacote RTP/ICE.
    for noisy in ("aioice", "aiortc"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    load_dotenv(Path.cwd() / ".env")
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Erro de configuração: {exc}", file=sys.stderr)
        return 2

    app = create_app(settings)
    scheme = "https" if settings.tls_enabled else "http"
    logging.getLogger("teleop").info(
        "Interface do operador em %s://<endereço-deste-computador>:%d", scheme, settings.port
    )
    if not settings.tls_enabled:
        logging.getLogger("teleop").warning(
            "TLS desativado: navegadores só liberam o microfone do operador em HTTPS ou localhost. "
            "Veja a seção HTTPS do README."
        )
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        ssl_certfile=settings.ssl_certfile,
        ssl_keyfile=settings.ssl_keyfile,
        log_level="info",
        ws_max_size=128 * 1024,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
