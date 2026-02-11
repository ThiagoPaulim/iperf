#!/usr/bin/env python3
"""Aplicação Flask + Socket.IO para executar testes iperf3 por interface."""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import threading
import time
import psutil
import paramiko
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit

BASE_DIR = Path(__file__).resolve().parent
RUNNER_SCRIPT = BASE_DIR / "scripts" / "iperf-runner.sh"

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "iperf-web-secret")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

TEST_LOCK = threading.Lock()
ACTIVE_TESTS: Dict[str, "TestTask"] = {}


@dataclass
class TestTask:
    """Representa uma execução iperf em uma interface/modo."""

    interface: str
    mode: str
    process: subprocess.Popen


def run_command(cmd: List[str]) -> str:
    """Executa comando sem shell para evitar injeção de comandos."""

    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        return ""
    return completed.stdout


def parse_mbps(value: float, unit: str) -> float:
    """Converte K/M/G bits/sec para Mbits/sec."""

    unit = (unit or "M").upper()
    if unit == "G":
        return value * 1000
    if unit == "K":
        return value / 1000
    return value


def list_interfaces() -> List[dict]:
    """Coleta interfaces (exceto loopback) e dados físicos via ethtool."""

    output = run_command(["ip", "-o", "link", "show"])
    interfaces: List[dict] = []

    for line in output.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 2:
            continue
        name = parts[1].strip()
        # Remove o sufixo @ifX de interfaces virtuais (ex: eth0@if5 -> eth0).
        if "@" in name:
            name = name.split("@", 1)[0]
        # Ignora loopback.
        if name == "lo":
            continue

        # Obtém IPv4 principal da interface.
        addr_out = run_command(["ip", "-4", "-o", "addr", "show", "dev", name])
        ipv4 = None
        if addr_out:
            match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/", addr_out)
            if match:
                ipv4 = match.group(1)

        # Coleta atributos físicos com ethtool.
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
            return f"Campo obrigatório ausente: {key}"

    try:
        ipaddress.ip_address(payload["server_ip"])
    except ValueError:
        return "IP do servidor inválido."

    try:
        duration = int(payload["duration"])
    except (TypeError, ValueError):
        return "Tempo do teste deve ser numérico."

    if duration < 1 or duration > 3600:
        return "Tempo do teste deve estar entre 1 e 3600 segundos."

    if payload["mode"] not in {"upload", "download", "both", "both_sequential"}:
        return "Modo inválido."

    if not isinstance(payload["interfaces"], list) or not payload["interfaces"]:
        return "Selecione ao menos uma interface."

    try:
        base_port = int(payload.get("base_port", 5201))
    except (TypeError, ValueError):
        return "Porta base deve ser numérica."

    if base_port < 1 or base_port > 65535:
        return "Porta base deve estar entre 1 e 65535."

    try:
        parallel = int(payload.get("parallel", 4))
    except (TypeError, ValueError):
        return "Parallel deve ser numérico."

    if parallel < 1 or parallel > 64:
        return "Parallel deve estar entre 1 e 64."

    available = {item["name"] for item in list_interfaces()}
    for iface in payload["interfaces"]:
        if iface not in available:
            return f"Interface inválida: {iface}"

    return None


def run_single_test(
    server_ip: str, duration: int, interface: str, mode: str, sid: str, port: int, parallel: int
) -> None:
    """Executa um único fluxo iperf e envia atualizações em tempo real."""

    task_id = f"{interface}:{mode}"
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
    # Formato padrão: [  5] ...
    # Formato SUM:    [SUM] ...
    line_pattern = re.compile(
        r"\[\s*(\d+|SUM)\]\s+\d+\.\d+-\d+\.\d+\s+sec\s+\S+\s+(\S+Bytes|Bytes)\s+([\d.]+)\s+([KMG])bits/sec"
    )

    # Regex para capturar métricas iniciais (Ping)
    # Formato esperado: [METRICS] Ping: 15.20 ms
    metrics_pattern = re.compile(r"\[METRICS\] Ping: ([\d.]+) ms")

    try:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.strip()
            # Log para debug em tempo real no terminal do container
            print(f"[iperf3 raw] {interface}:{mode} -> {line}", flush=True)

            # Checa se é linha de métrica
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
                # Se usarmos parallel = 1, queremos a linha normal (que tem ID numérico).
                if parallel > 1 and stream_id != "SUM":
                    continue
                if parallel == 1 and stream_id == "SUM":
                    continue

                # O grupo 3 é valor, grupo 4 é unidade.
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
    """Executa upload em todas as interfaces, espera, depois download."""

    for phase_mode in ["upload", "download"]:
        socketio.emit("phase_started", {"mode": phase_mode}, room=sid)
        threads = []
        for idx, iface in enumerate(interfaces):
            port = base_port + idx
            t = threading.Thread(
                target=run_single_test,
                args=(server_ip, duration, iface, phase_mode, sid, port, parallel),
            )
            t.start()
            threads.append(t)
        for t in threads:
            t.join()


