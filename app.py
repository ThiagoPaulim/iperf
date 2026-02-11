#!/usr/bin/env python3
"""AplicaÃ§Ã£o Flask + Socket.IO para executar testes iperf3 por interface."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
import threading
import time
import psutil
import paramiko
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit

BASE_DIR = Path(__file__).resolve().parent
RUNNER_SCRIPT = BASE_DIR / "scripts" / "iperf-runner.sh"

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "iperf-web-secret")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

TEST_LOCK = threading.Lock()
ACTIVE_TESTS: Dict[str, "TestTask"] = {}
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


@dataclass
class TestTask:
    """Representa uma execuÃ§Ã£o iperf em uma interface/modo."""

    interface: str
    mode: str
    process: subprocess.Popen


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
    """Executa comando sem shell para evitar injeÃ§Ã£o de comandos."""

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


def parse_mbps(value: float, unit: str) -> float:
    """Converte K/M/G bits/sec para Mbits/sec."""

    unit = (unit or "M").upper()
    if unit == "G":
        return value * 1000
    if unit == "K":
        return value / 1000
    return value


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
    return list(unique.values())

def validate_payload(payload: dict) -> Optional[str]:
    """Valida os campos recebidos do frontend."""

    required = ["server_ip", "duration", "mode", "interfaces"]
    for key in required:
        if key not in payload:
            return f"Campo obrigatÃ³rio ausente: {key}"

    try:
        ipaddress.ip_address(payload["server_ip"])
    except ValueError:
        return "IP do servidor invÃ¡lido."

    try:
        duration = int(payload["duration"])
    except (TypeError, ValueError):
        return "Tempo do teste deve ser numÃ©rico."

    if duration < 1 or duration > 3600:
        return "Tempo do teste deve estar entre 1 e 3600 segundos."

    if payload["mode"] not in {"upload", "download", "both", "both_sequential"}:
        return "Modo invÃ¡lido."

    if not isinstance(payload["interfaces"], list) or not payload["interfaces"]:
        return "Selecione ao menos uma interface."

    try:
        base_port = int(payload.get("base_port", 5201))
    except (TypeError, ValueError):
        return "Porta base deve ser numÃ©rica."

    if base_port < 1 or base_port > 65535:
        return "Porta base deve estar entre 1 e 65535."

    try:
        parallel = int(payload.get("parallel", 4))
    except (TypeError, ValueError):
        return "Parallel deve ser numÃ©rico."

    if parallel < 1 or parallel > 64:
        return "Parallel deve estar entre 1 e 64."

    available = {item["name"] for item in list_interfaces()}
    for iface in payload["interfaces"]:
        if iface not in available:
            return f"Interface invÃ¡lida: {iface}"

    return None


def run_single_test(
    server_ip: str, duration: int, interface: str, mode: str, sid: str, port: int, parallel: int
) -> None:
    """Executa um Ãºnico fluxo iperf e envia atualizaÃ§Ãµes em tempo real."""

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

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    with TEST_LOCK:
        ACTIVE_TESTS[task_id] = TestTask(interface=interface, mode=mode, process=process)

    final_mbps = 0.0
    # Regex base para capturar throughput
    # Formato padrÃ£o: [  5] ...
    # Formato SUM:    [SUM] ...
    line_pattern = re.compile(
        r"\[\s*(\d+|SUM)\]\s+\d+\.\d+-\d+\.\d+\s+sec\s+\S+\s+(\S+Bytes|Bytes)\s+([\d.]+)\s+([KMG])bits/sec"
    )

    # Regex para capturar mÃ©tricas iniciais (Ping)
    # Formato esperado: [METRICS] Ping: 15.20 ms
    metrics_pattern = re.compile(r"\[METRICS\] Ping: ([\d.]+) ms")

    try:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.strip()
            # Log para debug em tempo real no terminal do container
            print(f"[iperf3 raw] {interface}:{mode} -> {line}", flush=True)

            # Checa se Ã© linha de mÃ©trica
            m_metrics = metrics_pattern.search(line)
            if m_metrics:
                ping_val = m_metrics.group(1)
                socketio.emit(
                    "metrics_update",
                    {
                        "interface": interface,
                        "mode": mode,
                        "ping": ping_val
                    },
                    room=sid,
                )
                continue

            match = line_pattern.search(line)
            if match:
                stream_id = match.group(1)  # ID ou SUM
                # Se usarmos parallel > 1, queremos APENAS a linha [SUM].
                # Se usarmos parallel = 1, queremos a linha normal (que tem ID numÃ©rico).
                if parallel > 1 and stream_id != "SUM":
                    continue
                if parallel == 1 and stream_id == "SUM":
                    continue

                # O grupo 3 Ã© valor, grupo 4 Ã© unidade.
                raw = float(match.group(3))
                unit = match.group(4)
                mbps = round(parse_mbps(raw, unit), 2)
                final_mbps = mbps
                socketio.emit(
                    "throughput_update",
                    {
                        "interface": interface,
                        "mode": mode,
                        "mbps": mbps,
                        "timestamp": int(time.time()),
                    },
                    room=sid,
                )

        stderr_out = process.stderr.read().strip() if process.stderr else ""
        return_code = process.wait()
        if return_code == 0:
            socketio.emit(
                "test_result",
                {
                    "interface": interface,
                    "mode": mode,
                    "success": True,
                    "final_mbps": final_mbps,
                },
                room=sid,
            )
        else:
            socketio.emit(
                "test_result",
                {
                    "interface": interface,
                    "mode": mode,
                    "success": False,
                    "error": stderr_out or "Falha ao executar iperf3.",
                },
                room=sid,
            )
    finally:
        with TEST_LOCK:
            ACTIVE_TESTS.pop(task_id, None)


def run_sequential_both(
    server_ip: str,
    duration: int,
    interfaces: List[str],
    sid: str,
    base_port: int,
    parallel: int,
) -> None:
    """Executa upload e download por interface, sem concorrencia entre interfaces."""

    for phase_mode in ["upload", "download"]:
        socketio.emit("phase_started", {"mode": phase_mode}, room=sid)
        for idx, iface in enumerate(interfaces):
            port = base_port + idx
            run_single_test(server_ip, duration, iface, phase_mode, sid, port, parallel)


def run_parallel_tests(
    server_ip: str,
    duration: int,
    tests: List[Tuple[str, str, int]],
    sid: str,
    parallel: int,
) -> None:
    """Dispara todos os testes de uma vez para inicio praticamente simultaneo."""

    start_event = threading.Event()
    threads = []

    def worker(iface: str, mode: str, port: int) -> None:
        start_event.wait()
        run_single_test(server_ip, duration, iface, mode, sid, port, parallel)

    for iface, mode, port in tests:
        t = threading.Thread(target=worker, args=(iface, mode, port))
        t.start()
        threads.append(t)

    start_event.set()

    for t in threads:
        t.join()


def setup_remote_server(ip, user, password, ports):
    """Conecta via SSH e inicia instÃ¢ncias do iperf3 nas portas necessÃ¡rias."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        print(f"Conectando ao servidor remoto {ip} via SSH...", flush=True)
        client.connect(ip, username=user, password=password, timeout=5)
        
        running_ports = []
        for port in ports:
            # Mata qualquer processo jÃ¡ rodando nesta porta para garantir estado limpo
            # fuser -k -n tcp PORT ou pkill -f "iperf3 -s -p PORT"
            # O comando abaixo mata processos ouvindo na porta especificada.
            kill_cmd = f"fuser -k -n tcp {port} || true"
            client.exec_command(kill_cmd)
            time.sleep(0.5)

            # Inicia iperf3 em background (daemon)
            start_cmd = f"nohup iperf3 -s -p {port} -D > /dev/null 2>&1 &"
            stdin, stdout, stderr = client.exec_command(start_cmd)
            exit_status = stdout.channel.recv_exit_status()
            
            if exit_status == 0:
                running_ports.append(port)
            else:
                print(f"Falha ao iniciar iperf3 na porta {port} (remoto)", flush=True)
        missing_ports = sorted(set(ports) - set(running_ports))
        if missing_ports:
            return (
                False,
                "Falha ao iniciar iperf3 em todas as portas. "
                f"Ativas: {sorted(running_ports)} | Faltando: {missing_ports}",
            )
        return True, f"ServiÃ§os iniciados nas portas: {sorted(running_ports)}"

    except Exception as e:
        return False, f"Erro SSH: {str(e)}"
    finally:
        client.close()


