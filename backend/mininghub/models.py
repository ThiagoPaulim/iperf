import enum, uuid
from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase): pass
class Role(str, enum.Enum): admin='admin'; operator='operator'; readonly='readonly'
class MinerStatus(str, enum.Enum): online='online'; offline='offline'; warning='warning'; unknown='unknown'
class EventType(str, enum.Enum): reboot='reboot'; ota='ota'; pool_change='pool_change'; wallet_change='wallet_change'; offline='offline'; online='online'; login='login'; alert='alert'; discovery='discovery'

def now(): return datetime.now(timezone.utc)

class User(Base):
    __tablename__='users'
    id: Mapped[int]=mapped_column(primary_key=True)
    username: Mapped[str]=mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str]=mapped_column(String(255))
    role: Mapped[Role]=mapped_column(Enum(Role), default=Role.readonly)
    two_factor_enabled: Mapped[bool]=mapped_column(Boolean, default=False)
    is_active: Mapped[bool]=mapped_column(Boolean, default=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)

class Miner(Base):
    __tablename__='miners'
    id: Mapped[int]=mapped_column(primary_key=True)
    uuid: Mapped[str]=mapped_column(String(36), default=lambda: str(uuid.uuid4()), unique=True)
    name: Mapped[str]=mapped_column(String(120), index=True)
    hostname: Mapped[str|None]=mapped_column(String(255))
    model: Mapped[str|None]=mapped_column(String(120), index=True)
    manufacturer: Mapped[str|None]=mapped_column(String(120), index=True)
    firmware: Mapped[str|None]=mapped_column(String(120))
    ip: Mapped[str]=mapped_column(String(45), index=True)
    mac: Mapped[str|None]=mapped_column(String(32), index=True)
    gateway: Mapped[str|None]=mapped_column(String(45)); dns: Mapped[str|None]=mapped_column(String(120))
    pool: Mapped[str|None]=mapped_column(String(255)); wallet: Mapped[str|None]=mapped_column(String(255)); worker: Mapped[str|None]=mapped_column(String(120))
    status: Mapped[MinerStatus]=mapped_column(Enum(MinerStatus), default=MinerStatus.unknown, index=True)
    temperature: Mapped[float|None]=mapped_column(Float); hashrate: Mapped[float|None]=mapped_column(Float); clock: Mapped[float|None]=mapped_column(Float); voltage: Mapped[float|None]=mapped_column(Float)
    fan_speed: Mapped[float|None]=mapped_column(Float); chip_temperature: Mapped[float|None]=mapped_column(Float); board_temperature: Mapped[float|None]=mapped_column(Float)
    heap: Mapped[float|None]=mapped_column(Float); cpu: Mapped[float|None]=mapped_column(Float); ram: Mapped[float|None]=mapped_column(Float)
    rssi: Mapped[float|None]=mapped_column(Float); ssid: Mapped[str|None]=mapped_column(String(120)); wifi_channel: Mapped[int|None]=mapped_column(Integer)
    ota_version: Mapped[str|None]=mapped_column(String(120)); last_seen: Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); uptime_seconds: Mapped[int]=mapped_column(Integer, default=0)
    reboots: Mapped[int]=mapped_column(Integer, default=0); errors: Mapped[str|None]=mapped_column(Text); latency_ms: Mapped[float|None]=mapped_column(Float); ping_ms: Mapped[float|None]=mapped_column(Float)
    timezone: Mapped[str|None]=mapped_column(String(80)); location: Mapped[str|None]=mapped_column(String(120)); group: Mapped[str|None]=mapped_column(String(120)); tags: Mapped[list]=mapped_column(JSON, default=list)
    approved: Mapped[bool]=mapped_column(Boolean, default=False); plugin: Mapped[str]=mapped_column(String(80), default='http')
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now); updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    telemetry: Mapped[list['Telemetry']]=relationship(back_populates='miner', cascade='all, delete-orphan')
    __table_args__=(UniqueConstraint('ip','plugin',name='uq_miner_ip_plugin'),)

class Telemetry(Base):
    __tablename__='telemetry'
    id: Mapped[int]=mapped_column(primary_key=True); miner_id: Mapped[int]=mapped_column(ForeignKey('miners.id', ondelete='CASCADE'), index=True)
    timestamp: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now, index=True)
    hashrate: Mapped[float|None]=mapped_column(Float); temperature: Mapped[float|None]=mapped_column(Float); rssi: Mapped[float|None]=mapped_column(Float); clock: Mapped[float|None]=mapped_column(Float); voltage: Mapped[float|None]=mapped_column(Float)
    shares_accepted: Mapped[int]=mapped_column(Integer, default=0); shares_rejected: Mapped[int]=mapped_column(Integer, default=0); latency_ms: Mapped[float|None]=mapped_column(Float); power_watts: Mapped[float|None]=mapped_column(Float)
    raw: Mapped[dict]=mapped_column(JSON, default=dict); miner: Mapped[Miner]=relationship(back_populates='telemetry')

class Event(Base):
    __tablename__='events'
    id: Mapped[int]=mapped_column(primary_key=True); miner_id: Mapped[int|None]=mapped_column(ForeignKey('miners.id', ondelete='SET NULL'))
    type: Mapped[EventType]=mapped_column(Enum(EventType), index=True); message: Mapped[str]=mapped_column(Text); payload: Mapped[dict]=mapped_column(JSON, default=dict); created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now, index=True)

class Firmware(Base):
    __tablename__='firmware'
    id: Mapped[int]=mapped_column(primary_key=True); filename: Mapped[str]=mapped_column(String(255)); version: Mapped[str]=mapped_column(String(80)); model: Mapped[str|None]=mapped_column(String(120)); path: Mapped[str]=mapped_column(String(500)); checksum: Mapped[str]=mapped_column(String(128)); created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)

class Setting(Base):
    __tablename__='settings'
    key: Mapped[str]=mapped_column(String(120), primary_key=True); value: Mapped[dict]=mapped_column(JSON, default=dict)
