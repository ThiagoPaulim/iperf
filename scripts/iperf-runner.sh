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
# Essencial para impedir que o tráfego "vaze" para a interface errada (eth0)
# se ambas estiverem na mesma subnet ou compartilharem o mesmo gateway.

echo "Aplicando sysctl tunings para $IFACE..."

# ARP Ignore = 1: Responder ARP apenas se o IP alvo estiver configurado NA interface de entrada.
sysctl -w net.ipv4.conf.all.arp_ignore=1 >/dev/null 2>&1 || true
sysctl -w net.ipv4.conf."$IFACE".arp_ignore=1 >/dev/null 2>&1 || true

# ARP Announce = 2: Usar o melhor endereço local para anunciar nesta interface.
sysctl -w net.ipv4.conf.all.arp_announce=2 >/dev/null 2>&1 || true
sysctl -w net.ipv4.conf."$IFACE".arp_announce=2 >/dev/null 2>&1 || true

# RP Buffer = 0 ou 2: Permitir roteamento assimétrico se necessário, mas respeitando as tabelas.
# 2 (Loose mode) é geralmente mais seguro para multi-wan.
sysctl -w net.ipv4.conf.all.rp_filter=2 >/dev/null 2>&1 || true
sysctl -w net.ipv4.conf."$IFACE".rp_filter=2 >/dev/null 2>&1 || true

# --------------------------------------------------------

# Tenta descobrir o gateway da interface.
# Tenta descobrir o gateway da interface.
# 1. Rota default específica da interface
GATEWAY=$(ip -4 route show dev "$IFACE" default 2>/dev/null | awk '/default/{print $3}' | head -n1)

# 2. Se não encontrar, procura qualquer rota com "via" associada a esta interface (ex: DHCP routes)
if [[ -z "$GATEWAY" ]]; then
  GATEWAY=$(ip -4 route show dev "$IFACE" | awk '/via/{print $3}' | head -n1)
fi

# NOTA: Removemos o fallback para "ip route show default" (global) pois ele causa
# problemas em ambientes multi-wan (tenta usar gw da eth0 na eth1, falha, e sai pela eth0).

# ---------- Policy routing ----------

# ---------- Policy routing ----------

# Usamos a própria PORTA do iperf como ID da tabela de roteamento.
# Como o app.py garante que cada fluxo simultâneo tem uma porta única,
# isso garante que não haverá colisão de tabela entre interfaces ou fluxos.
TABLE_ID="$PORT"

echo "DEBUG: Interface '$IFACE' (Port $PORT) -> Table ID: $TABLE_ID" >&2

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
# Remove regras antigas que possam existir para este IP/tabela a força bruta
ip rule del from "$BIND_IP" table "$TABLE_ID" 2>/dev/null || true
# Remove regra por prioridade se existir (cleanup antigo pode ter falhado)
ip rule del pref 1000 table "$TABLE_ID" 2>/dev/null || true

ip route flush table "$TABLE_ID" 2>/dev/null || true

# Adiciona regra com Prioridade alta (1000)
ip rule add from "$BIND_IP" table "$TABLE_ID" pref 1000

if [[ -n "$GATEWAY" ]]; then
  # Adiciona rota na tabela dedicada.
  ip route add default via "$GATEWAY" dev "$IFACE" table "$TABLE_ID" 2>/dev/null || true
  
  # ROTAS FORÇADAS:
  # Adiciona rota específica (/32) para o IP do Servidor nesta tabela.
  ip route add "$SERVER_IP" via "$GATEWAY" dev "$IFACE" table "$TABLE_ID" 2>/dev/null || true

  echo "DEBUG: Policy routing (Gateway): from $BIND_IP via $GATEWAY dev $IFACE"
else
  # AVISO: Gateway não encontrado. Pode ser conexão direta (Link Local).
  # Tenta adicionar rota direta para o servidor via interface.
  ip route add "$SERVER_IP" dev "$IFACE" table "$TABLE_ID" 2>/dev/null || true
  
  echo "DEBUG: Policy routing (Direct/Local): from $BIND_IP dev $IFACE"
fi

# Força limpeza do cache de rotas para garantir que as novas regras peguem imediatamente
ip route flush cache 2>/dev/null || true

RULES_CREATED=1

# DEBUG: Verifica qual interface o Kernel decidiu usar para este destino+origem
ROUTE_DEBUG=$(ip route get "$SERVER_IP" from "$BIND_IP" iif "$IFACE" 2>/dev/null || echo "Erro rota")
echo "DEBUG_ROUTE: $ROUTE_DEBUG"

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
CMD=(iperf3 -c "$SERVER_IP" -t "$DURATION" -i 1 -f m -B "$BIND_IP" -p "$PORT" -w 1M -P "$PARALLEL" --forceflush)
if [[ "$MODE" == "download" ]]; then
  CMD+=( -R )
fi

# Execução sem shell expansion adicional para evitar command injection.
"${CMD[@]}"
