"""Gera um certificado autoassinado para HTTPS na rede local (funciona no Windows e no Linux).

Uso: python scripts/gerar_certificado.py [IP ...] [--host robo.local]
Sem IP, usa o IP da rede local detectado automaticamente.
"""

from __future__ import annotations

import argparse
import datetime
import ipaddress
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def detectar_ip() -> str:
    """IP da interface usada para sair para a rede (nenhum pacote é enviado)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ips", nargs="*", help="IP(s) do computador do robô")
    parser.add_argument("--host", default="robo.local", help="nome do host (padrão: robo.local)")
    args = parser.parse_args()

    ips = args.ips or [detectar_ip()]
    for ip in ips:
        ipaddress.ip_address(ip)  # valida

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, args.host)])
    san = x509.SubjectAlternativeName(
        [x509.DNSName(args.host), x509.DNSName("localhost")]
        + [x509.IPAddress(ipaddress.ip_address(ip)) for ip in [*ips, "127.0.0.1"]]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(san, critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    certs = Path("certs")
    certs.mkdir(exist_ok=True)
    keyfile = certs / "robo.key"
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    keyfile.chmod(0o600)
    (certs / "robo.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    print(f"Certificado criado em certs/robo.crt (válido para {', '.join(ips)} e {args.host}).")


if __name__ == "__main__":
    main()
