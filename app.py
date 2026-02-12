#!/usr/bin/env python3
"""Aplicacao Flask + Socket.IO para executar testes iperf3 por interface."""

from __future__ import annotations

from collections import deque
import ipaddress
import os
import random
import re
import signal
import socket
import subprocess
import threading
import time
import psutil
import paramiko
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from flask import Flask, abort, jsonify, render_template, request
from flask_socketio import SocketIO, emit

BASE_DIR = Path(__file__).resolve().parent
RUNNER_SCRIPT = BASE_DIR / "scripts" / "iperf-runner.sh"
APP_REV = "2026-02-12-r13"

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "iperf-web-secret")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

TEST_LOCK = threading.Lock()
ACTIVE_TESTS: Dict[str, "TestTask"] = {}
RUN_STATE_LOCK = threading.Lock()
RUN_STATE: Dict[str, str] = {}
REMOTE_MONITORS_LOCK = threading.Lock()
REMOTE_MONITORS: Dict[str, threading.Event] = {}
EXCLUDED_IFACE_PREFIXES = (
    "docker",
    "veth",
    "br-",
    "virbr",
    "cni",
    "flannel",
    "kube",
    "ifb",
    "dummy",
    "tun",
    "tap",
    "vboxnet",
    "vmnet",
    "zt",
    "wg",
    "tailscale",
    "services",
)
ALLOW_VIRTUAL_INTERFACES = os.environ.get("ALLOW_VIRTUAL_INTERFACES", "1") == "1"
try:
    FLOW_RETRIES = max(0, min(4, int(os.environ.get("FLOW_RETRIES", "2"))))
except ValueError:
    FLOW_RETRIES = 2


def get_runner_revision() -> str:
    try:
        content = RUNNER_SCRIPT.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return "unknown"
    match = re.search(r'RUNNER_REV="([^"]+)"', content)
    if match:
        return match.group(1)
    return "unknown"


@dataclass
class TestTask:
    """Representa uma execucao iperf em uma interface/modo."""

    sid: str
    run_id: str
    server_ip: str
    port: int
    interface: str
    mode: str
    process: subprocess.Popen


def set_run_id(sid: str, run_id: str) -> None:
    with RUN_STATE_LOCK:
        RUN_STATE[sid] = run_id


def get_run_id(sid: str) -> Optional[str]:
    with RUN_STATE_LOCK:
        return RUN_STATE.get(sid)


def is_current_run(sid: str, run_id: str) -> bool:
    return get_run_id(sid) == run_id


def emit_run_event(event: str, payload: dict, sid: str, run_id: str) -> None:
    message = dict(payload)
    message["run_id"] = run_id
    socketio.emit(event, message, room=sid)


def terminate_process_tree(process: subprocess.Popen, grace_s: float = 8.0) -> bool:
    """Encerra processo (e filhos) com TERM e fallback para KILL."""

    if process.poll() is not None:
        return True

    try:
        # Linux: start_new_session=True permite finalizar todo o process-group.
        os.killpg(process.pid, signal.SIGTERM)
    except Exception:
        try:
            process.terminate()
        except Exception:
            pass

    try:
        process.wait(timeout=grace_s)
        return True
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass

    try:
        process.wait(timeout=3)
    except Exception:
        pass
    return process.poll() is not None


def stop_test_tasks(tasks: List[Tuple[str, "TestTask"]]) -> int:
    stopped = 0
    for tid, task in tasks:
        ok = terminate_process_tree(task.process)
        if ok:
            stopped += 1
        else:
            print(f"Falha ao encerrar processo do teste {tid}", flush=True)

    with TEST_LOCK:
        for tid, _task in tasks:
            ACTIVE_TESTS.pop(tid, None)

    return stopped


def stop_active_tests_for_sid(sid: str) -> int:
    """Encerra processos ativos de teste associados a uma sessao Socket.IO."""

    with TEST_LOCK:
        targets = [(tid, task) for tid, task in ACTIVE_TESTS.items() if task.sid == sid]

    if not targets:
        return 0

    return stop_test_tasks(targets)


def stop_all_active_tests() -> int:
    """Encerra qualquer teste ativo, independente da sessao."""

    with TEST_LOCK:
        targets = list(ACTIVE_TESTS.items())
    if not targets:
        return 0
    return stop_test_tasks(targets)


