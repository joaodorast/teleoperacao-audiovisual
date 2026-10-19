"""Teleoperação Audiovisual — Projeto 2 (robô humanoide).

Pacotes:
- ``capture``   captura de vídeo (OpenCV) e de áudio (microfone do robô);
- ``playback``  reprodução da voz do operador no alto-falante do robô;
- ``transport`` trilhas e conexão WebRTC (aiortc);
- ``session``   ciclo de vida controlado da sessão de teleoperação;
- ``signaling`` canal WebSocket de sinalização e controle;
- ``auth``      autenticação do operador.
"""

__version__ = "1.0.0"
