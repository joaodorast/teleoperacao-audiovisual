#!/usr/bin/env sh
# Gera um certificado autoassinado para HTTPS na rede local.
# Uso: scripts/gerar_certificado.sh 192.168.0.42 [nome-do-host]
set -eu
IP="${1:?informe o IP do computador do robô}"
HOST="${2:-robo.local}"
mkdir -p certs
openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout certs/robo.key -out certs/robo.crt \
  -subj "/CN=$HOST" \
  -addext "subjectAltName=IP:$IP,DNS:$HOST,DNS:localhost,IP:127.0.0.1"
chmod 600 certs/robo.key
echo "Certificado criado em certs/robo.crt (válido para $IP e $HOST)."
