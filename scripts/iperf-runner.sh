#!/usr/bin/env bash
set -euo pipefail

# Script auxiliar para executar um fluxo iperf3 ligado a uma interface especifica.
# Cria policy routing temporario para garantir que o trafego saia pela interface correta.
# Uso: iperf-runner.sh <interface> <server_ip> <duration_s> <upload|download> <port> <parallel>

if [[ $# -ne 6 ]]; then
  echo "Uso: $0 <interface> <server_ip> <duration_s> <upload|download> <port> <parallel>" >&2
  exit 2
fi

IFACE="$1"
SERVER_IP="$2"
DURATION="$3"
MODE="$4"
PORT="$5"
PARALLEL="$6"

# Validacao basica para evitar entradas nao previstas.
if [[ ! "$IFACE" =~ ^[a-zA-Z0-9._:-]+$ ]]; then
  echo "Interface invalida" >&2
  exit 2
fi
if [[ ! "$SERVER_IP" =~ ^[0-9a-fA-F:.]+$ ]]; then
  echo "IP do servidor invalido" >&2
  exit 2
fi
if [[ ! "$DURATION" =~ ^[0-9]+$ ]]; then
  echo "Duracao invalida" >&2
  exit 2
fi
if [[ "$MODE" != "upload" && "$MODE" != "download" ]]; then
  echo "Modo invalido" >&2
  exit 2
fi
if [[ ! "$PORT" =~ ^[0-9]+$ ]]; then
  echo "Porta invalida" >&2
  exit 2
fi
if [[ ! "$PARALLEL" =~ ^[0-9]+$ ]]; then
  echo "Parallel invalido" >&2
  exit 2
fi

# ---------- Coleta de informacoes da interface ----------

BIND_IP=$(ip -4 -o addr show dev "$IFACE" | awk '{print $4}' | cut -d/ -f1 | head -n1)
if [[ -z "$BIND_IP" ]]; then
  echo "Interface $IFACE nao possui IPv4 valido para bind" >&2
  exit 1
fi

# Obtem a subnet/prefixo da interface.
SUBNET=$(ip -4 -o addr show dev "$IFACE" | awk '{print $4}' | head -n1)

# ---------- Tunings de hardware (NIC) ----------

echo "Otimizando Ring Buffers e Offloading na interface $IFACE..." >&2

# Aumenta os buffers RX/TX para o maximo suportado pelo hardware.
ethtool -G "$IFACE" rx 4096 tx 4096 2>/dev/null || true
# Desabilita o Adaptive-RX para reduzir jitter em testes de throughput.
ethtool -C "$IFACE" adaptive-rx off 2>/dev/null || true
# Garante que as interrupcoes de hardware nao fiquem presas em um so core (RPS).
RPS_FILE="/sys/class/net/$IFACE/queues/rx-0/rps_cpus"
if [[ -e "$RPS_FILE" ]]; then
  # Em alguns ambientes o sysfs e read-only; suprimimos erro de redirecionamento do shell.
  { echo "f" > "$RPS_FILE"; } 2>/dev/null || true
fi

# ---------- Policy routing por interface ----------

IFINDEX=$(cat "/sys/class/net/$IFACE/ifindex" 2>/dev/null || echo "$PORT")
TABLE_ID=$((IFINDEX + 1000))
RULE_PREF_FROM=$((20000 + IFINDEX))
LOCK_FILE="/tmp/iperf-runner-${IFACE}.lock"
COUNT_FILE="/tmp/iperf-runner-${IFACE}.count"

exec 9>"$LOCK_FILE"
flock 9
ACTIVE_COUNT=0
if [[ -f "$COUNT_FILE" ]]; then
  ACTIVE_COUNT=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
fi
ACTIVE_COUNT=$((ACTIVE_COUNT + 1))
echo "$ACTIVE_COUNT" > "$COUNT_FILE"

# Evita recriar regras a cada fluxo e reduz interferencia entre uploads/downloads simultaneos.
if ! ip rule show | grep -q "^${RULE_PREF_FROM}:"; then
  ip rule add from "$BIND_IP" table "$TABLE_ID" pref "$RULE_PREF_FROM" 2>/dev/null || true
fi

cleanup() {
  flock 9
  local active_count
  active_count=0
  if [[ -f "$COUNT_FILE" ]]; then
    active_count=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
  fi
  if [[ "$active_count" -gt 0 ]]; then
    active_count=$((active_count - 1))
  fi

  if [[ "$active_count" -le 0 ]]; then
    rm -f "$COUNT_FILE"
    echo "DEBUG: Restaurando estado original de $IFACE..." >&2
    ip rule del pref "$RULE_PREF_FROM" 2>/dev/null || true
    ip route flush table "$TABLE_ID" 2>/dev/null || true
  else
    echo "$active_count" > "$COUNT_FILE"
  fi
  flock -u 9
}
trap cleanup EXIT

# Monta rotas na tabela dedicada.
GATEWAY=$(ip -4 route show dev "$IFACE" table main | awk '/default via/{print $3}' | head -n1)
[[ -z "$GATEWAY" ]] && GATEWAY=$(ip -4 route show dev "$IFACE" table main | awk '/via/{print $3}' | head -n1)

if [[ -n "$SUBNET" ]]; then
  ip route replace "$SUBNET" dev "$IFACE" proto kernel scope link src "$BIND_IP" table "$TABLE_ID" 2>/dev/null || true
fi

if [[ -n "$GATEWAY" ]]; then
  ip route replace "$SERVER_IP"/32 via "$GATEWAY" dev "$IFACE" table "$TABLE_ID" 2>/dev/null || true
  ip route replace default via "$GATEWAY" dev "$IFACE" table "$TABLE_ID" 2>/dev/null || true
else
  ip route replace "$SERVER_IP" dev "$IFACE" table "$TABLE_ID" 2>/dev/null || true
fi

flock -u 9

# ---------- Tunings de alta performance (rede) ----------
sysctl -w net.core.rmem_max=33554432 >/dev/null 2>&1 || true
sysctl -w net.core.wmem_max=33554432 >/dev/null 2>&1 || true
sysctl -w net.ipv4.tcp_rmem="4096 87380 33554432" >/dev/null 2>&1 || true
sysctl -w net.ipv4.tcp_wmem="4096 65536 33554432" >/dev/null 2>&1 || true

# ---------- Metricas iniciais (ping) ----------
echo "Coletando metricas..." >&2
PING_AVG=$(ping -4 -c 3 -i 0.2 -W 1 -I "$BIND_IP" "$SERVER_IP" | tail -1 | awk -F '/' '{print $5}' 2>/dev/null || echo "0")
echo "[METRICS] Ping: $PING_AVG ms"

# ---------- Execucao do iperf3 ----------
ROUTE_DEBUG=$(ip route get "$SERVER_IP" from "$BIND_IP" 2>/dev/null || echo "Erro ao verificar rota final")
echo "DEBUG_ROUTE: $ROUTE_DEBUG" >&2

NUM_CPUS=$(nproc)
CORE_ID=$(( (IFINDEX + PORT) % NUM_CPUS ))

EXEC_PREFIX=(taskset -c "$CORE_ID")

# --bind-dev reduz ambiguidade de roteamento quando ha interfaces em sub-redes similares.
BIND_DEV_ARGS=()
if iperf3 --help 2>&1 | grep -q -- "--bind-dev"; then
  BIND_DEV_ARGS=(--bind-dev "$IFACE")
fi

CMD=(
  "${EXEC_PREFIX[@]}"
  iperf3
  -c "$SERVER_IP"
  -t "$DURATION"
  -i 1
  -f m
  -B "$BIND_IP"
  "${BIND_DEV_ARGS[@]}"
  -p "$PORT"
  -P "$PARALLEL"
  --forceflush
)

if [[ "$MODE" == "download" ]]; then
  CMD+=( -R )
fi

echo "DEBUG: Iniciando iperf3 no Core $CORE_ID (policy table: $TABLE_ID)" >&2
"${CMD[@]}"
