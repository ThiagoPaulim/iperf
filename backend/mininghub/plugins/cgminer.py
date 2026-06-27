import asyncio, json
from .base import MinerPlugin, MinerSnapshot
class CGMinerPlugin(MinerPlugin):
    name='cgminer'
    async def probe(self, ip: str) -> MinerSnapshot | None:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, 4028), timeout=2)
            writer.write(json.dumps({'command':'summary'}).encode()+b'\n'); await writer.drain()
            data = await asyncio.wait_for(reader.read(8192), timeout=2)
            writer.close(); await writer.wait_closed()
            text=data.decode(errors='ignore').strip('\x00\n '); raw=json.loads(text) if text.startswith('{') else {'summary':text}
            summary=(raw.get('SUMMARY') or [{}])[0] if isinstance(raw.get('SUMMARY'), list) else raw
            return MinerSnapshot(name=ip, ip=ip, plugin=self.name, model='CGMiner compatible', hashrate=summary.get('MHS av') or summary.get('GHS av'), raw=raw)
        except Exception: return None
