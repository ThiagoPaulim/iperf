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
  const interfaces = [...document.querySelectorAll(".iface-check:checked")].map((el) => el.value);

  resultsTable.innerHTML = "";
  throughputChart.data.labels = [];
  throughputChart.data.datasets = [];
  throughputChart.update();
  log("Iniciando testes...");

  socket.emit("start_test", {
    server_ip: serverIp,
    duration,
    mode,
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

  if (msg.mode === "upload") {
    uploadGauge.data.datasets[0].data = [msg.mbps, Math.max(0, 1000 - msg.mbps)];
    uploadGauge.options.plugins.title.text = `Upload: ${msg.mbps} Mbits/s`;
    uploadGauge.update();
  }
  if (msg.mode === "download") {
    downloadGauge.data.datasets[0].data = [msg.mbps, Math.max(0, 1000 - msg.mbps)];
    downloadGauge.options.plugins.title.text = `Download: ${msg.mbps} Mbits/s`;
    downloadGauge.update();
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
