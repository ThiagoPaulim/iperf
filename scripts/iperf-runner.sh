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

# ---------- Tunings de Performance de Kernel (Rede de Alta Velocidade) ----------
# Estes tunings preparam o kernel para lidar com 2.5Gbps+ e múltiplas streams.

echo "Otimizando Kernel para Alta Performance..." >&2

# Aumenta os buffers de socket TCP (Essencial para > 1Gbps)
sysctl -w net.core.rmem_max=16777216 >/dev/null 2>&1 || true
sysctl -w net.core.wmem_max=16777216 >/dev/null 2>&1 || true
sysctl -w net.ipv4.tcp_rmem="4096 87380 16777216" >/dev/null 2>&1 || true
sysctl -w net.ipv4.tcp_wmem="4096 65536 16777216" >/dev/null 2>&1 || true

# Melhora a fila de pacotes e backlog
sysctl -w net.core.netdev_max_backlog=10000 >/dev/null 2>&1 || true
sysctl -w net.core.somaxconn=4096 >/dev/null 2>&1 || true

# Ativa BBR se disponível (Melhor algoritmo de congestionamento do Google)
if sysctl net.ipv4.tcp_congestion_control | grep -q "bbr"; then
    sysctl -w net.ipv4.tcp_congestion_control=bbr >/dev/null 2>&1 || true
fi

# Otimização de ARP para evitar lentidão em redes grandes
sysctl -w net.ipv4.neigh.default.gc_thresh1=1024 >/dev/null 2>&1 || true
sysctl -w net.ipv4.neigh.default.gc_thresh2=2048 >/dev/null 2>&1 || true
sysctl -w net.ipv4.neigh.default.gc_thresh3=4096 >/dev/null 2>&1 || true

# ---------- Tuning de Sysctl para Isolamento (Multi-Homing) ----------

# ARP Filter = 1: Essencial para interfaces na mesma sub-rede
sysctl -w net.ipv4.conf.all.arp_filter=1 >/dev/null 2>&1 || true
sysctl -w net.ipv4.conf."$IFACE".arp_filter=1 >/dev/null 2>&1 || true
sysctl -w net.ipv4.conf."$IFACE".rp_filter=0 >/dev/null 2>&1 || true
sysctl -w net.ipv4.conf.all.rp_filter=0 >/dev/null 2>&1 || true

# --------------------------------------------------------

# Tenta descobrir o gateway.
GATEWAY=$(ip -4 route show dev "$IFACE" default 2>/dev/null | awk '/default/{print $3}' | head -n1)
[[ -z "$GATEWAY" ]] && GATEWAY=$(ip -4 route show dev "$IFACE" | awk '/via/{print $3}' | head -n1)

# ---------- Policy routing (Isolamento de Interface via FWMARK) ----------

IFINDEX=$(cat "/sys/class/net/$IFACE/ifindex" 2>/dev/null || echo "$PORT")
TABLE_ID=$((IFINDEX + 1000))
MARK_ID=$TABLE_ID

echo "DEBUG: Configurando Tabela $TABLE_ID e Mark $MARK_ID para $IFACE (IP: $BIND_IP)" >&2

cleanup() {
  local active_runners
  if command -v pgrep >/dev/null; then
    active_runners=$(pgrep -f "iperf-runner.sh $IFACE" | wc -l)
  else
    active_runners=$(ps aux | grep "iperf-runner.sh $IFACE" | grep -v grep | wc -l)
  fi

  if [[ "$active_runners" -le 1 ]]; then
    echo "DEBUG: Liberando interface $IFACE..." >&2
    ip rule del fwmark "$MARK_ID" table "$TABLE_ID" 2>/dev/null || true
    ip rule del from "$BIND_IP" table "$TABLE_ID" 2>/dev/null || true
    ip rule del oif "$IFACE" table "$TABLE_ID" 2>/dev/null || true
    ip route flush table "$TABLE_ID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# Limpa estado anterior (Garante isolamento)
ip rule del fwmark "$MARK_ID" table "$TABLE_ID" 2>/dev/null || true
ip rule del from "$BIND_IP" table "$TABLE_ID" 2>/dev/null || true
ip rule del oif "$IFACE" table "$TABLE_ID" 2>/dev/null || true
ip route flush table "$TABLE_ID" 2>/dev/null || true

# 1. Rota Local e Gateway
if [[ -n "$SUBNET" ]]; then
    ip route add "$SUBNET" dev "$IFACE" proto kernel scope link src "$BIND_IP" table "$TABLE_ID" 2>/dev/null || true
fi

if [[ -n "$GATEWAY" ]]; then
    ip route add default via "$GATEWAY" dev "$IFACE" src "$BIND_IP" table "$TABLE_ID" 2>/dev/null || true
else
    ip route add "$SERVER_IP" dev "$IFACE" scope link src "$BIND_IP" table "$TABLE_ID" 2>/dev/null || true
fi

# 2. Regras de Política Triplas (Cinturão e Suspensório)
# - Baseado no IP de origem
# - Baseado na Interface de saída
# - Baseado no FW Mark (Caso o kernel tente trocar a interface depois do bind)
ip rule add from "$BIND_IP" table "$TABLE_ID" pref 1000
ip rule add oif "$IFACE" table "$TABLE_ID" pref 1001
ip rule add fwmark "$MARK_ID" table "$TABLE_ID" pref 1002

# 3. Força flush do cache
ip route flush cache 2>/dev/null || true

# DEBUG FINAL
echo "--- TABELA $TABLE_ID ($IFACE) ---" >&2
ip route show table "$TABLE_ID" >&2
echo "---------------------------------" >&2

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
