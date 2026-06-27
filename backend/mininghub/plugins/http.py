import time
import aiohttp
from .base import MinerPlugin, MinerSnapshot

def pick(data: dict, *names, default=None):
    for name in names:
        cur=data
        ok=True
        for part in name.split('.'):
            if isinstance(cur, dict) and part in cur: cur=cur[part]
            else: ok=False; break
        if ok: return cur
    return default

class HTTPPlugin(MinerPlugin):
    name='http'
    paths=('/api/system/info','/api/status','/status','/api/v1/info','/')
    async def probe(self, ip: str) -> MinerSnapshot | None:
        timeout=aiohttp.ClientTimeout(total=3)
        async with aiohttp.ClientSession(timeout=timeout) as client:
            for path in self.paths:
                started=time.perf_counter()
                try:
                    async with client.get(f'http://{ip}{path}') as resp:
                        if resp.status >= 400: continue
                        data=await resp.json(content_type=None)
                        return self.normalize(ip, data, (time.perf_counter()-started)*1000)
                except Exception:
                    continue
        return None
    def normalize(self, ip: str, data: dict, latency: float) -> MinerSnapshot:
        return MinerSnapshot(name=str(pick(data,'name','hostname','system.hostname',default=ip)), ip=ip, plugin=self.name, hostname=pick(data,'hostname','system.hostname'), model=pick(data,'model','device.model'), manufacturer=pick(data,'manufacturer','device.manufacturer'), firmware=pick(data,'firmware','version','system.firmware'), mac=pick(data,'mac','network.mac'), pool=pick(data,'pool','stratum.pool'), wallet=pick(data,'wallet','stratum.wallet'), worker=pick(data,'worker','stratum.worker'), temperature=pick(data,'temperature','temp','stats.temp'), hashrate=pick(data,'hashrate','hashRate','stats.hashrate'), clock=pick(data,'clock','frequency'), voltage=pick(data,'voltage','coreVoltage'), fan_speed=pick(data,'fan','fan_speed'), rssi=pick(data,'rssi','wifi.rssi'), ssid=pick(data,'ssid','wifi.ssid'), uptime_seconds=int(pick(data,'uptime','uptime_seconds',default=0) or 0), reboots=int(pick(data,'reboots',default=0) or 0), latency_ms=latency, raw=data)
