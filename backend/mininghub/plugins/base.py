from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

@dataclass
class MinerSnapshot:
    name: str
    ip: str
    plugin: str
    status: str = 'online'
    hostname: str | None = None
    model: str | None = None
    manufacturer: str | None = None
    firmware: str | None = None
    mac: str | None = None
    pool: str | None = None
    wallet: str | None = None
    worker: str | None = None
    temperature: float | None = None
    hashrate: float | None = None
    clock: float | None = None
    voltage: float | None = None
    fan_speed: float | None = None
    chip_temperature: float | None = None
    board_temperature: float | None = None
    heap: float | None = None
    cpu: float | None = None
    ram: float | None = None
    rssi: float | None = None
    ssid: str | None = None
    wifi_channel: int | None = None
    ota_version: str | None = None
    uptime_seconds: int = 0
    reboots: int = 0
    errors: str | None = None
    latency_ms: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

class MinerPlugin(ABC):
    name: str
    @abstractmethod
    async def probe(self, ip: str) -> MinerSnapshot | None: ...
    async def control(self, ip: str, command: str, parameters: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(f'{self.name} does not implement control command {command}')
