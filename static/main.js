const socket = io();

// Elementos da DOM
const interfacesTable = document.getElementById("interfacesTable");
const resultsTable = document.getElementById("resultsTable");
const metricsTable = document.getElementById("metricsTable");
const logBox = document.getElementById("log");
const startBtn = document.getElementById("startBtn");
const gaugeContainer = document.getElementById("gaugeContainer");
const cpuVal = document.getElementById("cpuVal");
const cpuBar = document.getElementById("cpuBar");
const ramVal = document.getElementById("ramVal");
const ramBar = document.getElementById("ramBar");
const ramDetails = document.getElementById("ramDetails");
const remoteCpuVal = document.getElementById("remoteCpuVal");
const remoteCpuBar = document.getElementById("remoteCpuBar");
const remoteRamVal = document.getElementById("remoteRamVal");
const remoteRamBar = document.getElementById("remoteRamBar");
const remoteRamDetails = document.getElementById("remoteRamDetails");
const throughputCtx = document.getElementById("throughputChart");

// VariÃ¡veis Globais
const interfaceGauges = {};
const interfaceSpeeds = {};
const latestThroughput = { upload: {}, download: {} };
let testInProgress = false;
let runStarted = false;
let expectedResults = 0;
let receivedResults = 0;
let currentRunId = null;

function isCurrentRunMessage(msg) {
  if (!msg || !Object.prototype.hasOwnProperty.call(msg, "run_id")) return true;
  if (!currentRunId) return true;
  return msg.run_id === currentRunId;
}

function parseSpeed(speedStr) {
  if (!speedStr || speedStr === "N/A") return 1000;
  // Extrai o nÃºmero de strings como "2500Mb/s", "10000Mb/s", etc.
  const match = speedStr.match(/(\d+)/);
  return match ? parseInt(match[1]) : 1000;
}

function log(msg) {
  logBox.textContent += `[${new Date().toLocaleTimeString()}] ${msg}\n`;
  logBox.scrollTop = logBox.scrollHeight;
}

function resetRemoteSystemStats() {
  if (!remoteCpuVal || !remoteCpuBar || !remoteRamVal || !remoteRamBar || !remoteRamDetails) return;
  remoteCpuVal.textContent = "N/A";
  remoteCpuBar.style.width = "0%";
  remoteRamVal.textContent = "N/A";
  remoteRamBar.style.width = "0%";
  remoteRamDetails.textContent = "N/A";
}

// ConfiguraÃ§Ã£o do GrÃ¡fico de Linha (Throughput)
const throughputChart = new Chart(throughputCtx, {
  type: "line",
  data: { labels: [], datasets: [] },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 0 },
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { labels: { color: '#cbd5e1', font: { family: 'Segoe UI', size: 11 } } },
      tooltip: {
        backgroundColor: 'rgba(15, 23, 42, 0.9)',
        titleColor: '#e2e8f0',
        bodyColor: '#cbd5e1',
        borderColor: 'rgba(51, 65, 85, 0.5)',
        borderWidth: 1,
        padding: 10,
        displayColors: true,
      }
    },
    scales: {
      x: {
        grid: { display: false, color: '#334155' },
        ticks: { color: '#94a3b8', maxTicksLimit: 8 }
      },
      y: {
        grid: { color: '#334155', borderDash: [5, 5] },
        ticks: { color: '#94a3b8' },
        title: { display: true, text: "Mbits/s", color: '#64748b', font: { size: 10, weight: 600 } },
        beginAtZero: true
      },
    },
    elements: {
      line: { tension: 0.4, borderWidth: 2 },
      point: { radius: 0, hitRadius: 10, hoverRadius: 4 }
    }
  },
});

// Cores e UtilitÃ¡rios
const modernColors = [
  '#22d3ee', '#f472b6', '#a78bfa', '#34d399', '#fbbf24', '#60a5fa'
];

function colorForIndex(index) {
  return modernColors[index % modernColors.length];
}

function colorForKey(key) {
  let hash = 0;
  for (let i = 0; i < key.length; i += 1) hash = key.charCodeAt(i) + ((hash << 5) - hash);
  return colorForIndex(Math.abs(hash));
}