def cleanup_orphan_runner_processes() -> int:
    """Finaliza processos runner/iperf cliente que ficaram fora do controle do ACTIVE_TESTS."""

    tracked_pids = set()
    with TEST_LOCK:
        for task in ACTIVE_TESTS.values():
            tracked_pids.add(task.process.pid)

    victims = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            pid = int(proc.info.get("pid") or 0)
            if pid <= 0 or pid in tracked_pids:
                continue
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if not cmdline:
                continue
            if "iperf-runner.sh" in cmdline or ("iperf3" in cmdline and " -c " in f" {cmdline} "):
                victims.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    stopped = 0
    for proc in victims:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    for proc in victims:
        try:
            proc.wait(timeout=3)
            stopped += 1
        except (psutil.NoSuchProcess, psutil.TimeoutExpired):
            try:
                proc.kill()
                proc.wait(timeout=2)
                stopped += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
                pass
    return stopped


def reset_policy_state(interface: str) -> None:
    """Remove regras/tabelas residuais de policy routing da interface."""

    if os.environ.get("ENABLE_POLICY_ROUTING", "1") != "1":
        return

    ifindex_out = run_command(["cat", f"/sys/class/net/{interface}/ifindex"]).strip()
    if not ifindex_out.isdigit():
        return

    ifindex = int(ifindex_out)
    table_id = ifindex + 1000
    rule_pref = 20000 + ifindex

    # Remove todas as regras com o mesmo pref (nao apenas uma).
    for _ in range(8):
        result = subprocess.run(
            ["ip", "rule", "del", "pref", str(rule_pref)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            break

    subprocess.run(
        ["ip", "route", "flush", "table", str(table_id)],
        capture_output=True,
        text=True,
        check=False,
    )

    Path(f"/tmp/iperf-runner-{interface}.count").unlink(missing_ok=True)
    Path(f"/tmp/iperf-runner-{interface}.lock").unlink(missing_ok=True)


def stop_remote_monitor(sid: str, expected_event: Optional[threading.Event] = None) -> None:
    """Encerra monitoramento remoto ativo para uma sessao."""

    with REMOTE_MONITORS_LOCK:
        current = REMOTE_MONITORS.get(sid)
        if expected_event is not None and current is not expected_event:
            return
        event = REMOTE_MONITORS.pop(sid, None)
    if event is not None:
        event.set()


def run_command(cmd: List[str]) -> str:
    """Executa comando sem shell para evitar injecao de comandos."""

    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        return ""
    return completed.stdout


def find_closed_ports(server_ip: str, ports: List[int], timeout: float = 0.8) -> List[int]:
    """Verifica quais portas do servidor remoto nao estao aceitando conexao TCP."""

    closed = []
    for port in sorted(set(ports)):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            result = sock.connect_ex((server_ip, int(port)))
            if result != 0:
                closed.append(port)
        except Exception:
            closed.append(port)
        finally:
            sock.close()
    return closed


def wait_for_open_ports(
    server_ip: str, ports: List[int], max_wait_s: float, probe_timeout_s: float = 0.8
) -> List[int]:
    """Aguarda ate max_wait_s para todas as portas ficarem acessiveis."""

    end_ts = time.time() + max_wait_s
    closed = sorted(set(ports))
    while True:
        closed = find_closed_ports(server_ip, ports, timeout=probe_timeout_s)
        if not closed:
            return []
        if time.time() >= end_ts:
            return sorted(set(closed))
        time.sleep(0.4)


def parse_mbps(value: float, unit: str) -> float:
    """Converte K/M/G bits/sec para Mbits/sec."""

    unit = (unit or "M").upper()
    if unit == "G":
        return value * 1000
    if unit == "K":
        return value / 1000
    return value


def is_transient_iperf_error(text: str) -> bool:
    """Detecta falhas transitorias que valem uma retentativa curta."""

    t = (text or "").lower()
    transient_patterns = (
        "unable to connect to server",
        "connection refused",
        "no route to host",
        "network is unreachable",
        "server is busy",
        "resource temporarily unavailable",
        "temporary failure",
        "sem rota valida",
        "rota final nao saiu",
        "network is down",
        "cannot assign requested address",
    )
    return any(p in t for p in transient_patterns)


def has_hard_iperf_failure(text: str) -> bool:
    """Assinaturas de falha real (nao aproveita resultado parcial)."""

    t = (text or "").lower()
    hard_patterns = (
        "unable to connect to server",
        "connection timed out",
        "connection refused",
        "no route to host",
        "network is unreachable",
        "precheck tcp falhou",
        "sem rota valida",
        "rota final nao saiu",
        "cannot assign requested address",
        "network is down",
    )
    return any(p in t for p in hard_patterns)


def build_error_tail(lines: deque) -> str:
    """Resume as ultimas linhas, priorizando erro util ao inves de debug."""

    filtered = []
    for item in list(lines)[-16:]:
        if not item:
            continue
        lower = item.lower()
        if lower.startswith("debug:") or lower.startswith("debug_route:"):
            continue
        filtered.append(item)

    if not filtered:
        filtered = [item for item in list(lines)[-8:] if item]

    prioritized = []
    error_markers = (
        "error",
        "falha",
        "unable to connect",
        "timed out",
        "timeout",
        "interrupt",
        "sem rota",
        "precheck tcp falhou",
        "connection refused",
        "no route to host",
    )
    for item in filtered:
        lower = item.lower()
        if any(marker in lower for marker in error_markers):
            prioritized.append(item)
    if prioritized:
        return " | ".join(prioritized[-4:])

    return " | ".join(filtered[-10:])


def should_skip_interface(raw_name: str, flags: List[str]) -> bool:
    """Filtra interfaces virtuais/de infra para listar apenas candidatas reais."""

    name = raw_name.split("@", 1)[0]
    lower_name = name.lower()

    if name == "lo":
        return True

    # Sufixo @ifX geralmente indica par virtual/link de namespace.
    if "@" in raw_name:
        return True

    if any(lower_name.startswith(prefix) for prefix in EXCLUDED_IFACE_PREFIXES):
        return True

    # Mostra apenas interfaces com link ativo.
    if "LOWER_UP" not in flags:
        return True

    return False


def list_interfaces() -> List[dict]:
    """Coleta interfaces candidatas para teste via iperf3."""

    output = run_command(["ip", "-o", "link", "show"])
    interfaces: List[dict] = []

    for line in output.splitlines():
        link_match = re.match(r"^\d+:\s+([^:]+):\s+<([^>]*)>", line)
        if not link_match:
            continue

        raw_name = link_match.group(1).strip()
        flags = [flag.strip() for flag in link_match.group(2).split(",") if flag.strip()]
        if should_skip_interface(raw_name, flags):
            continue

        name = raw_name.split("@", 1)[0]

        # Mantem apenas interfaces fisicas por padrao, reduzindo ruido de veth/bridge.
        if not ALLOW_VIRTUAL_INTERFACES and not Path(f"/sys/class/net/{name}/device").exists():
            continue

        # A aplicacao usa bind IPv4, entao exige IPv4 global na interface.
        addr_out = run_command(["ip", "-4", "-o", "addr", "show", "dev", name, "scope", "global"])
        ipv4 = None
        if addr_out:
            match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/", addr_out)
            if match:
                ipv4 = match.group(1)
        if not ipv4:
            continue

        # Coleta atributos fisicos com ethtool.
        eth_out = run_command(["ethtool", name])
        speed = re.search(r"Speed:\s*([^\n]+)", eth_out)
        duplex = re.search(r"Duplex:\s*([^\n]+)", eth_out)
        autoneg = re.search(r"Auto-negotiation:\s*([^\n]+)", eth_out)

        interfaces.append(
            {
                "name": name,
                "ipv4": ipv4,
                "speed": speed.group(1).strip() if speed else "N/A",
                "duplex": duplex.group(1).strip() if duplex else "N/A",
                "autoneg": autoneg.group(1).strip() if autoneg else "N/A",
            }
        )

    # Remove duplicatas preservando ordem.
    unique = {}
    for item in interfaces:
        unique[item["name"]] = item
    ordered = list(unique.values())

    def sort_key(item: dict):
        # Ordenacao natural por blocos alfanumericos.
        # Ex.: enp2s0 < enp10s0, eth9 < eth10
        name = item["name"].lower()
        chunks = re.split(r"(\d+)", name)
        return [int(chunk) if chunk.isdigit() else chunk for chunk in chunks]

    ordered.sort(key=sort_key)
    return ordered

def validate_payload(payload: dict) -> Optional[str]:
    """Valida os campos recebidos do frontend."""

    required = ["server_ip", "duration", "mode", "interfaces"]
    for key in required:
        if key not in payload:
            return f"Campo obrigatorio ausente: {key}"

    try:
        ipaddress.ip_address(payload["server_ip"])
    except ValueError:
        return "IP do servidor invalido."

    try:
        duration = int(payload["duration"])
    except (TypeError, ValueError):
        return "Tempo do teste deve ser numerico."

    if duration < 1 or duration > 3600:
        return "Tempo do teste deve estar entre 1 e 3600 segundos."

    if payload["mode"] not in {"upload", "download", "both", "both_sequential"}:
        return "Modo invalido."

    if not isinstance(payload["interfaces"], list) or not payload["interfaces"]:
        return "Selecione ao menos uma interface."

    try:
        base_port = int(payload.get("base_port", 5201))
    except (TypeError, ValueError):
        return "Porta base deve ser numerica."

    if base_port < 1 or base_port > 65535:
        return "Porta base deve estar entre 1 e 65535."

    try:
        parallel = int(payload.get("parallel", 4))
    except (TypeError, ValueError):
        return "Parallel deve ser numerico."

    if parallel < 1 or parallel > 64:
        return "Parallel deve estar entre 1 e 64."

    available = {item["name"] for item in list_interfaces()}
    for iface in payload["interfaces"]:
        if iface not in available:
            return f"Interface invalida: {iface}"

    return None


def run_single_test(
    server_ip: str,
    duration: int,
    interface: str,
    mode: str,
    sid: str,
    run_id: str,
    port: int,
    parallel: int,
    retry_left: int = FLOW_RETRIES,
    attempt: int = 1,
) -> None:
    """Executa um unico fluxo iperf e envia atualizacoes em tempo real."""

    task_id = f"{sid}:{interface}:{mode}:{port}:{time.time_ns()}"
    cmd = [
        str(RUNNER_SCRIPT),
        interface,
        server_ip,
        str(duration),
        mode,
        str(port),
        str(parallel),
    ]
    runner_env = os.environ.copy()
    # Blindagem: evita tunings em /sys (read-only em varios ambientes/container).
    runner_env["ENABLE_NIC_TUNING"] = "0"
    # Mantem comportamento estavel de rede por processo.
    runner_env.setdefault("ENABLE_SYSCTL_TUNING", "0")
    runner_env.setdefault("ENABLE_POLICY_ROUTING", "1")

    process: Optional[subprocess.Popen] = None

    final_mbps = 0.0
    recent_lines = deque(maxlen=80)
    # Regex base para capturar throughput
    # Formato padrao: [  5] ...
    # Formato SUM:    [SUM] ...
    line_pattern = re.compile(
        r"\[\s*(\d+|SUM)\]\s+\d+\.\d+-\d+\.\d+\s+sec\s+\S+\s+(\S+Bytes|Bytes)\s+([\d.]+)\s+([KMG])bits/sec"
    )

    # Regex para capturar metricas iniciais (Ping)
    # Formato esperado: [METRICS] Ping: 15.20 ms
    metrics_pattern = re.compile(r"\[METRICS\] Ping: ([\d.]+) ms")

    try:
        if not is_current_run(sid, run_id):
            return

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=runner_env,
        )

        with TEST_LOCK:
            ACTIVE_TESTS[task_id] = TestTask(
                sid=sid,
                run_id=run_id,
                server_ip=server_ip,
                port=port,
                interface=interface,
                mode=mode,
                process=process,
            )

        assert process.stdout is not None
        for line in process.stdout:
            if not is_current_run(sid, run_id):
                terminate_process_tree(process, grace_s=3)
                break
            line = line.strip()
            recent_lines.append(line)
            # Log para debug em tempo real no terminal do container
            print(f"[iperf3 raw] {interface}:{mode} -> {line}", flush=True)

            # Checa se e linha de metrica
            m_metrics = metrics_pattern.search(line)
            if m_metrics:
                ping_val = m_metrics.group(1)
                emit_run_event(
                    "metrics_update",
                    {
                        "interface": interface,
                        "mode": mode,
                        "ping": ping_val,
                    },
                    sid,
                    run_id,
                )
                continue

            match = line_pattern.search(line)
            if match:
                stream_id = match.group(1)  # ID ou SUM
                # Se usarmos parallel > 1, queremos APENAS a linha [SUM].
                # Se usarmos parallel = 1, queremos a linha normal (que tem ID numerico).
                if parallel > 1 and stream_id != "SUM":
                    continue
                if parallel == 1 and stream_id == "SUM":
                    continue

                # O grupo 3 e valor, grupo 4 e unidade.
                raw = float(match.group(3))
                unit = match.group(4)
                mbps = round(parse_mbps(raw, unit), 2)
                final_mbps = mbps
                emit_run_event(
                    "throughput_update",
                    {
                        "interface": interface,
                        "mode": mode,
                        "mbps": mbps,
                        "timestamp": int(time.time()),
                    },
                    sid,
                    run_id,
                )

        return_code = process.wait()
        if not is_current_run(sid, run_id):
            return
        if return_code == 0:
            emit_run_event(
                "test_result",
                {
                    "interface": interface,
                    "mode": mode,
                    "success": True,
                    "final_mbps": final_mbps,
                },
                sid,
                run_id,
            )
        else:
            error_tail = build_error_tail(recent_lines)
            if final_mbps > 0 and not has_hard_iperf_failure(error_tail):
                emit_run_event(
                    "test_error",
                    {
                        "message": (
                            f"Fluxo {interface} ({mode}) finalizou com aviso, "
                            f"mas throughput valido foi mantido ({final_mbps} Mbps)."
                        ),
                        "fatal": False,
                    },
                    sid,
                    run_id,
                )
                emit_run_event(
                    "test_result",
                    {
                        "interface": interface,
                        "mode": mode,
                        "success": True,
                        "final_mbps": final_mbps,
                    },
                    sid,
                    run_id,
                )
                return
            if retry_left > 0 and is_current_run(sid, run_id) and is_transient_iperf_error(error_tail):
                # Jitter progressivo evita rajada simultanea de reconexao.
                wait_s = min(4.8, 1.1 + (attempt * 0.9) + random.uniform(0.2, 1.0))
                emit_run_event(
                    "test_error",
                    {
                        "message": (
                            f"Falha transitoria em {interface} ({mode}) na porta {port}. "
                            f"Retentando automaticamente ({attempt}/{FLOW_RETRIES + 1}) em {wait_s:.1f}s..."
                        ),
                        "fatal": False,
                    },
                    sid,
                    run_id,
                )
                # Evita limpar policy no meio de fluxos simultaneos da mesma interface.
                # O runner ja faz cleanup no EXIT de cada processo.
                time.sleep(wait_s)
                run_single_test(
                    server_ip,
                    duration,
                    interface,
                    mode,
                    sid,
                    run_id,
                    port,
                    parallel,
                    retry_left=retry_left - 1,
                    attempt=attempt + 1,
                )
                return
            emit_run_event(
                "test_result",
                {
                    "interface": interface,
                    "mode": mode,
                    "success": False,
                    "error": error_tail or "Falha ao executar iperf3.",
                },
                sid,
                run_id,
            )
    except Exception as exc:
        if is_current_run(sid, run_id):
            emit_run_event(
                "test_result",
                {
                    "interface": interface,
                    "mode": mode,
                    "success": False,
                    "error": f"Erro interno ao executar teste: {str(exc)}",
                },
                sid,
                run_id,
            )
    finally:
        with TEST_LOCK:
            ACTIVE_TESTS.pop(task_id, None)


def run_sequential_both(
    server_ip: str,
    duration: int,
    interfaces: List[str],
    sid: str,
    run_id: str,
    base_port: int,
    parallel: int,
) -> None:
    """Executa fases upload e download, com interfaces simultaneas em cada fase."""

    for phase_mode in ["upload", "download"]:
        if not is_current_run(sid, run_id):
            return
        emit_run_event("phase_started", {"mode": phase_mode}, sid, run_id)
        phase_tests: List[Tuple[str, str, int]] = []
        for idx, iface in enumerate(interfaces):
            port = base_port + idx
            phase_tests.append((iface, phase_mode, port))
        run_parallel_tests(server_ip, duration, phase_tests, sid, run_id, parallel)


def run_parallel_tests(
    server_ip: str,
    duration: int,
    tests: List[Tuple[str, str, int]],
    sid: str,
    run_id: str,
    parallel: int,
) -> None:
    """Dispara todos os testes de uma vez para inicio praticamente simultaneo."""

    start_event = threading.Event()
    threads = []

    def worker(iface: str, mode: str, port: int) -> None:
        start_event.wait()
        if not is_current_run(sid, run_id):
            return
        # Mantem quase simultaneo, mas evita pico de SYN/controle no mesmo milissegundo.
        spread_key = f"{iface}:{mode}:{port}:{run_id}"
        initial_jitter_s = (sum(ord(ch) for ch in spread_key) % 900) / 1000.0
        if initial_jitter_s > 0:
            time.sleep(initial_jitter_s)
        run_single_test(server_ip, duration, iface, mode, sid, run_id, port, parallel)

    for iface, mode, port in tests:
        t = threading.Thread(target=worker, args=(iface, mode, port))
        t.start()
        threads.append(t)

    start_event.set()

    for t in threads:
        t.join()


def setup_remote_server(ip, user, password, ports):
    """Conecta via SSH e inicia instancias do iperf3 nas portas necessarias."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        print(f"Conectando ao servidor remoto {ip} via SSH...", flush=True)
        client.connect(ip, username=user, password=password, timeout=5)

        ports = sorted({int(p) for p in ports})
        ports_arg = " ".join(str(p) for p in ports)
        # Usa uma unica execucao remota para reduzir abertura de canais SSH.
        remote_cmd = f"""
PORTS="{ports_arg}"
for p in $PORTS; do
  fuser -k -n tcp "$p" >/dev/null 2>&1 || true
  pkill -f "iperf3 -s -p $p" >/dev/null 2>&1 || true
done
for p in $PORTS; do
  for i in $(seq 1 20); do
    ss -ltn sport = :"$p" 2>/dev/null | grep -q LISTEN || break
    sleep 0.2
  done
done
for p in $PORTS; do
  iperf3 -s -p "$p" -D >/dev/null 2>&1 || true
done
for p in $PORTS; do
  ok=0
  for i in $(seq 1 25); do
    if ss -ltn sport = :"$p" 2>/dev/null | grep -q LISTEN; then
      ok=1
      break
    fi
    sleep 0.2
  done
  if [ "$ok" -eq 1 ]; then
    echo "OK:$p"
  else
    echo "FAIL:$p"
  fi
done
"""
        stdin, stdout, stderr = client.exec_command(remote_cmd, timeout=45)
        _ = stdin
        exit_status = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="ignore")
        err = stderr.read().decode(errors="ignore")

        running_ports = sorted(
            {
                int(line.split(":", 1)[1].strip())
                for line in out.splitlines()
                if line.startswith("OK:")
            }
        )
        failed_ports = sorted(
            {
                int(line.split(":", 1)[1].strip())
                for line in out.splitlines()
                if line.startswith("FAIL:")
            }
        )

        if exit_status != 0:
            print(f"Setup remoto retornou status {exit_status}. stderr={err.strip()}", flush=True)
        missing_ports = sorted(set(ports) - set(running_ports))
        if missing_ports:
            return (
                False,
                "Falha ao iniciar iperf3 em todas as portas. "
                f"Ativas: {sorted(running_ports)} | Faltando: {missing_ports} | "
                f"Falhas reportadas: {failed_ports} | stderr: {err.strip()[:180]}",
            )
        return True, f"Servicos iniciados nas portas: {sorted(running_ports)}"

    except Exception as e:
        return False, f"Erro SSH: {str(e)}"
    finally:
        client.close()


def monitor_remote_system(
    ip: str,
    user: str,
    password: str,
    sid: str,
    run_id: str,
    stop_event: threading.Event,
    deadline_ts: float,
) -> None:
    """Monitora CPU/RAM do servidor remoto e emite via socketio."""

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    prev_total: Optional[int] = None
    prev_idle: Optional[int] = None

    cmd = (
        "awk '/^cpu / {print $2, $3, $4, $5, $6, $7, $8, $9} "
        "/^MemTotal:/ {mt=$2} /^MemAvailable:/ {ma=$2} "
        "END {print mt, ma}' /proc/stat /proc/meminfo"
    )

    try:
        client.connect(ip, username=user, password=password, timeout=5)
        while not stop_event.is_set() and time.time() < deadline_ts and is_current_run(sid, run_id):
            try:
                stdin, stdout, stderr = client.exec_command(cmd, timeout=4)
            except Exception:
                # Reabre sessao SSH em falhas transitórias de canal.
                try:
                    client.close()
                except Exception:
                    pass
                time.sleep(1)
                try:
                    client.connect(ip, username=user, password=password, timeout=5)
                except Exception:
                    time.sleep(1)
                continue
            _ = stdin
            err = stderr.read().decode(errors="ignore").strip()
            out_lines = stdout.read().decode(errors="ignore").strip().splitlines()
            if err or len(out_lines) < 2:
                time.sleep(2)
                continue

            try:
                cpu_fields = [int(x) for x in out_lines[0].split()]
                mem_total_kb, mem_avail_kb = [int(x) for x in out_lines[1].split()[:2]]
            except (TypeError, ValueError, IndexError):
                time.sleep(1)
                continue

            total = sum(cpu_fields)
            idle = cpu_fields[3] + cpu_fields[4]  # idle + iowait

            cpu_percent = None
            if prev_total is not None and prev_idle is not None:
                diff_total = total - prev_total
                diff_idle = idle - prev_idle
                if diff_total > 0:
                    cpu_percent = round((1 - (diff_idle / diff_total)) * 100, 1)

            prev_total = total
            prev_idle = idle

            mem_used_kb = max(0, mem_total_kb - mem_avail_kb)
            ram_percent = round((mem_used_kb / mem_total_kb) * 100, 1) if mem_total_kb > 0 else 0.0

            emit_run_event(
                "remote_system_status",
                {
                    "cpu": cpu_percent,
                    "ram_percent": ram_percent,
                    "ram_used_gb": round(mem_used_kb / (1024**2), 2),
                    "ram_total_gb": round(mem_total_kb / (1024**2), 2),
                },
                sid,
                run_id,
            )
            time.sleep(2)
    except Exception as exc:
        if is_current_run(sid, run_id):
            emit_run_event(
                "remote_system_status",
                {"error": f"Falha ao monitorar servidor remoto: {str(exc)}"},
                sid,
                run_id,
            )
    finally:
        client.close()
        stop_remote_monitor(sid, stop_event)


@app.route("/")
def index():
    return render_template("index.html", app_rev=APP_REV, runner_rev=get_runner_revision())


@app.route("/r10b", strict_slashes=False)
def index_r10b():
    return render_template("index.html", app_rev=APP_REV, runner_rev=get_runner_revision())


@app.route("/v2", strict_slashes=False)
def index_v2():
    return render_template("index.html", app_rev=APP_REV, runner_rev=get_runner_revision())


@app.route("/version", methods=["GET"])
def get_version_alias():
    return jsonify({"app_rev": APP_REV, "runner_rev": get_runner_revision(), "flow_retries": FLOW_RETRIES})


@app.after_request
def add_no_cache_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/api/version", methods=["GET"])
def get_version():
    return jsonify({"app_rev": APP_REV, "runner_rev": get_runner_revision(), "flow_retries": FLOW_RETRIES})


@app.route("/api/interfaces", methods=["GET"])
def get_interfaces():
    return jsonify({"interfaces": list_interfaces()})


@app.route("/<path:path>", methods=["GET"])
def index_fallback(path: str):
    # Mantem /api e /static com comportamento normal de 404 quando inexistentes.
    if path.startswith("api/") or path.startswith("static/"):
        abort(404)
    return render_template("index.html", app_rev=APP_REV, runner_rev=get_runner_revision())


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    stop_remote_monitor(sid)
    # Nao encerra testes em andamento aqui: desconexoes curtas do websocket
    # podem ocorrer durante carga alta e nao devem interromper o teste.
    print(f"Disconnect {sid}: websocket desconectado, testes continuam.", flush=True)


@socketio.on("start_test")
def start_test(payload: dict):
    """Inicia testes simultaneos de acordo com interfaces e modo selecionados."""

    sid = request.sid
    stop_remote_monitor(sid)
    run_id = f"{int(time.time() * 1000)}-{os.urandom(3).hex()}"
    set_run_id(sid, run_id)
    emit_run_event("run_ack", {"status": "accepted"}, sid, run_id)

    error = validate_payload(payload)
    if error:
        emit_run_event("test_error", {"message": error}, sid, run_id)
        return

    # Verifica configuracao remota opcional
    configure_server = bool(payload.get("configure_server", False))
    ssh_user = payload.get("ssh_user")
    ssh_pass = payload.get("ssh_pass")

    server_ip = payload["server_ip"]
    interfaces = payload["interfaces"]
    base_port = int(payload.get("base_port", 5201))
    parallel = int(payload.get("parallel", 4))
    selected_mode = payload["mode"]
    duration = int(payload["duration"])
    total_duration = duration * 2 if selected_mode == "both_sequential" else duration

    stopped = stop_all_active_tests()
    orphan_killed = cleanup_orphan_runner_processes()
    for iface in interfaces:
        reset_policy_state(iface)

    if stopped > 0 or orphan_killed > 0:
        print(
            f"Pre-run cleanup: encerrados={stopped}, orfaos_encerrados={orphan_killed}, sid={sid}",
            flush=True,
        )
        emit_run_event(
            "test_error",
            {
                "message": "Reiniciando... aguardando liberacao de recursos do teste anterior.",
                "fatal": False,
            },
            sid,
            run_id,
        )
        time.sleep(2)

    # Portas necessarias
    # Se modo for 'both' (simultaneo), precisamos de 2 portas por interface (uma pra up, uma pra down).
    # Em 'both_sequential', reutilizamos a mesma porta pois as fases nao se sobrepoem.
    needed_ports: List[int] = []
    if selected_mode == "both":
        total_slots = len(interfaces) * 2
        needed_ports = [base_port + i for i in range(total_slots)]
    else:
        # upload, download e both_sequential usam 1 porta por interface
        needed_ports = [base_port + i for i in range(len(interfaces))]

    if configure_server:
        if not ssh_user or not ssh_pass:
            emit_run_event(
                "test_error",
                {"message": "Usuario e senha SSH sao obrigatorios para configuracao automatica."},
                sid,
                run_id,
            )
            return

        emit_run_event(
            "test_error",
            {"message": "Configurando servidor remoto via SSH...", "fatal": False},
            sid,
            run_id,
        )
        success, msg = setup_remote_server(server_ip, ssh_user, ssh_pass, needed_ports)
        if not success:
            emit_run_event("test_error", {"message": f"Falha na configuracao remota: {msg}"}, sid, run_id)
            return

        emit_run_event(
            "test_error",
            {"message": f"Servidor remoto configurado com sucesso. {msg}", "fatal": False},
            sid,
            run_id,
        )
        print(f"Servidor remoto configurado: {msg}", flush=True)
        time.sleep(1)

    closed_ports = wait_for_open_ports(
        server_ip,
        needed_ports,
        max_wait_s=10.0 if configure_server else 2.0,
        probe_timeout_s=0.7,
    )
    if closed_ports:
        preview = ", ".join(str(p) for p in closed_ports[:8])
        suffix = "..." if len(closed_ports) > 8 else ""
        emit_run_event(
            "test_error",
            {
                "message": (
                    "Portas do iperf3 indisponiveis no servidor remoto: "
                    f"{preview}{suffix}. "
                    "No modo 'Ambos (simultaneo)' sao necessarias 2 portas por interface."
                )
            },
            sid,
            run_id,
        )
        return

    if selected_mode == "both_sequential":
        modes = ["upload", "download"]
    elif selected_mode == "both":
        modes = ["upload", "download"]
    else:
        modes = [selected_mode]

    emit_run_event(
        "test_started",
        {
            "interfaces": interfaces,
            "modes": modes,
            "app_rev": APP_REV,
            "runner_rev": get_runner_revision(),
            "flow_retries": FLOW_RETRIES,
        },
        sid,
        run_id,
    )

    if configure_server:
        monitor_stop = threading.Event()
        with REMOTE_MONITORS_LOCK:
            REMOTE_MONITORS[sid] = monitor_stop
        socketio.start_background_task(
            monitor_remote_system,
            server_ip,
            ssh_user,
            ssh_pass,
            sid,
            run_id,
            monitor_stop,
            time.time() + total_duration + 20,
        )
    else:
        emit_run_event("remote_system_status", {"disabled": True}, sid, run_id)

    if selected_mode == "both_sequential":
        # Modo sequencial: upload em todas, espera, depois download em todas.
        socketio.start_background_task(
            run_sequential_both,
            server_ip,
            duration,
            interfaces,
            sid,
            run_id,
            base_port,
            parallel,
        )
    else:
        # Modos simultaneos: todos os testes ao mesmo tempo.
        tests: List[Tuple[str, str, int]] = []
        port_idx = 0
        for iface in interfaces:
            for mode in modes:
                port = base_port + port_idx
                tests.append((iface, mode, port))
                port_idx += 1
        socketio.start_background_task(
            run_parallel_tests,
            server_ip,
            duration,
            tests,
            sid,
            run_id,
            parallel,
        )


def monitor_system():
    """Monitora CPU e RAM globalmente e emite via socketio a cada 1s."""
    while True:
        cpu_percent = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory()
        socketio.emit(
            "system_status",
            {
                "cpu": cpu_percent,
                "ram_percent": ram.percent,
                "ram_used_gb": round(ram.used / (1024**3), 2),
                "ram_total_gb": round(ram.total / (1024**3), 2),
            },
        )


if __name__ == "__main__":
    # Inicia a thread de monitoramento em background
    threading.Thread(target=monitor_system, daemon=True).start()
    print(
        f"Startup: APP_REV={APP_REV} RUNNER_REV={get_runner_revision()} FLOW_RETRIES={FLOW_RETRIES}",
        flush=True,
    )
    socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)

