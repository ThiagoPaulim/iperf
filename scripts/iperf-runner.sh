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

# ---------- Tunings de Hardware e Kernel (Extremos) ----------

echo "Aplicando tunings de hardware e isolamento VRF..." >&2

# 1. Otimiza filas de transmissão da interface para prevenir gargalos
ip link set dev "$IFACE" txqueuelen 10000 2>/dev/null || true

# 2. Tunings de processamento de pacotes (Kernel Softnet)
sysctl -w net.core.netdev_budget=600 >/dev/null 2>&1 || true
sysctl -w net.core.netdev_budget_usecs=8000 >/dev/null 2>&1 || true
sysctl -w net.ipv4.udp_rmem_min=16384 >/dev/null 2>&1 || true

# 3. Garante que o isolamento ARP seja absoluto
sysctl -w net.ipv4.conf.all.arp_ignore=1 >/dev/null 2>&1 || true
sysctl -w net.ipv4.conf."$IFACE".arp_ignore=1 >/dev/null 2>&1 || true
sysctl -w net.ipv4.conf.all.arp_announce=2 >/dev/null 2>&1 || true
sysctl -w net.ipv4.conf."$IFACE".arp_announce=2 >/dev/null 2>&1 || true

# ---------- Isolamento via VRF (Virtual Routing and Forwarding) ----------
# Esta é a técnica mais avançada para garantir que o tráfego NUNCA saia por outra interface,
# mesmo que estejam na mesma sub-rede e compartilhem o mesmo gateway.

VRF_NAME="vrf-$IFACE"
TABLE_ID=$(cat "/sys/class/net/$IFACE/ifindex" 2>/dev/null || echo "$PORT")
TABLE_ID=$((TABLE_ID + 1000))

# Limpeza e Criação da VRF
ip link del "$VRF_NAME" 2>/dev/null || true
ip link add "$VRF_NAME" type vrf table "$TABLE_ID"
ip link set "$VRF_NAME" up
ip link set dev "$IFACE" master "$VRF_NAME"

echo "DEBUG: Interface $IFACE vinculada à VRF $VRF_NAME (Tabela $TABLE_ID)" >&2

cleanup() {
  local active_runners
  if command -v pgrep >/dev/null; then
    active_runners=$(pgrep -f "iperf-runner.sh $IFACE" | wc -l)
  else
    active_runners=$(ps aux | grep "iperf-runner.sh $IFACE" | grep -v grep | wc -l)
  fi

  if [[ "$active_runners" -le 1 ]]; then
    echo "DEBUG: Restaurando interface $IFACE (Removendo VRF)..." >&2
    ip link set dev "$IFACE" nomaster 2>/dev/null || true
    ip link del "$VRF_NAME" 2>/dev/null || true
    ip route flush table "$TABLE_ID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# Obtém gateway oficial (agora deve ser inserido na tabela da VRF)
GATEWAY=$(ip -4 route show dev "$IFACE" table main | awk '/default via/{print $3}' | head -n1)
[[ -z "$GATEWAY" ]] && GATEWAY=$(ip -4 route show dev "$IFACE" table main | awk '/via/{print $3}' | head -n1)

# Monta rotas EXCLUSIVAS na VRF
ip route flush table "$TABLE_ID" 2>/dev/null || true
if [[ -n "$SUBNET" ]]; then
    ip route add "$SUBNET" dev "$IFACE" proto kernel scope link src "$BIND_IP" table "$TABLE_ID" 2>/dev/null || true
fi

if [[ -n "$GATEWAY" ]]; then
    ip route add default via "$GATEWAY" dev "$IFACE" table "$TABLE_ID" 2>/dev/null || true
else
    ip route add "$SERVER_IP" dev "$IFACE" table "$TABLE_ID" 2>/dev/null || true
fi

# ---------- Execução com CPU Pinning (Afinidade) ----------
# Em testes multi-interface, o iperf3 pode saturar um único núcleo da CPU.
# Distribuímos cada processo em um núcleo diferente.

# Pega o número de CPUs disponíveis
NUM_CPUS=$(nproc)
# Usa o ifindex para escolher um core de forma determinística
CORE_ID=$(( (TABLE_ID - 1000) % NUM_CPUS ))

echo "DEBUG: Executando no Core CPU $CORE_ID com Isolamento VRF" >&2

# Comando iperf3 forçado a usar o dispositivo de bind e VRF
# No iperf3 3.10+, podemos usar --bind-dev. Em outros, a VRF + -B resolve.
CMD=(taskset -c "$CORE_ID" iperf3 -c "$SERVER_IP" -t "$DURATION" -i 1 -f m -B "$BIND_IP" -p "$PORT" -P "$PARALLEL" --forceflush)
if [[ "$MODE" == "download" ]]; then
  CMD+=( -R )
fi

# Execução (O isolamento VRF agora é garantido pelo kernel via master interface)
"${CMD[@]}"

# Não precisamos de ip rule manual pois a VRF encapsula o roteamento do dispositivo.
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
