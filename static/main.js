const socket = io();

const interfacesTable = document.getElementById("interfacesTable");
const resultsTable = document.getElementById("resultsTable");
const logBox = document.getElementById("log");
const startBtn = document.getElementById("startBtn");
const gaugeContainer = document.getElementById("gaugeContainer");

// Elementos da barra de status
const cpuVal = document.getElementById("cpuVal");
const cpuBar = document.getElementById("cpuBar");
const ramVal = document.getElementById("ramVal");
const ramBar = document.getElementById("ramBar");
const ramDetails = document.getElementById("ramDetails");

const throughputCtx = document.getElementById("throughputChart");

const throughputChart = new Chart(throughputCtx, {
  type: "line",
  data: { labels: [], datasets: [] },
  options: {
    responsive: true,
    animation: false,
    scales: {
      y: { title: { display: true, text: "Mbits/s" } },
    },
  },
});

/**
 * Armazena gauges dinâmicos criados por interface.
 * Formato: { "eth0": { upload: Chart, download: Chart }, ... }
 */
const interfaceGauges = {};

/**
 * Armazena o último throughput por interface/modo para soma total.
 * Formato: { upload: { "eth0": 450 }, download: { "eth0": 320 } }
 */
const latestThroughput = { upload: {}, download: {} };

function log(msg) {
  logBox.textContent += `[${new Date().toLocaleTimeString()}] ${msg}\n`;
  logBox.scrollTop = logBox.scrollHeight;
}

function colorForKey(key) {
  let hash = 0;
  for (let i = 0; i < key.length; i += 1) hash = key.charCodeAt(i) + ((hash << 5) - hash);
  return `hsl(${Math.abs(hash) % 360}, 80%, 60%)`;
}

function createGauge(ctx, label) {
  return new Chart(ctx, {
    type: "doughnut",
    data: {
      labels: ["Atual", "Restante"],
      datasets: [{ data: [0, 1000], backgroundColor: ["#22d3ee", "#1f2937"] }],
    },
    options: {
      circumference: 180,
      rotation: 270,
      plugins: {
        legend: { display: false },
        title: { display: true, text: `${label}: 0 Mbits/s`, color: "#e2e8f0" },
      },
    },
  });
}

function getDataset(label) {
  let ds = throughputChart.data.datasets.find((item) => item.label === label);
  if (!ds) {
    ds = {
      label,
      data: [],
      borderColor: colorForKey(label),
      backgroundColor: "transparent",
      tension: 0.2,
    };
    throughputChart.data.datasets.push(ds);
  }
  return ds;
}

/** Cria os gauges dinâmicos para cada interface selecionada. */
function createInterfaceGauges(interfaces, modes) {
  // Limpa gauges anteriores.
  gaugeContainer.innerHTML = "";

  // Destrói instâncias Chart.js anteriores.
  Object.values(interfaceGauges).forEach((gauges) => {
    if (gauges.upload) gauges.upload.destroy();
    if (gauges.download) gauges.download.destroy();
  });

  // Limpa referências.
  for (const key of Object.keys(interfaceGauges)) {
    delete interfaceGauges[key];
  }

  // Cria gauges para cada interface.
  interfaces.forEach((iface) => {
    const wrapper = document.createElement("div");
    wrapper.className = "interface-gauge-wrapper";

    const title = document.createElement("h3");
    title.textContent = iface;
    title.className = "gauge-iface-title";
    wrapper.appendChild(title);

    const pairDiv = document.createElement("div");
    pairDiv.className = "gauge-pair";

    const gauges = {};

    modes.forEach((mode) => {
      const modeDiv = document.createElement("div");
      modeDiv.className = "gauge-item";

      const modeLabel = document.createElement("h4");
      modeLabel.textContent = mode === "upload" ? "Upload" : "Download";
      modeDiv.appendChild(modeLabel);

      const canvas = document.createElement("canvas");
      canvas.id = `gauge-${iface}-${mode}`;
      modeDiv.appendChild(canvas);

      pairDiv.appendChild(modeDiv);
      gauges[mode] = createGauge(canvas, `${iface} ${mode}`);
    });

    wrapper.appendChild(pairDiv);
    gaugeContainer.appendChild(wrapper);
    interfaceGauges[iface] = gauges;
  });

  // NOTA: A seção "TOTAL (soma)" foi removida conforme solicitado.
}