def setup_remote_server(ip, user, password, ports):
    """Conecta via SSH e inicia instâncias do iperf3 nas portas necessárias."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        print(f"Conectando ao servidor remoto {ip} via SSH...", flush=True)
        client.connect(ip, username=user, password=password, timeout=5)
        
        running_ports = []
        for port in ports:
            # Mata qualquer processo já rodando nesta porta para garantir estado limpo
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

        return True, f"Serviços iniciados nas portas: {running_ports}"

    except Exception as e:
        return False, f"Erro SSH: {str(e)}"
    finally:
        client.close()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/interfaces", methods=["GET"])
def get_interfaces():
    return jsonify({"interfaces": list_interfaces()})


@socketio.on("start_test")
def start_test(payload: dict):
    """Inicia testes simultâneos de acordo com interfaces e modo selecionados."""
    
    error = validate_payload(payload)
    if error:
        emit("test_error", {"message": error})
        return

    # Verifica configuração remota opcional
    configure_server = payload.get("configure_server", False)
    ssh_user = payload.get("ssh_user")
    ssh_pass = payload.get("ssh_pass")
    
    interfaces = payload["interfaces"]
    base_port = int(payload.get("base_port", 5201))
    parallel = int(payload.get("parallel", 4))
    selected_mode = payload["mode"]
    
    # Portas necessárias
    # Se modo for 'both' (simultâneo), precisamos de 2 portas por interface (uma pra up, uma pra down).
    # Em 'both_sequential', reutilizamos a mesma porta pois as fases não se sobrepõem.
    needed_ports = []
    if selected_mode == "both":
        total_slots = len(interfaces) * 2
        needed_ports = [base_port + i for i in range(total_slots)]
    else:
        # upload, download e both_sequential usam 1 porta por interface
        needed_ports = [base_port + i for i in range(len(interfaces))]

    # Se configuração automática estiver ativa, tenta configurar o servidor antes
    if configure_server:
        if not ssh_user or not ssh_pass:
            emit("test_error", {"message": "Usuário e Senha SSH são obrigatórios para configuração automática."})
            return

        emit("test_error", {"message": "Configurando servidor remoto via SSH..."}) # Usa error message para toast/log
        success, msg = setup_remote_server(payload["server_ip"], ssh_user, ssh_pass, needed_ports)
        if not success:
            emit("test_error", {"message": f"Falha na configuração remota: {msg}"})
            return
        
        print(f"Servidor remoto configurado: {msg}", flush=True)
        # Dá um tempinho para o iperf3 subir no remoto
        time.sleep(2)

    # Limpa testes anteriores forçadamente
    with TEST_LOCK:
        if ACTIVE_TESTS:
            print(f"Parando {len(ACTIVE_TESTS)} testes ativos...", flush=True)
            for tid, task in list(ACTIVE_TESTS.items()):
                try:
                    task.process.terminate()
                    # Espera o processo morrer de fato para garantir que o cleanup do shell script rodou
                    task.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    print(f"Teste {tid} travou. Forçando kill...", flush=True)
                    task.process.kill()
                    task.process.wait()
                except Exception as e:
                    print(f"Erro ao parar teste {tid}: {e}", flush=True)
            ACTIVE_TESTS.clear()
            # Aguarda para o servidor remoto detectar a queda da conexão e liberar a porta (evita Server Busy)
            print("Aguardando 3s para liberação de portas no servidor...", flush=True)
            socketio.emit("test_error", {"message": "Reiniciando... Aguardando liberação de portas no servidor..."})
            time.sleep(3)

    error = validate_payload(payload)
    if error:
        emit("test_error", {"message": error})
        return

    sid = request.sid

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
        # Modos simultâneos: todos os testes ao mesmo tempo.
        port_idx = 0
        for iface in interfaces:
            for mode in modes:
                port = base_port + port_idx
                socketio.start_background_task(
                    run_single_test,
                    server_ip,
                    duration,
                    iface,
                    mode,
                    sid,
                    port,
                    parallel,
                )
                port_idx += 1


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
