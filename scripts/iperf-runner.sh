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
RUNNER_REV="2026-02-11-r6"
ENABLE_NIC_TUNING="${ENABLE_NIC_TUNING:-0}"
ENABLE_SYSCTL_TUNING="${ENABLE_SYSCTL_TUNING:-0}"
ENABLE_POLICY_ROUTING="${ENABLE_POLICY_ROUTING:-1}"

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

echo "Iniciando runner na interface $IFACE... (rev: $RUNNER_REV)" >&2

# ---------- Tunings de hardware (NIC) ----------
# Desativado por padrao para evitar erros em ambientes com /sys read-only.
# Ative explicitamente com ENABLE_NIC_TUNING=1 se precisar.
if [[ "$ENABLE_NIC_TUNING" == "1" ]]; then
  echo "Aplicando tunings de NIC em $IFACE..." >&2
  ethtool -G "$IFACE" rx 4096 tx 4096 2>/dev/null || true
  ethtool -C "$IFACE" adaptive-rx off 2>/dev/null || true
  RPS_FILE="/sys/class/net/$IFACE/queues/rx-0/rps_cpus"
  if [[ -e "$RPS_FILE" ]]; then
    printf "f\n" | tee "$RPS_FILE" >/dev/null 2>/dev/null || true
  fi
else
  echo "Tunings de NIC desativados (ENABLE_NIC_TUNING=0)." >&2
fi

# ---------- Policy routing por interface (opcional) ----------

IFINDEX=$(cat "/sys/class/net/$IFACE/ifindex" 2>/dev/null || echo "$PORT")
TABLE_ID=$((IFINDEX + 1000))
RULE_PREF_FROM=$((20000 + IFINDEX))
LOCK_FILE="/tmp/iperf-runner-${IFACE}.lock"
COUNT_FILE="/tmp/iperf-runner-${IFACE}.count"
GATEWAY=$(ip -4 route show dev "$IFACE" table main | awk '/default via/{print $3}' | head -n1)
[[ -z "$GATEWAY" ]] && GATEWAY=$(ip -4 route show dev "$IFACE" table main | awk '/via/{print $3}' | head -n1)
ROUTE_HINT=$(ip -4 route get "$SERVER_IP" oif "$IFACE" 2>/dev/null | head -n1 || true)
HINT_GATEWAY=$(awk '{for(i=1;i<=NF;i++) if($i=="via"){print $(i+1); exit}}' <<<"$ROUTE_HINT")
if [[ -n "$HINT_GATEWAY" ]]; then
  GATEWAY="$HINT_GATEWAY"
fi

cleanup() {
  if [[ "$ENABLE_POLICY_ROUTING" != "1" ]]; then
    return
  fi

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
trap cleanup EXIT INT TERM

if [[ "$ENABLE_POLICY_ROUTING" == "1" ]]; then
  exec 9>"$LOCK_FILE"
  flock 9
  ACTIVE_COUNT=0
  if [[ -f "$COUNT_FILE" ]]; then
    # Se sobrou contador antigo e nao existe outro runner da interface, reseta estado.
    if [[ "$(pgrep -fc "iperf-runner.sh $IFACE" || true)" -le 1 ]]; then
      rm -f "$COUNT_FILE"
    fi
  fi
  if [[ -f "$COUNT_FILE" ]]; then
    ACTIVE_COUNT=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
  fi
  ACTIVE_COUNT=$((ACTIVE_COUNT + 1))
  echo "$ACTIVE_COUNT" > "$COUNT_FILE"

  # Primeiro fluxo da interface prepara tabela/rotas do zero (evita "File exists" residual).
  if [[ "$ACTIVE_COUNT" -eq 1 ]]; then
    ip rule del pref "$RULE_PREF_FROM" 2>/dev/null || true
    ip route flush table "$TABLE_ID" 2>/dev/null || true
    ip rule add from "$BIND_IP" table "$TABLE_ID" pref "$RULE_PREF_FROM" 2>/dev/null || true
  fi

  # Monta rotas na tabela dedicada.
  if [[ -n "$SUBNET" ]]; then
    ip route replace "$SUBNET" dev "$IFACE" scope link src "$BIND_IP" table "$TABLE_ID" 2>/dev/null || true
  fi

  if [[ -n "$GATEWAY" ]]; then
    ip route replace "$SERVER_IP"/32 via "$GATEWAY" dev "$IFACE" table "$TABLE_ID" 2>/dev/null || true
    ip route replace default via "$GATEWAY" dev "$IFACE" table "$TABLE_ID" 2>/dev/null || true
  else
    ip route replace "$SERVER_IP" dev "$IFACE" table "$TABLE_ID" 2>/dev/null || true
  fi

  flock -u 9
else
  echo "Policy routing desativado (ENABLE_POLICY_ROUTING=0)." >&2
fi

# ---------- Tunings de alta performance (rede) ----------
# Desativado por padrao para evitar alteracoes globais no host a cada fluxo.
if [[ "$ENABLE_SYSCTL_TUNING" == "1" ]]; then
  sysctl -w net.core.rmem_max=33554432 >/dev/null 2>&1 || true
  sysctl -w net.core.wmem_max=33554432 >/dev/null 2>&1 || true
  sysctl -w net.ipv4.tcp_rmem="4096 87380 33554432" >/dev/null 2>&1 || true
  sysctl -w net.ipv4.tcp_wmem="4096 65536 33554432" >/dev/null 2>&1 || true
fi

# ---------- Metricas iniciais (ping) ----------
echo "Coletando metricas..." >&2
PING_AVG=$(ping -4 -c 3 -i 0.2 -W 1 -I "$BIND_IP" "$SERVER_IP" | tail -1 | awk -F '/' '{print $5}' 2>/dev/null || echo "0")
echo "[METRICS] Ping: $PING_AVG ms"

# ---------- Execucao do iperf3 ----------
ROUTE_DEBUG=$(ip route get "$SERVER_IP" from "$BIND_IP" 2>/dev/null || echo "Erro ao verificar rota final")
echo "DEBUG_ROUTE: $ROUTE_DEBUG" >&2
if [[ "$ROUTE_DEBUG" == "Erro ao verificar rota final" || "$ROUTE_DEBUG" == *"unreachable"* || "$ROUTE_DEBUG" == *"prohibit"* ]]; then
  echo "Sem rota valida para $SERVER_IP a partir de $BIND_IP na interface $IFACE." >&2
  exit 1
fi
if [[ "$ENABLE_POLICY_ROUTING" == "1" && "$ROUTE_DEBUG" != *" dev $IFACE "* ]]; then
  echo "Rota final nao saiu por $IFACE: $ROUTE_DEBUG" >&2
  exit 1
fi

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

# Evita travamentos longos de conexao em interfaces sem alcance.
if iperf3 --help 2>&1 | grep -q -- "--connect-timeout"; then
  CMD+=( --connect-timeout 5000 )
fi

echo "DEBUG: Iniciando iperf3 no Core $CORE_ID (policy table: $TABLE_ID)" >&2
RUN_TIMEOUT=$((DURATION + 25))
if command -v timeout >/dev/null 2>&1; then
  timeout --foreground --signal=TERM "$RUN_TIMEOUT" "${CMD[@]}"
else
  "${CMD[@]}"
fi
