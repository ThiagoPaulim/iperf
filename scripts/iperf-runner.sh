#!/usr/bin/env bash
set -euo pipefail

# Script auxiliar para executar um fluxo iperf3 ligado a uma interface específica.
# Cria policy routing temporário para garantir que o tráfego saia pela interface correta.
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

# ---------- Coleta de informações da interface ----------

BIND_IP=$(ip -4 -o addr show dev "$IFACE" | awk '{print $4}' | cut -d/ -f1 | head -n1)
if [[ -z "$BIND_IP" ]]; then
  echo "Interface $IFACE não possui IPv4 válido para bind" >&2
  exit 1
fi

# Obtém a subnet/prefixo da interface.
SUBNET=$(ip -4 -o addr show dev "$IFACE" | awk '{print $4}' | head -n1)

# Tenta descobrir o gateway da interface.
# Primeiro tenta a rota default especifica para este device.
GATEWAY=$(ip -4 route show dev "$IFACE" default 2>/dev/null | awk '/default/{print $3}' | head -n1)

# Se não encontrar, tenta o gateway da rota default geral.
if [[ -z "$GATEWAY" ]]; then
  GATEWAY=$(ip -4 route show default | awk '/default/{print $3}' | head -n1)
fi

# Se ainda não encontrar, tenta pegar o gateway via a subnet da interface.
if [[ -z "$GATEWAY" ]]; then
  GATEWAY=$(ip -4 route show dev "$IFACE" | awk '/via/{print $3}' | head -n1)
fi

# ---------- Policy routing ----------

# Gera um table ID baseado no hash do nome da interface (100-250 range).
TABLE_ID=$(( ( $(echo "$IFACE" | cksum | awk '{print $1}') % 150 ) + 100 ))

# Flag para controlar se criamos as regras (para cleanup).
RULES_CREATED=0

cleanup() {
  if [[ "$RULES_CREATED" -eq 1 ]]; then
    # Remove as regras de policy routing criadas.
    ip rule del from "$BIND_IP" table "$TABLE_ID" 2>/dev/null || true
    ip route flush table "$TABLE_ID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# Só cria policy routing se encontrou um gateway.
if [[ -n "$GATEWAY" ]]; then
  # Remove regras antigas que possam existir para este IP/tabela.
  ip rule del from "$BIND_IP" table "$TABLE_ID" 2>/dev/null || true
  ip route flush table "$TABLE_ID" 2>/dev/null || true

  # Adiciona rota na tabela dedicada.
  ip route add default via "$GATEWAY" dev "$IFACE" table "$TABLE_ID" 2>/dev/null || true

  # Adiciona rota da subnet local na tabela dedicada.
  if [[ -n "$SUBNET" ]]; then
    ip route add "$SUBNET" dev "$IFACE" scope link table "$TABLE_ID" 2>/dev/null || true
  fi

  # Adiciona regra: tráfego com source IP = BIND_IP usa a tabela dedicada.
  ip rule add from "$BIND_IP" table "$TABLE_ID" 2>/dev/null || true

  RULES_CREATED=1

  echo "Policy routing criado: from $BIND_IP table $TABLE_ID via $GATEWAY dev $IFACE" >&2
else
  echo "AVISO: Gateway não encontrado para $IFACE. Usando roteamento padrão." >&2
fi

# ---------- Execução do iperf3 ----------

CMD=(iperf3 -c "$SERVER_IP" -t "$DURATION" -i 1 -f m -B "$BIND_IP")
if [[ "$MODE" == "download" ]]; then
  CMD+=( -R )
fi

# Execução sem shell expansion adicional para evitar command injection.
"${CMD[@]}"
