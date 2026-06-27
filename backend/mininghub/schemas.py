from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field
from .models import MinerStatus, Role, EventType

class Token(BaseModel): access_token: str; token_type: str='bearer'; role: Role
class LoginRequest(BaseModel): username: str; password: str
class UserCreate(BaseModel): username: str; password: str=Field(min_length=8); role: Role=Role.readonly
class UserRead(BaseModel): model_config=ConfigDict(from_attributes=True); id:int; username:str; role:Role; is_active:bool; two_factor_enabled:bool
class MinerBase(BaseModel):
    name:str; ip:str; plugin:str='http'; hostname:str|None=None; model:str|None=None; manufacturer:str|None=None; firmware:str|None=None; mac:str|None=None; pool:str|None=None; wallet:str|None=None; worker:str|None=None; approved:bool=False; tags:list[str]=[]
class MinerCreate(MinerBase): pass
class MinerRead(MinerBase):
    model_config=ConfigDict(from_attributes=True)
    id:int; uuid:str; status:MinerStatus; temperature:float|None=None; hashrate:float|None=None; rssi:float|None=None; last_seen:datetime|None=None; uptime_seconds:int; reboots:int; latency_ms:float|None=None; created_at:datetime; updated_at:datetime
class TelemetryRead(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    timestamp:datetime; hashrate:float|None=None; temperature:float|None=None; rssi:float|None=None; clock:float|None=None; voltage:float|None=None; shares_accepted:int; shares_rejected:int; latency_ms:float|None=None; power_watts:float|None=None
class EventRead(BaseModel): model_config=ConfigDict(from_attributes=True); id:int; miner_id:int|None; type:EventType; message:str; payload:dict; created_at:datetime
class DashboardStats(BaseModel): total:int; online:int; offline:int; hashrate_total:float; hashrate_average:float; temperature_average:float|None; shares_accepted:int; shares_rejected:int; hottest_miner:MinerRead|None; best_miner:MinerRead|None; worst_miner:MinerRead|None
class ControlRequest(BaseModel): command:str; parameters:dict={}
class DiscoveryRequest(BaseModel): cidr:str|None=None; approve:bool=False
