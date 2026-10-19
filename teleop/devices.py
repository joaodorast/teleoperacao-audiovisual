"""Estado dos dispositivos do robô (webcam, microfone, alto-falante), enviado à interface."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Literal

DeviceState = Literal["idle", "starting", "active", "unavailable"]


@dataclass
class DeviceStatus:
    state: DeviceState = "idle"
    # Mensagem curta para o operador; nunca contém detalhes internos (caminhos, stack traces).
    message: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


StatusListener = Callable[[], None]


def audio_backend_error() -> str | None:
    """Retorna uma mensagem se a biblioteca de áudio (PortAudio) não puder ser carregada."""
    try:
        import sounddevice  # noqa: F401
    except (OSError, ImportError):
        return "Biblioteca de áudio (PortAudio) não encontrada no computador do robô."
    return None
