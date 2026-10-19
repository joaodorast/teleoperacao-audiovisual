"""Gera o valor de TELEOP_PASSWORD_HASH sem gravar a senha em lugar algum.

Uso: python -m teleop.hash_password
"""

from __future__ import annotations

import getpass
import sys

from .auth import hash_password


def main() -> int:
    password = getpass.getpass("Nova senha do operador: ")
    if len(password) < 8:
        print("A senha deve ter pelo menos 8 caracteres.", file=sys.stderr)
        return 1
    if getpass.getpass("Confirme a senha: ") != password:
        print("As senhas não coincidem.", file=sys.stderr)
        return 1
    print(f"TELEOP_PASSWORD_HASH={hash_password(password)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