/** Atualiza um gauge específico. */
function updateGauge(gauge, label, mbps) {
  const rounded = Math.round(mbps * 100) / 100;
  gauge.data.datasets[0].data = [rounded, Math.max(0, 1000 - rounded)];
  gauge.options.plugins.title.text = `${label}: ${rounded} Mbits/s`;
  gauge.update();
}

async function loadInterfaces() {
  const response = await fetch("/api/interfaces");
  const data = await response.json();

  interfacesTable.innerHTML = "";
  data.interfaces.forEach((iface) => {
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

startBtn.addEventListener("click", () => {
  const serverIp = document.getElementById("serverIp").value.trim();
  const duration = Number(document.getElementById("duration").value);
  const mode = document.getElementById("mode").value;
  const basePort = Number(document.getElementById("basePort").value) || 5201;
  const interfaces = [...document.querySelectorAll(".iface-check:checked")].map((el) => el.value);

  // Limpa estado anterior.
  resultsTable.innerHTML = "";
  throughputChart.data.labels = [];
  throughputChart.data.datasets = [];
  throughputChart.update();
  latestThroughput.upload = {};
  latestThroughput.download = {};
  log("Iniciando testes...");

  socket.emit("start_test", {
    server_ip: serverIp,
    duration,
    mode,
    base_port: basePort,
    interfaces,
  });
});

socket.on("test_started", (msg) => {
  log(`Testes iniciados para interfaces: ${msg.interfaces.join(", ")}. Modos: ${msg.modes.join(", ")}`);
  // Cria gauges dinâmicos para as interfaces selecionadas.
  createInterfaceGauges(msg.interfaces, msg.modes);
});

socket.on("phase_started", (msg) => {
  log(`▶ Fase iniciada: ${msg.mode.toUpperCase()}`);
});

socket.on("throughput_update", (msg) => {
  const label = `${msg.interface} (${msg.mode})`;
  const dataset = getDataset(label);

  const t = new Date(msg.timestamp * 1000).toLocaleTimeString();
  if (!throughputChart.data.labels.includes(t)) {
    throughputChart.data.labels.push(t);
  }
  dataset.data.push(msg.mbps);
  throughputChart.update();

  // Atualiza gauge da interface específica.
  if (interfaceGauges[msg.interface] && interfaceGauges[msg.interface][msg.mode]) {
    updateGauge(
      interfaceGauges[msg.interface][msg.mode],
      `${msg.interface} ${msg.mode}`,
      msg.mbps
    );
  }
});

socket.on("test_result", (msg) => {
  const row = document.createElement("tr");
  const status = msg.success ? "OK" : "ERRO";
  const result = msg.success ? `${msg.final_mbps} Mbits/s` : (msg.error || "Falha");
  row.innerHTML = `<td>${msg.interface}</td><td>${msg.mode}</td><td>${status}</td><td>${result}</td>`;
  resultsTable.appendChild(row);
  log(`Finalizado: ${msg.interface} ${msg.mode} -> ${result}`);
});

socket.on("test_error", (msg) => {
  log(`Erro: ${msg.message}`);
});

// Listener para monitoramento de sistema (CPU/RAM)
socket.on("system_status", (msg) => {
  cpuVal.textContent = `${msg.cpu}%`;
  cpuBar.style.width = `${msg.cpu}%`;

  ramVal.textContent = `${msg.ram_percent}%`;
  ramBar.style.width = `${msg.ram_percent}%`;
  ramDetails.textContent = `${msg.ram_used_gb} GB / ${msg.ram_total_gb} GB`;
});

loadInterfaces().catch((err) => log(`Falha ao carregar interfaces: ${err.message}`));