// Plugin para desenhar texto no centro do Doughnut
const centerTextPlugin = {
  id: 'centerText',
  beforeDraw: function (chart) {
    if (chart.config.type !== 'doughnut') return;
    const width = chart.width,
      height = chart.height,
      ctx = chart.ctx;

    ctx.restore();

    // ConfiguraÃ§Ã£o do texto
    const fontSize = (height / 114).toFixed(2);
    ctx.font = `bold ${fontSize}em Segoe UI`;
    ctx.textBaseline = "middle";
    ctx.fillStyle = "#e2e8f0";

    const text = chart.config.options.plugins.centerText.text || "0.00";
    const textX = Math.round((width - ctx.measureText(text).width) / 2);
    const textY = height / 1.8; // Um pouco abaixo do meio visual devido ao arco

    ctx.fillText(text, textX, textY);

    // Unidade abaixo
    ctx.font = `normal ${(height / 200).toFixed(2)}em Segoe UI`;
    ctx.fillStyle = "#94a3b8";
    const unit = "Mbps";
    const unitX = Math.round((width - ctx.measureText(unit).width) / 2);
    ctx.fillText(unit, unitX, textY + height * 0.15);

    ctx.save();
  }
};

Chart.register(centerTextPlugin);

function createGauge(ctx, label, maxSpeed = 1000) {
  // Ajusta o maxSpeed para ser pelo menos o mbps atual ou 1000
  const displayMax = Math.max(maxSpeed, 1000);
  // label vem como "eth0 upload". Vamos extrair apenas o modo.
  const mode = label.split(' ').pop().toUpperCase();

  return new Chart(ctx, {
    type: "doughnut",
    data: {
      labels: ["Atual", "Restante"],
      datasets: [{
        data: [0, displayMax],
        backgroundColor: (context) => {
          const chart = context.chart;
          const { ctx, chartArea } = chart;
          if (!chartArea) return null;
          const gradient = ctx.createLinearGradient(0, chartArea.bottom, 0, chartArea.top);
          gradient.addColorStop(0, '#0ea5e9');
          gradient.addColorStop(1, '#22d3ee');
          return [gradient, '#1e293b'];
        },
        borderWidth: 0,
        borderRadius: 20,
        cutout: '85%',
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: true,
      circumference: 260,
      rotation: 230,
      animation: { animateRotate: false, animateScale: false },
      plugins: {
        legend: { display: false },
        title: {
          display: true,
          text: mode, // Apenas UPLOAD ou DOWNLOAD
          color: "#94a3b8",
          font: { size: 12, weight: 'bold', family: 'Segoe UI' },
          padding: { bottom: 10 },
          position: 'top'
        },
        tooltip: { enabled: false },
        centerText: { text: "0.00" } // Estado inicial
      },
      // Passamos o maxSpeed para ser acessÃ­vel no update
      maxSpeed: displayMax
    },
  });
}

function getDataset(label) {
  let ds = throughputChart.data.datasets.find((item) => item.label === label);
  if (!ds) {
    const color = colorForIndex(throughputChart.data.datasets.length);
    ds = {
      label,
      data: [],
      borderColor: color,
      backgroundColor: (context) => {
        const ctx = context.chart.ctx;
        const gradient = ctx.createLinearGradient(0, 0, 0, 400);
        gradient.addColorStop(0, color + '40');
        gradient.addColorStop(1, color + '00');
        return gradient;
      },
      fill: true,
      tension: 0.4,
    };
    throughputChart.data.datasets.push(ds);
  }
  return ds;
}

function createInterfaceGauges(interfaces, modes) {
  gaugeContainer.innerHTML = "";

  Object.values(interfaceGauges).forEach((gauges) => {
    if (gauges.upload) gauges.upload.destroy();
    if (gauges.download) gauges.download.destroy();
  });

  for (const key of Object.keys(interfaceGauges)) {
    delete interfaceGauges[key];
  }

  interfaces.forEach((iface) => {
    const wrapper = document.createElement("div");
    wrapper.className = "interface-gauge-wrapper";

    const title = document.createElement("h3");
    title.textContent = iface;
    title.className = "gauge-iface-title";
    wrapper.appendChild(title);

    const pairDiv = document.createElement("div");
    pairDiv.className = "gauge-pair";

    const maxSpeed = interfaceSpeeds[iface] || 1000;
    const gauges = {};

    modes.forEach((mode) => {
      const modeDiv = document.createElement("div");
      modeDiv.className = "gauge-item";

      const canvas = document.createElement("canvas");
      canvas.id = `gauge-${iface}-${mode}`;
      modeDiv.appendChild(canvas);

      pairDiv.appendChild(modeDiv);
      gauges[mode] = createGauge(canvas, `${iface} ${mode}`, maxSpeed);
    });

    wrapper.appendChild(pairDiv);
    gaugeContainer.appendChild(wrapper);
    interfaceGauges[iface] = gauges;
  });
}

function updateGauge(gauge, label, mbps) {
  const rounded = (Math.round(mbps * 100) / 100).toFixed(2);
  const maxSpeed = gauge.options.maxSpeed || 1000;

  // Se o throughput ultrapassar o maxSpeed (ex: burst), ajustamos o grÃ¡fico
  const currentMax = Math.max(maxSpeed, mbps);

  gauge.data.datasets[0].data = [mbps, Math.max(0, currentMax - mbps)];
  gauge.options.plugins.centerText.text = rounded;
  gauge.update();
}

async function loadInterfaces() {
  const response = await fetch("/api/interfaces");
  const data = await response.json();

  interfacesTable.innerHTML = "";
  data.interfaces.forEach((iface) => {
    interfaceSpeeds[iface.name] = parseSpeed(iface.speed);
    const row = document.createElement("tr");
    row.innerHTML = `
      <td><input type="checkbox" class="iface-check" value="${iface.name}" /></td>
      <td>${iface.name}</td>
      <td>${iface.ipv4 || "N/A"}</td>
      <td>${iface.speed}</td>
      <td>${iface.duplex}</td>
      <td>${iface.autoneg}</td>
    `;
    interfacesTable.appendChild(row);
  });
}

const toastContainer = document.getElementById("toastContainer");

function showToast(message, type = "info") {
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;

  toastContainer.appendChild(toast);

  // Remove apÃ³s 5 segundos
  setTimeout(() => {
    toast.style.animation = "fadeOut 0.3s forwards";
    toast.addEventListener("animationend", () => {
      toast.remove();
    });
  }, 5000);
}

const configureServerCheck = document.getElementById("configureServer");
const sshFieldsDiv = document.getElementById("sshFields");

if (configureServerCheck) {
  configureServerCheck.addEventListener("change", () => {
    sshFieldsDiv.style.display = configureServerCheck.checked ? "grid" : "none";
  });
}

startBtn.addEventListener("click", () => {
  if (testInProgress) {
    showToast("Aguarde o teste atual finalizar.", "error");
    return;
  }

  const serverIp = document.getElementById("serverIp").value.trim();
  const duration = Number(document.getElementById("duration").value);
  const mode = document.getElementById("mode").value;
  const basePort = Number(document.getElementById("basePort").value) || 5201;
  const parallel = Number(document.getElementById("parallel").value) || 4;
  const interfaces = [...document.querySelectorAll(".iface-check:checked")].map((el) => el.value);

  const configureServer = configureServerCheck ? configureServerCheck.checked : false;
  const sshUser = document.getElementById("sshUser") ? document.getElementById("sshUser").value.trim() : "";
  const sshPass = document.getElementById("sshPass") ? document.getElementById("sshPass").value : "";

  // ValidaÃ§Ãµes com Toast
  if (!serverIp) {
    showToast("Por favor, informe o IP do servidor.", "error");
    return;
  }
  if (interfaces.length === 0) {
    showToast("Selecione pelo menos uma interface de rede.", "error");
    return;
  }
  if (configureServer && (!sshUser || !sshPass)) {
    showToast("Preencha UsuÃ¡rio e Senha para configuraÃ§Ã£o SSH.", "error");
    return;
  }

  resultsTable.innerHTML = "";
  if (metricsTable) {
    metricsTable.innerHTML = `<tr><td colspan="3" style="text-align:center; color:#64748b;">Aguardando mÃ©tricas...</td></tr>`;
  }
  throughputChart.data.labels = [];
  throughputChart.data.datasets = [];
  throughputChart.update();
  latestThroughput.upload = {};
  latestThroughput.download = {};
  testInProgress = true;
  runStarted = false;
  expectedResults = 0;
  receivedResults = 0;
  currentRunId = null;
  startBtn.disabled = true;
  startBtn.textContent = "Teste em andamento...";

  log(`Iniciando testes com ${parallel} streams paralelas...`);
  if (configureServer) {
    log(`ConfiguraÃ§Ã£o remota via SSH ativada.`);
  } else {
    resetRemoteSystemStats();
  }

  socket.emit("start_test", {
    server_ip: serverIp,
    duration,
    mode,
    base_port: basePort,
    parallel,
    interfaces,
    configure_server: configureServer,
    ssh_user: sshUser,
    ssh_pass: sshPass
  });
});

socket.on("test_started", (msg) => {
  if (!isCurrentRunMessage(msg)) return;
  currentRunId = msg.run_id || currentRunId;
  runStarted = true;
  expectedResults = (msg.interfaces?.length || 0) * (msg.modes?.length || 0);
  receivedResults = 0;
  showToast(`Teste iniciado! Interfaces: ${msg.interfaces.join(", ")}`, "success");
  log(`Testes iniciados para interfaces: ${msg.interfaces.join(", ")}. Modos: ${msg.modes.join(", ")}`);
  createInterfaceGauges(msg.interfaces, msg.modes);
});

socket.on("test_error", (msg) => {
  if (!isCurrentRunMessage(msg)) return;
  showToast(msg.message, "error");
  log(`Erro: ${msg.message}`);
  if (!runStarted) {
    testInProgress = false;
    startBtn.disabled = false;
    startBtn.textContent = "Iniciar teste";
  }
});

socket.on("phase_started", (msg) => {
  if (!isCurrentRunMessage(msg)) return;
  log(`â–¶ Fase iniciada: ${msg.mode.toUpperCase()}`);
});

socket.on("metrics_update", (msg) => {
  if (!isCurrentRunMessage(msg)) return;
  if (!metricsTable) return;
  const placeholder = metricsTable.querySelector("td[colspan]");
  if (placeholder) {
    metricsTable.removeChild(placeholder.parentElement);
  }
  const row = document.createElement("tr");
  row.innerHTML = `
      <td>${msg.interface}</td>
      <td>${msg.mode}</td>
      <td>${msg.ping} ms</td>
    `;
  metricsTable.appendChild(row);
});

socket.on("throughput_update", (msg) => {
  if (!isCurrentRunMessage(msg)) return;
  const label = `${msg.interface} (${msg.mode})`;
  const dataset = getDataset(label);

  const t = new Date(msg.timestamp * 1000).toLocaleTimeString();
  if (!throughputChart.data.labels.includes(t)) {
    throughputChart.data.labels.push(t);
  }
  dataset.data.push(msg.mbps);
  throughputChart.update();

  if (interfaceGauges[msg.interface] && interfaceGauges[msg.interface][msg.mode]) {
    updateGauge(
      interfaceGauges[msg.interface][msg.mode],
      `${msg.interface} ${msg.mode}`,
      msg.mbps
    );
  }
});

socket.on("test_result", (msg) => {
  if (!isCurrentRunMessage(msg)) return;
  const row = document.createElement("tr");
  const status = msg.success ? "OK" : "ERRO";
  const result = msg.success ? `${msg.final_mbps} Mbps` : (msg.error || "Falha");
  row.innerHTML = `<td>${msg.interface}</td><td>${msg.mode}</td><td>${status}</td><td>${result}</td>`;
  resultsTable.appendChild(row);
  log(`Finalizado: ${msg.interface} ${msg.mode} -> ${result}`);

  if (runStarted) {
    receivedResults += 1;
    if (expectedResults > 0 && receivedResults >= expectedResults) {
      testInProgress = false;
      runStarted = false;
      startBtn.disabled = false;
      startBtn.textContent = "Iniciar teste";
      log("Ciclo de testes finalizado.");
    }
  }
});

socket.on("system_status", (msg) => {
  cpuVal.textContent = `${msg.cpu}%`;
  cpuBar.style.width = `${msg.cpu}%`;

  ramVal.textContent = `${msg.ram_percent}%`;
  ramBar.style.width = `${msg.ram_percent}%`;
  ramDetails.textContent = `${msg.ram_used_gb} GB / ${msg.ram_total_gb} GB`;
});

socket.on("remote_system_status", (msg) => {
  if (!isCurrentRunMessage(msg)) return;
  if (!remoteCpuVal || !remoteCpuBar || !remoteRamVal || !remoteRamBar || !remoteRamDetails) return;

  if (msg.disabled) {
    resetRemoteSystemStats();
    return;
  }

  if (msg.error) {
    log(`Erro monitor remoto: ${msg.error}`);
    return;
  }

  if (typeof msg.cpu === "number") {
    remoteCpuVal.textContent = `${msg.cpu}%`;
    remoteCpuBar.style.width = `${msg.cpu}%`;
  }

  if (typeof msg.ram_percent === "number") {
    remoteRamVal.textContent = `${msg.ram_percent}%`;
    remoteRamBar.style.width = `${msg.ram_percent}%`;
  }

  if (typeof msg.ram_used_gb === "number" && typeof msg.ram_total_gb === "number") {
    remoteRamDetails.textContent = `${msg.ram_used_gb} GB / ${msg.ram_total_gb} GB`;
  }
});

resetRemoteSystemStats();
loadInterfaces().catch((err) => log(`Falha ao carregar interfaces: ${err.message}`));


