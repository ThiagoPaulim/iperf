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

# ---------- Tuning de Sysctl para Multi-Homing ----------
# Essencial para impedir que o tráfego "vaze" para a interface errada.

echo "Aplicando sysctl tunings para $IFACE..."

# ARP Filter = 1: Usa a tabela de roteamento para determinar se deve responder ao ARP.
# Fundamental para quando múltiplas interfaces estão na mesma sub-rede.
sysctl -w net.ipv4.conf.all.arp_filter=1 >/dev/null 2>&1 || true
sysctl -w net.ipv4.conf."$IFACE".arp_filter=1 >/dev/null 2>&1 || true

# ARP Ignore = 1: Responder ARP apenas se o IP alvo estiver configurado NA interface de entrada.
sysctl -w net.ipv4.conf.all.arp_ignore=1 >/dev/null 2>&1 || true
sysctl -w net.ipv4.conf."$IFACE".arp_ignore=1 >/dev/null 2>&1 || true

# ARP Announce = 2: Usar o melhor endereço local para anunciar nesta interface.
sysctl -w net.ipv4.conf.all.arp_announce=2 >/dev/null 2>&1 || true
sysctl -w net.ipv4.conf."$IFACE".arp_announce=2 >/dev/null 2>&1 || true

# RP Buffer = 0: Desabilitar RP filter para evitar drop de pacotes em interfaces secundárias.
sysctl -w net.ipv4.conf.all.rp_filter=0 >/dev/null 2>&1 || true
sysctl -w net.ipv4.conf."$IFACE".rp_filter=0 >/dev/null 2>&1 || true

# --------------------------------------------------------

# Tenta descobrir o gateway da interface.
GATEWAY=$(ip -4 route show dev "$IFACE" default 2>/dev/null | awk '/default/{print $3}' | head -n1)
if [[ -z "$GATEWAY" ]]; then
  GATEWAY=$(ip -4 route show dev "$IFACE" | awk '/via/{print $3}' | head -n1)
fi

# ---------- Policy routing (Isolamento de Interface) ----------

# Usamos o ifindex da interface como base para o ID da tabela.
IFINDEX=$(cat "/sys/class/net/$IFACE/ifindex" 2>/dev/null || echo "$PORT")
TABLE_ID=$((IFINDEX + 1000))

echo "DEBUG: Configurando Tabela $TABLE_ID para interface $IFACE (IP: $BIND_IP)" >&2

# Função de limpeza segura
cleanup() {
  local active_runners
  # Fallback caso pgrep não esteja disponível (embora deva estar após o build)
  if command -v pgrep >/dev/null; then
    active_runners=$(pgrep -f "iperf-runner.sh $IFACE" | wc -l)
  else
    active_runners=$(ps aux | grep "iperf-runner.sh $IFACE" | grep -v grep | wc -l)
  fi

  if [[ "$active_runners" -le 1 ]]; then
    echo "DEBUG: Liberando interface $IFACE (Table $TABLE_ID)..." >&2
    ip rule del from "$BIND_IP" table "$TABLE_ID" 2>/dev/null || true
    ip rule del oif "$IFACE" table "$TABLE_ID" 2>/dev/null || true
    ip route flush table "$TABLE_ID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# Limpa estado anterior
ip rule del from "$BIND_IP" table "$TABLE_ID" 2>/dev/null || true
ip rule del oif "$IFACE" table "$TABLE_ID" 2>/dev/null || true
ip route flush table "$TABLE_ID" 2>/dev/null || true

# 1. Rota para a Subrede Local (Essencial para ARP e comunicação local)
if [[ -n "$SUBNET" ]]; then
    # Adiciona a subrede como rota direta (link-local)
    ip route add "$SUBNET" dev "$IFACE" proto kernel scope link src "$BIND_IP" table "$TABLE_ID" 2>/dev/null || true
fi

# 2. Rota Default (Gateway)
if [[ -n "$GATEWAY" ]]; then
    echo "DEBUG: Usando Gateway $GATEWAY para $IFACE" >&2
    ip route add default via "$GATEWAY" dev "$IFACE" src "$BIND_IP" table "$TABLE_ID" 2>/dev/null || true
else
    # Fallback caso o IP do servidor esteja em outra rede e não tenhamos gateway detectado
    echo "DEBUG: Gateway não detectado. Forçando rota para o servidor via interface." >&2
    ip route add "$SERVER_IP" dev "$IFACE" scope link src "$BIND_IP" table "$TABLE_ID" 2>/dev/null || true
fi

# 3. Regras de Política de Roteamento (Prioridade Máxima)
# Com 'from BIND_IP', garantimos que qualquer pacote gerado pelo iperf (-B) use esta tabela.
# Com 'oif IFACE', garantimos que pacotes de resposta do kernel também sigam a mesma lógica.
ip rule add from "$BIND_IP" table "$TABLE_ID" pref 1000
ip rule add oif "$IFACE" table "$TABLE_ID" pref 1001

# Força atualização do cache de rotas
ip route flush cache 2>/dev/null || true

# EXIBE TABELA PARA DEBUG (Nos logs do container)
echo "--- CONFIGURAÇÃO FINAL Tabela $TABLE_ID ($IFACE) ---" >&2
ip route show table "$TABLE_ID" >&2
echo "-----------------------------------------------------" >&2

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