def monitor_remote_system(
    ip: str,
    user: str,
    password: str,
    sid: str,
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
        while not stop_event.is_set() and time.time() < deadline_ts:
            stdin, stdout, stderr = client.exec_command(cmd, timeout=4)
            _ = stdin
            err = stderr.read().decode(errors="ignore").strip()
            out_lines = stdout.read().decode(errors="ignore").strip().splitlines()
            if err or len(out_lines) < 2:
                time.sleep(1)
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

            socketio.emit(
                "remote_system_status",
                {
                    "cpu": cpu_percent,
                    "ram_percent": ram_percent,
                    "ram_used_gb": round(mem_used_kb / (1024**2), 2),
                    "ram_total_gb": round(mem_total_kb / (1024**2), 2),
                },
                room=sid,
            )
            time.sleep(1)
    except Exception as exc:
        socketio.emit(
            "remote_system_status",
            {"error": f"Falha ao monitorar servidor remoto: {str(exc)}"},
            room=sid,
        )
    finally:
        client.close()
        stop_remote_monitor(sid, stop_event)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/interfaces", methods=["GET"])
def get_interfaces():
    return jsonify({"interfaces": list_interfaces()})


@socketio.on("start_test")
def start_test(payload: dict):
    """Inicia testes simultÃ¢neos de acordo com interfaces e modo selecionados."""

    sid = request.sid
    stop_remote_monitor(sid)

    error = validate_payload(payload)
    if error:
        emit("test_error", {"message": error})
        return

    # Verifica configuraÃ§Ã£o remota opcional
    configure_server = payload.get("configure_server", False)
    ssh_user = payload.get("ssh_user")
    ssh_pass = payload.get("ssh_pass")
    
    interfaces = payload["interfaces"]
    base_port = int(payload.get("base_port", 5201))
    parallel = int(payload.get("parallel", 4))
    selected_mode = payload["mode"]
    duration = int(payload["duration"])
    total_duration = duration * 2 if selected_mode == "both_sequential" else duration
    
    # Portas necessÃ¡rias
    # Se modo for 'both' (simultÃ¢neo), precisamos de 2 portas por interface (uma pra up, uma pra down).
    # Em 'both_sequential', reutilizamos a mesma porta pois as fases nÃ£o se sobrepÃµem.
    needed_ports = []
    if selected_mode == "both":
        total_slots = len(interfaces) * 2
        needed_ports = [base_port + i for i in range(total_slots)]
    else:
        # upload, download e both_sequential usam 1 porta por interface
        needed_ports = [base_port + i for i in range(len(interfaces))]

    # Se configuraÃ§Ã£o automÃ¡tica estiver ativa, tenta configurar o servidor antes
    if configure_server:
        if not ssh_user or not ssh_pass:
            emit("test_error", {"message": "UsuÃ¡rio e Senha SSH sÃ£o obrigatÃ³rios para configuraÃ§Ã£o automÃ¡tica."})
            return

        emit("test_error", {"message": "Configurando servidor remoto via SSH..."}) # Usa error message para toast/log
        success, msg = setup_remote_server(payload["server_ip"], ssh_user, ssh_pass, needed_ports)
        if not success:
            emit("test_error", {"message": f"Falha na configuraÃ§Ã£o remota: {msg}"})
            return
        
        print(f"Servidor remoto configurado: {msg}", flush=True)
        # DÃ¡ um tempinho para o iperf3 subir no remoto
        time.sleep(2)

        monitor_stop = threading.Event()
        with REMOTE_MONITORS_LOCK:
            REMOTE_MONITORS[sid] = monitor_stop
        socketio.start_background_task(
            monitor_remote_system,
            payload["server_ip"],
            ssh_user,
            ssh_pass,
            sid,
            monitor_stop,
            time.time() + total_duration + 20,
        )
    else:
        emit("remote_system_status", {"disabled": True})
        closed_ports = find_closed_ports(payload["server_ip"], needed_ports)
        if closed_ports:
            preview = ", ".join(str(p) for p in closed_ports[:8])
            suffix = "..." if len(closed_ports) > 8 else ""
            emit(
                "test_error",
                {
                    "message": (
                        "Portas do iperf3 indisponiveis no servidor remoto: "
                        f"{preview}{suffix}. "
                        "No modo 'Ambos (simultaneo)' sao necessarias 2 portas por interface."
                    )
                },
            )
            return

    # Limpa testes anteriores forÃ§adamente
    with TEST_LOCK:
        if ACTIVE_TESTS:
            print(f"Parando {len(ACTIVE_TESTS)} testes ativos...", flush=True)
            for tid, task in list(ACTIVE_TESTS.items()):
                try:
                    task.process.terminate()
                    # Espera o processo morrer de fato para garantir que o cleanup do shell script rodou
                    task.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    print(f"Teste {tid} travou. ForÃ§ando kill...", flush=True)
                    task.process.kill()
                    task.process.wait()
                except Exception as e:
                    print(f"Erro ao parar teste {tid}: {e}", flush=True)
            ACTIVE_TESTS.clear()
            # Aguarda para o servidor remoto detectar a queda da conexÃ£o e liberar a porta (evita Server Busy)
            print("Aguardando 3s para liberaÃ§Ã£o de portas no servidor...", flush=True)
            socketio.emit("test_error", {"message": "Reiniciando... Aguardando liberaÃ§Ã£o de portas no servidor..."})
            time.sleep(3)

    error = validate_payload(payload)
    if error:
        emit("test_error", {"message": error})
        return

    server_ip = payload["server_ip"]
    duration = int(payload["duration"])
    interfaces = payload["interfaces"]
    base_port = int(payload.get("base_port", 5201))
    parallel = int(payload.get("parallel", 4))
    selected_mode = payload["mode"]

    if selected_mode == "both_sequential":
        modes = ["upload", "download"]
    elif selected_mode == "both":
        modes = ["upload", "download"]
    else:
        modes = [selected_mode]

    emit("test_started", {"interfaces": interfaces, "modes": modes})

    if selected_mode == "both_sequential":
        # Modo sequencial: upload em todas, espera, depois download em todas.
        socketio.start_background_task(
            run_sequential_both,
            server_ip,
            duration,
            interfaces,
            sid,
            base_port,
            parallel,
        )
    else:
        # Modos simultÃ¢neos: todos os testes ao mesmo tempo.
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
    socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)

