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
RUNNER_REV="2026-02-12-r14"
ENABLE_SYSCTL_TUNING="${ENABLE_SYSCTL_TUNING:-0}"
ENABLE_POLICY_ROUTING="${ENABLE_POLICY_ROUTING:-1}"
ENABLE_MULTIHOME_TUNING="${ENABLE_MULTIHOME_TUNING:-1}"
CONNECT_TIMEOUT_MS="${CONNECT_TIMEOUT_MS:-12000}"
PRECHECK_TIMEOUT_S="${PRECHECK_TIMEOUT_S:-3.0}"

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
if [[ ! "$CONNECT_TIMEOUT_MS" =~ ^[0-9]+$ ]]; then
  CONNECT_TIMEOUT_MS=12000
fi

tune_multihome_iface() {
  if [[ "$ENABLE_MULTIHOME_TUNING" != "1" ]]; then
    return
  fi
  # Ambientes multihomed com varias NICs na mesma faixa podem sofrer ARP flux/rp_filter.
  # Ajustes abaixo reduzem timeout intermitente por interface.
  sysctl -w "net.ipv4.conf.${IFACE}.rp_filter=2" >/dev/null 2>&1 || true
  sysctl -w "net.ipv4.conf.${IFACE}.arp_ignore=1" >/dev/null 2>&1 || true
  sysctl -w "net.ipv4.conf.${IFACE}.arp_announce=2" >/dev/null 2>&1 || true
  sysctl -w "net.ipv4.conf.${IFACE}.arp_filter=1" >/dev/null 2>&1 || true
}

# ---------- Coleta de informacoes da interface ----------

BIND_IP=$(ip -4 -o addr show dev "$IFACE" | awk '{print $4}' | cut -d/ -f1 | head -n1)
if [[ -z "$BIND_IP" ]]; then
  echo "Interface $IFACE nao possui IPv4 valido para bind" >&2
  exit 1
fi

# Obtem a subnet/prefixo da interface.
SUBNET=$(ip -4 -o addr show dev "$IFACE" | awk '{print $4}' | head -n1)
SERVER_IN_SUBNET=0
if python - "$SUBNET" "$SERVER_IP" <<'PY'
import ipaddress, sys
subnet, server_ip = sys.argv[1], sys.argv[2]
try:
    net = ipaddress.ip_network(subnet, strict=False)
    ip = ipaddress.ip_address(server_ip)
    sys.exit(0 if ip in net else 1)
except Exception:
    sys.exit(1)
PY
then
  SERVER_IN_SUBNET=1
fi

echo "Iniciando runner na interface $IFACE... (rev: $RUNNER_REV)" >&2

# ---------- Tunings de hardware (NIC) ----------
# Desativado de forma definitiva para eliminar erros em /sys read-only.
echo "Tunings de NIC desativados (fixo)." >&2

# ---------- Policy routing por interface (opcional) ----------

IFINDEX=$(cat "/sys/class/net/$IFACE/ifindex" 2>/dev/null || echo "$PORT")
TABLE_ID=$((IFINDEX + 1000))
RULE_PREF_FROM=$((20000 + IFINDEX))
LOCK_FILE="/tmp/iperf-runner-${IFACE}.lock"
COUNT_FILE="/tmp/iperf-runner-${IFACE}.count"
LOCK_READY=0
GATEWAY=$(ip -4 route show dev "$IFACE" table main | awk '/default via/{print $3}' | head -n1)
[[ -z "$GATEWAY" ]] && GATEWAY=$(ip -4 route show dev "$IFACE" table main | awk '/via/{print $3}' | head -n1)
ROUTE_HINT=$(ip -4 route get "$SERVER_IP" oif "$IFACE" 2>/dev/null | head -n1 || true)
HINT_GATEWAY=$(awk '{for(i=1;i<=NF;i++) if($i=="via"){print $(i+1); exit}}' <<<"$ROUTE_HINT")
if [[ -n "$HINT_GATEWAY" ]]; then
  GATEWAY="$HINT_GATEWAY"
fi

cleanup() {
  if [[ "$ENABLE_POLICY_ROUTING" != "1" || "$LOCK_READY" != "1" ]]; then
    return
  fi

  flock 9
  local active_count
  active_count=0
  if [[ -f "$COUNT_FILE" ]]; then
    active_count=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
  fi
  [[ "$active_count" =~ ^[0-9]+$ ]] || active_count=0
  if [[ "$active_count" -gt 0 ]]; then
    active_count=$((active_count - 1))
  fi

  if [[ "$active_count" -le 0 ]]; then
    rm -f "$COUNT_FILE"
    echo "DEBUG: Restaurando estado original de $IFACE..." >&2
    BEFORE_RULES=$(ip rule show 2>/dev/null | awk -v p="$RULE_PREF_FROM" '$1 ~ ("^" p ":") {c++} END {print c+0}')
    while ip rule del pref "$RULE_PREF_FROM" 2>/dev/null; do :; done
    ip route flush table "$TABLE_ID" 2>/dev/null || true
    AFTER_RULES=$(ip rule show 2>/dev/null | awk -v p="$RULE_PREF_FROM" '$1 ~ ("^" p ":") {c++} END {print c+0}')
    echo "DEBUG: policy cleanup iface=$IFACE pref=$RULE_PREF_FROM rules_before=$BEFORE_RULES rules_after=$AFTER_RULES table=$TABLE_ID" >&2
  else
    echo "$active_count" > "$COUNT_FILE"
  fi
  flock -u 9
}
trap cleanup EXIT INT TERM

