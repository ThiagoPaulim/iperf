from datetime import datetime, timezone
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from .models import Miner, MinerStatus, Telemetry
from .plugins.base import MinerSnapshot

def apply_snapshot(miner: Miner, snap: MinerSnapshot):
    for field in ['name','hostname','model','manufacturer','firmware','mac','pool','wallet','worker','temperature','hashrate','clock','voltage','fan_speed','chip_temperature','board_temperature','heap','cpu','ram','rssi','ssid','wifi_channel','ota_version','uptime_seconds','reboots','errors','latency_ms']:
        value=getattr(snap, field)
        if value is not None: setattr(miner, field, value)
    miner.status=MinerStatus(snap.status); miner.last_seen=datetime.now(timezone.utc); miner.plugin=snap.plugin

async def upsert_snapshot(session: AsyncSession, snap: MinerSnapshot, approve: bool=False) -> Miner:
    miner=(await session.execute(select(Miner).where(Miner.ip==snap.ip, Miner.plugin==snap.plugin))).scalar_one_or_none()
    if not miner:
        miner=Miner(name=snap.name, ip=snap.ip, plugin=snap.plugin, approved=approve); session.add(miner)
    apply_snapshot(miner, snap); await session.flush()
    session.add(Telemetry(miner_id=miner.id, hashrate=snap.hashrate, temperature=snap.temperature, rssi=snap.rssi, clock=snap.clock, voltage=snap.voltage, latency_ms=snap.latency_ms, raw=snap.raw))
    return miner

async def dashboard(session: AsyncSession):
    miners=(await session.execute(select(Miner))).scalars().all()
    total=len(miners); online=sum(1 for m in miners if m.status==MinerStatus.online); offline=sum(1 for m in miners if m.status==MinerStatus.offline)
    hashrates=[m.hashrate or 0 for m in miners]; temps=[m.temperature for m in miners if m.temperature is not None]
    shares=(await session.execute(select(func.coalesce(func.sum(Telemetry.shares_accepted),0), func.coalesce(func.sum(Telemetry.shares_rejected),0)))).one()
    return {'total':total,'online':online,'offline':offline,'hashrate_total':sum(hashrates),'hashrate_average':sum(hashrates)/total if total else 0,'temperature_average':sum(temps)/len(temps) if temps else None,'shares_accepted':shares[0],'shares_rejected':shares[1],'hottest_miner':max(miners,key=lambda m:m.temperature or -999, default=None),'best_miner':max(miners,key=lambda m:m.hashrate or -1, default=None),'worst_miner':min(miners,key=lambda m:m.hashrate or 10**18, default=None)}
