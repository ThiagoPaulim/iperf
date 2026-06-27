const api = (p, o={}) => fetch(p, {headers:{'Content-Type':'application/json', ...(localStorage.token?{Authorization:`Bearer ${localStorage.token}`}:{})}, ...o}).then(r=>r.json());
const fmt = n => n === null || n === undefined ? '-' : Number(n).toLocaleString(undefined,{maximumFractionDigits:2});
function card(title, value, icon){return `<div class="col-6 col-md-3 col-xxl-2"><div class="card metric"><div class="card-body"><div class="d-flex justify-content-between"><span>${title}</span><i class="bi ${icon}"></i></div><strong>${value}</strong></div></div></div>`}
async function load(){
 const [stats, miners] = await Promise.all([api('/api/dashboard'), api('/api/miners')]);
 document.querySelector('#cards').innerHTML = [card('Total',stats.total,'bi-hdd-network'),card('Online',stats.online,'bi-wifi'),card('Offline',stats.offline,'bi-wifi-off'),card('Hashrate',fmt(stats.hashrate_total),'bi-speedometer2'),card('Média',fmt(stats.hashrate_average),'bi-graph-up'),card('Temp média',fmt(stats.temperature_average),'bi-thermometer-half'),card('Shares OK',stats.shares_accepted,'bi-check2-circle'),card('Shares rejeitadas',stats.shares_rejected,'bi-x-circle')].join('');
 document.querySelector('#miners').innerHTML = miners.map(m=>`<tr><td>${m.name}</td><td><span class="badge text-bg-${m.status==='online'?'success':'secondary'}">${m.status}</span></td><td>${m.model||'-'}</td><td>${m.ip}</td><td>${fmt(m.hashrate)}</td><td>${fmt(m.temperature)}</td><td>${fmt(m.rssi)}</td><td>${m.pool||'-'}</td></tr>`).join('');
 document.querySelector('#status-map').innerHTML = miners.map(m=>`<span class="node ${m.status}">${m.name}</span>`).join('') || '<p class="text-secondary">Nenhum minerador cadastrado.</p>';
 chart.data.labels = miners.map(m=>m.name); chart.data.datasets[0].data = miners.map(m=>m.hashrate||0); chart.update();
}
const chart = new Chart(document.getElementById('hashrate'), {type:'bar', data:{labels:[], datasets:[{label:'Hashrate', data:[], backgroundColor:'#f59e0b'}]}, options:{responsive:true}});
document.querySelector('#theme').onclick=()=>{const h=document.documentElement; h.dataset.bsTheme=h.dataset.bsTheme==='dark'?'light':'dark'};
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');
load(); setInterval(load, 10000);
try { const ws = new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/api/websocket`); ws.onmessage=()=>load(); ws.onopen=()=>setInterval(()=>ws.send('ping'), 25000); } catch(e) {}
