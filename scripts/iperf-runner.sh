#!/usr/bin/env bash
set -euo pipefail

# Script auxiliar para executar um fluxo iperf3 ligado a uma interface específica.
# Uso: iperf-runner.sh <interface> <server_ip> <duration_s> <upload|download>

if [[ $# -ne 4 ]]; then
  echo "Uso: $0 <interface> <server_ip> <duration_s> <upload|download>" >&2
  exit 2
fi

IFACE="$1"
SERVER_IP="$2"
DURATION="$3"
MODE="$4"

# Validação básica para evitar entradas não previstas.
if [[ ! "$IFACE" =~ ^[a-zA-Z0-9._:-]+$ ]]; then
  echo "Interface inválida" >&2
  exit 2
fi
if [[ ! "$SERVER_IP" =~ ^[0-9a-fA-F:.]+$ ]]; then
  echo "IP do servidor inválido" >&2
  exit 2
fi
if [[ ! "$DURATION" =~ ^[0-9]+$ ]]; then
  echo "Duração inválida" >&2
  exit 2
fi
if [[ "$MODE" != "upload" && "$MODE" != "download" ]]; then
  echo "Modo inválido" >&2
  exit 2
fi

BIND_IP=$(ip -4 -o addr show dev "$IFACE" | awk '{print $4}' | cut -d/ -f1 | head -n1)
if [[ -z "$BIND_IP" ]]; then
  echo "Interface $IFACE não possui IPv4 válido para bind" >&2
  exit 1
fi

CMD=(iperf3 -c "$SERVER_IP" -t "$DURATION" -i 1 -f m -B "$BIND_IP")
if [[ "$MODE" == "download" ]]; then
  CMD+=( -R )
fi

# Execução sem shell expansion adicional para evitar command injection.
exec "${CMD[@]}"
