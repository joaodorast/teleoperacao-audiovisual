from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from teleop.auth import hash_password  # noqa: E402
from teleop.config import load_settings  # noqa: E402

TEST_PASSWORD = "senha-de-teste-123"


@pytest.fixture(scope="session")
def password_hash() -> str:
    return hash_password(TEST_PASSWORD, iterations=1000)


@pytest.fixture
def settings(password_hash):
    return load_settings(
        {
            "TELEOP_USERNAME": "operador",
            "TELEOP_PASSWORD_HASH": password_hash,
            "TELEOP_SECRET_KEY": "k" * 40,
            "TELEOP_SESSION_GRACE_S": "3",
        }
    )
