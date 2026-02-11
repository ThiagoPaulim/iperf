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
echo "f" > "/sys/class/net/$IFACE/queues/rx-0/rps_cpus" 2>/dev/null || true

# ---------- Policy routing vs VRF (fallback) ----------

IFINDEX=$(cat "/sys/class/net/$IFACE/ifindex" 2>/dev/null || echo "$PORT")
TABLE_ID=$((IFINDEX + 1000))
# Nome da VRF encurtado para evitar erro de validacao (limite de 15 chars no kernel).
VRF_NAME="v$IFINDEX"

# Tentativa de isolamento via VRF (metodo mais robusto).
USE_VRF=0
if ip link add "$VRF_NAME" type vrf table "$TABLE_ID" 2>/dev/null; then
  ip link set "$VRF_NAME" up 2>/dev/null
  if ip link set dev "$IFACE" master "$VRF_NAME" 2>/dev/null; then
    USE_VRF=1
    echo "DEBUG: Ativado isolamento via VRF $VRF_NAME" >&2
  else
    ip link del "$VRF_NAME" 2>/dev/null || true
  fi
fi

if [[ "$USE_VRF" -eq 0 ]]; then
  echo "DEBUG: VRF falhou. Usando policy routing." >&2
  RULE_PREF_FROM=$((20000 + IFINDEX))
  RULE_PREF_OIF=$((30000 + IFINDEX))
  ip rule del from "$BIND_IP" table "$TABLE_ID" 2>/dev/null || true
  ip rule del oif "$IFACE" table "$TABLE_ID" 2>/dev/null || true
  ip rule add from "$BIND_IP" table "$TABLE_ID" pref "$RULE_PREF_FROM"
  ip rule add oif "$IFACE" table "$TABLE_ID" pref "$RULE_PREF_OIF"
fi

cleanup() {
  local active_runners
  if command -v pgrep >/dev/null; then
    active_runners=$(pgrep -f "iperf-runner.sh $IFACE" | wc -l)
  else
    active_runners=$(ps aux | grep "iperf-runner.sh $IFACE" | grep -v grep | wc -l)
  fi

  if [[ "$active_runners" -le 1 ]]; then
    echo "DEBUG: Restaurando estado original de $IFACE..." >&2
    if [[ "$USE_VRF" -eq 1 ]]; then
      ip link set dev "$IFACE" nomaster 2>/dev/null || true
      ip link del "$VRF_NAME" 2>/dev/null || true
    fi
    ip rule del from "$BIND_IP" table "$TABLE_ID" 2>/dev/null || true
    ip rule del oif "$IFACE" table "$TABLE_ID" 2>/dev/null || true
    ip route flush table "$TABLE_ID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# Monta rotas na tabela dedicada (seja VRF ou tabela manual).
ip route flush table "$TABLE_ID" 2>/dev/null || true
GATEWAY=$(ip -4 route show dev "$IFACE" table main | awk '/default via/{print $3}' | head -n1)
[[ -z "$GATEWAY" ]] && GATEWAY=$(ip -4 route show dev "$IFACE" table main | awk '/via/{print $3}' | head -n1)

if [[ -n "$SUBNET" ]]; then
  ip route add "$SUBNET" dev "$IFACE" proto kernel scope link src "$BIND_IP" table "$TABLE_ID" 2>/dev/null || true
fi

if [[ -n "$GATEWAY" ]]; then
  ip route add default via "$GATEWAY" dev "$IFACE" table "$TABLE_ID" 2>/dev/null || true
else
  ip route add "$SERVER_IP" dev "$IFACE" table "$TABLE_ID" 2>/dev/null || true
fi

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

# Prefixamos com ip vrf exec quando VRF ativa.
EXEC_PREFIX=(taskset -c "$CORE_ID")
if [[ "$USE_VRF" -eq 1 ]]; then
  EXEC_PREFIX+=(ip vrf exec "$VRF_NAME")
fi

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

echo "DEBUG: Iniciando iperf3 no Core $CORE_ID (VRF: $USE_VRF)" >&2
"${CMD[@]}"
