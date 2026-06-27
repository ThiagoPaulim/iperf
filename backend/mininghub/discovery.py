import asyncio, ipaddress
from .plugins.registry import PLUGINS

async def discover(cidr: str, limit: int = 512):
    hosts=[str(h) for h in ipaddress.ip_network(cidr, strict=False).hosts()][:limit]
    sem=asyncio.Semaphore(128)
    async def scan(ip):
        async with sem:
            for plugin in PLUGINS.values():
                snap=await plugin.probe(ip)
                if snap: return snap
        return None
    results=await asyncio.gather(*(scan(ip) for ip in hosts))
    return [r for r in results if r]
