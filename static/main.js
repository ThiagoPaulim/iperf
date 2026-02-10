const socket = io();

const interfacesTable = document.getElementById("interfacesTable");
const resultsTable = document.getElementById("resultsTable");
const logBox = document.getElementById("log");
const startBtn = document.getElementById("startBtn");

const throughputCtx = document.getElementById("throughputChart");
const uploadGaugeCtx = document.getElementById("uploadGauge");
const downloadGaugeCtx = document.getElementById("downloadGauge");

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
        title: { display: true, text: `${label}: 0 Mbits/s` },
      },
    },
  });
}

const uploadGauge = createGauge(uploadGaugeCtx, "Upload");
const downloadGauge = createGauge(downloadGaugeCtx, "Download");

// Armazena o último throughput reportado por cada interface/modo
// para calcular a soma total (agregação).
const latestThroughput = {
  upload: {},   // { "eth0": 450, "eth1": 520 }
  download: {}, // { "eth0": 320, "eth1": 410 }
};

function log(msg) {
  logBox.textContent += `[${new Date().toLocaleTimeString()}] ${msg}\n`;
  logBox.scrollTop = logBox.scrollHeight;
}

function colorForKey(key) {
  let hash = 0;
  for (let i = 0; i < key.length; i += 1) hash = key.charCodeAt(i) + ((hash << 5) - hash);
  return `hsl(${Math.abs(hash) % 360}, 80%, 60%)`;
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

/** Calcula a soma do throughput de todas as interfaces para um modo. */
function sumThroughput(mode) {
  const values = Object.values(latestThroughput[mode] || {});
  return values.reduce((acc, val) => acc + val, 0);
}

/** Atualiza o gauge com a soma total de todas as interfaces. */
function updateGauge(gauge, label, totalMbps) {
  const rounded = Math.round(totalMbps * 100) / 100;
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

  // Atualiza o throughput individual desta interface/modo.
  if (msg.mode === "upload" || msg.mode === "download") {
    latestThroughput[msg.mode][msg.interface] = msg.mbps;
  }

  // Atualiza gauges com a SOMA de todas as interfaces.
  if (msg.mode === "upload") {
    updateGauge(uploadGauge, "Upload (total)", sumThroughput("upload"));
  }
  if (msg.mode === "download") {
    updateGauge(downloadGauge, "Download (total)", sumThroughput("download"));
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

loadInterfaces().catch((err) => log(`Falha ao carregar interfaces: ${err.message}`));
