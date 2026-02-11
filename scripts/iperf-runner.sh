#!/usr/bin/env bash
set -euo pipefail

# Script auxiliar para executar um fluxo iperf3 ligado a uma interface específica.
# Cria policy routing temporário para garantir que o tráfego saia pela interface correta.
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
if [[ ! "$PORT" =~ ^[0-9]+$ ]]; then
  echo "Porta inválida" >&2
  exit 2
fi
if [[ ! "$PARALLEL" =~ ^[0-9]+$ ]]; then
  echo "Parallel inválido" >&2
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

# ---------- Tunings de Hardware (NIC Otimização) ----------
# Aumenta os buffers de hardware da placa de rede para evitar perda de pacotes.

echo "Otimizando Ring Buffers e Offloading na interface $IFACE..." >&2

# Aumenta os buffers RX/TX para o máximo suportado pelo hardware
ethtool -G "$IFACE" rx 4096 tx 4096 2>/dev/null || true
# Desabilita o Adaptive-RX para reduzir jitter em testes de throughput
ethtool -C "$IFACE" adaptive-rx off 2>/dev/null || true
# Garante que as interrupções de hardware não fiquem presas em um só core (RPS)
echo "f" > "/sys/class/net/$IFACE/queues/rx-0/rps_cpus" 2>/dev/null || true

# ---------- Policy Routing vs VRF (Fallback de Decisao) ----------

IFINDEX=$(cat "/sys/class/net/$IFACE/ifindex" 2>/dev/null || echo "$PORT")
TABLE_ID=$((IFINDEX + 1000))
# Nome da VRF encurtado para evitar erro de validação (limite de 15 chars no kernel)
VRF_NAME="v$IFINDEX"

# Tentativa de isolamento via VRF (Método mais robusto)
USE_VRF=0
if ip link add "$VRF_NAME" type vrf table "$TABLE_ID" 2>/dev/null; then
    ip link set "$VRF_NAME" up 2>/dev/null
    if ip link set dev "$IFACE" master "$VRF_NAME" 2>/dev/null; then
        USE_VRF=1
        echo "DEBUG: Ativado isolamento via VRF $VRF_NAME" >&2
    else
        ip link del "$VRF_NAME" 2>/dev/null
    fi
fi

if [[ "$USE_VRF" -eq 0 ]]; then
    echo "DEBUG: VRF falhou (Attribute Validation). Usando Policy Routing + FWMARK..." >&2
    # Fallback para Policy Routing
    ip rule del from "$BIND_IP" table "$TABLE_ID" 2>/dev/null || true
    ip rule del oif "$IFACE" table "$TABLE_ID" 2>/dev/null || true
    ip rule add from "$BIND_IP" table "$TABLE_ID" pref 1000
    ip rule add oif "$IFACE" table "$TABLE_ID" pref 1001
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

# Monta rotas na tabela dedicada (seja VRF ou Tabela Manual)
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

# ---------- Tunings de Alta Performance (Rede) ----------
sysctl -w net.core.rmem_max=33554432 >/dev/null 2>&1 || true
sysctl -w net.core.wmem_max=33554432 >/dev/null 2>&1 || true
sysctl -w net.ipv4.tcp_rmem="4096 87380 33554432" >/dev/null 2>&1 || true
sysctl -w net.ipv4.tcp_wmem="4096 65536 33554432" >/dev/null 2>&1 || true

# ---------- Execução com Afinidade e VRF (Exec Context) ----------
NUM_CPUS=$(nproc)
CORE_ID=$(( (TABLE_ID - 1000) % NUM_CPUS ))

# Prefixamos o comando com 'ip vrf exec' se a VRF estiver ativa, 
# caso contrário usamos apenas taskset.
EXEC_PREFIX=(taskset -c "$CORE_ID")
if [[ "$USE_VRF" -eq 1 ]]; then
    EXEC_PREFIX+=(ip vrf exec "$VRF_NAME")
fi

CMD=("${EXEC_PREFIX[@]}" iperf3 -c "$SERVER_IP" -t "$DURATION" -i 1 -f m -B "$BIND_IP" -p "$PORT" -P "$PARALLEL" --forceflush)
if [[ "$MODE" == "download" ]]; then
  CMD+=( -R )
fi

echo "DEBUG: Iniciando iperf3 no Core $CORE_ID (VRF: $USE_VRF)" >&2
"${CMD[@]}"

RULES_CREATED=1

# Verifica qual interface o Kernel decidiu usar para este destino+origem
ROUTE_DEBUG=$(ip route get "$SERVER_IP" from "$BIND_IP" 2>/dev/null || echo "Erro ao verificar rota final")
echo "DEBUG_ROUTE: $ROUTE_DEBUG" >&2

# ---------- Métricas Iniciais (Ping) ----------

echo "Coletando métricas..." >&2

# 1. Ping (Latência)
# Envia 3 pings rápidos. Pega a média (campo 2 da linha rtt).
PING_AVG=$(ping -4 -c 3 -i 0.2 -W 1 -I "$BIND_IP" "$SERVER_IP" | tail -1 | awk -F '/' '{print $5}' 2>/dev/null || echo "0")

# Formata saída para o Python capturar
echo "[METRICS] Ping: $PING_AVG ms"

# ---------- Execução do iperf3 (Teste Principal) ----------

# Adicionamos -P (parallel) e --forceflush
# Também adicionamos -w 1M (janela TCP) por padrão para ajudar performance
# Removemos o limite de janela (-w 1M) para permitir o auto-tuning do Kernel/iperf3,
# essencial para atingir velocidades acima de 1Gbps em redes de alta performance.
CMD=(iperf3 -c "$SERVER_IP" -t "$DURATION" -i 1 -f m -B "$BIND_IP" -p "$PORT" -P "$PARALLEL" --forceflush)
if [[ "$MODE" == "download" ]]; then
  CMD+=( -R )
fi

# Execução sem shell expansion adicional para evitar command injection.
"${CMD[@]}"