if [[ "$ENABLE_POLICY_ROUTING" == "1" ]]; then
  exec 9>"$LOCK_FILE"
  LOCK_READY=1
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
  [[ "$ACTIVE_COUNT" =~ ^[0-9]+$ ]] || ACTIVE_COUNT=0
  ACTIVE_COUNT=$((ACTIVE_COUNT + 1))
  echo "$ACTIVE_COUNT" > "$COUNT_FILE"

  # Primeiro fluxo da interface prepara tabela/rotas do zero (evita "File exists" residual).
  if [[ "$ACTIVE_COUNT" -eq 1 ]]; then
    tune_multihome_iface
    BEFORE_RULES=$(ip rule show 2>/dev/null | awk -v p="$RULE_PREF_FROM" '$1 ~ ("^" p ":") {c++} END {print c+0}')
    while ip rule del pref "$RULE_PREF_FROM" 2>/dev/null; do :; done
    ip route flush table "$TABLE_ID" 2>/dev/null || true
    if ! ip rule show 2>/dev/null | awk -v p="$RULE_PREF_FROM" '$1 ~ ("^" p ":") {found=1} END {exit(found?0:1)}'; then
      ip rule add from "$BIND_IP" table "$TABLE_ID" pref "$RULE_PREF_FROM" 2>/dev/null || true
    fi
    AFTER_RULES=$(ip rule show 2>/dev/null | awk -v p="$RULE_PREF_FROM" '$1 ~ ("^" p ":") {c++} END {print c+0}')
    echo "DEBUG: policy setup iface=$IFACE pref=$RULE_PREF_FROM rules_before=$BEFORE_RULES rules_after=$AFTER_RULES table=$TABLE_ID src=$BIND_IP" >&2
  fi

  # Monta rotas na tabela dedicada.
  if [[ -n "$SUBNET" ]]; then
    ip route replace "$SUBNET" dev "$IFACE" scope link src "$BIND_IP" table "$TABLE_ID" 2>/dev/null || true
  fi

  if [[ "$SERVER_IN_SUBNET" == "1" ]]; then
    # Quando servidor e interface estao na mesma faixa, evita forcar via gateway.
    ip route replace "$SERVER_IP"/32 dev "$IFACE" scope link src "$BIND_IP" table "$TABLE_ID" 2>/dev/null || true
    if [[ -n "$GATEWAY" ]]; then
      ip route replace default via "$GATEWAY" dev "$IFACE" table "$TABLE_ID" 2>/dev/null || true
    fi
  elif [[ -n "$GATEWAY" ]]; then
    ip route replace "$SERVER_IP"/32 via "$GATEWAY" dev "$IFACE" table "$TABLE_ID" 2>/dev/null || true
    ip route replace default via "$GATEWAY" dev "$IFACE" table "$TABLE_ID" 2>/dev/null || true
  else
    ip route replace "$SERVER_IP" dev "$IFACE" table "$TABLE_ID" 2>/dev/null || true
  fi

  echo "DEBUG: same_subnet=$SERVER_IN_SUBNET subnet=$SUBNET gateway=${GATEWAY:-none}" >&2

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
PING_AVG=$(ping -4 -c 3 -i 0.2 -W 1 -I "$BIND_IP" "$SERVER_IP" | tail -1 | awk -F '/' '{print $5}' 2>/dev/null || echo "N/A")
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

# ---------- Preflight TCP ----------
# Verifica rapidamente se a porta remota responde a partir do IP da interface.
PRECHECK_OK=0
for attempt in 1 2 3 4 5; do
  if python - "$BIND_IP" "$SERVER_IP" "$PORT" "$PRECHECK_TIMEOUT_S" <<'PY'
import socket, sys
bind_ip, server_ip, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
timeout_s = float(sys.argv[4])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(timeout_s)
try:
    s.bind((bind_ip, 0))
    s.connect((server_ip, port))
    print("ok")
except Exception as exc:
    print(f"precheck_error:{exc}", file=sys.stderr)
    sys.exit(1)
finally:
    s.close()
PY
  then
    PRECHECK_OK=1
    break
  fi
  sleep 0.4
done

if [[ "$PRECHECK_OK" -ne 1 ]]; then
  echo "Precheck TCP falhou para $SERVER_IP:$PORT a partir de $BIND_IP ($IFACE)." >&2
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
  CMD+=( --connect-timeout "$CONNECT_TIMEOUT_MS" )
fi

echo "DEBUG: Iniciando iperf3 no Core $CORE_ID (policy table: $TABLE_ID)" >&2
RUN_TIMEOUT=$((DURATION + 12))
if command -v timeout >/dev/null 2>&1; then
  timeout --foreground --signal=TERM "$RUN_TIMEOUT" "${CMD[@]}"
else
  "${CMD[@]}"
fi
