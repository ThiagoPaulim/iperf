from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil, zipfile
from fastapi import Depends, FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from .config import get_settings
from .database import engine, get_session
from .discovery import discover
from .models import Base, Event, EventType, Firmware, Miner, Role, Telemetry, User
from .plugins.registry import get_plugin, list_plugins
from .schemas import *
from .security import create_token, hash_password, require_role, verify_password
from .services import dashboard, upsert_snapshot

app=FastAPI(title='MiningHub API', version='0.1.0', openapi_url='/api/openapi.json', docs_url='/api/docs')
settings=get_settings()
app.add_middleware(CORSMiddleware, allow_origins=['*'] if settings.cors_origins=='*' else settings.cors_origins.split(','), allow_credentials=True, allow_methods=['*'], allow_headers=['*'])
Instrumentator().instrument(app).expose(app, endpoint='/metrics')
clients:set[WebSocket]=set()

@app.on_event('startup')
async def startup():
    Path('data').mkdir(exist_ok=True)
    async with engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)

@app.get('/api/health')
async def health(): return {'status':'ok','time':datetime.now(timezone.utc).isoformat()}

@app.post('/api/auth/bootstrap', response_model=UserRead)
async def bootstrap(data:UserCreate, session:AsyncSession=Depends(get_session)):
    exists=(await session.execute(select(User))).first()
    if exists: raise HTTPException(409,'Bootstrap already completed')
    user=User(username=data.username,password_hash=hash_password(data.password),role=Role.admin); session.add(user); await session.commit(); await session.refresh(user); return user
@app.post('/api/auth/login', response_model=Token)
async def login(data:LoginRequest, session:AsyncSession=Depends(get_session)):
    user=(await session.execute(select(User).where(User.username==data.username))).scalar_one_or_none()
    if not user or not verify_password(data.password,user.password_hash): raise HTTPException(401,'Invalid credentials')
    session.add(Event(type=EventType.login,message=f'Login for {user.username}',payload={'user':user.username})); await session.commit()
    return Token(access_token=create_token(user.username,user.role), role=user.role)

@app.get('/api/miners', response_model=list[MinerRead])
async def miners(session:AsyncSession=Depends(get_session)): return (await session.execute(select(Miner).order_by(Miner.name))).scalars().all()
@app.post('/api/miners', response_model=MinerRead)
async def create_miner(data:MinerCreate, session:AsyncSession=Depends(get_session), _=Depends(require_role(Role.admin,Role.operator))):
    miner=Miner(**data.model_dump()); session.add(miner); await session.commit(); await session.refresh(miner); return miner
@app.get('/api/miners/{miner_id}', response_model=MinerRead)
async def get_miner(miner_id:int, session:AsyncSession=Depends(get_session)):
    miner=await session.get(Miner, miner_id)
    if not miner: raise HTTPException(404,'Miner not found')
    return miner
@app.post('/api/miners/{miner_id}/control')
async def control(miner_id:int, data:ControlRequest, session:AsyncSession=Depends(get_session), _=Depends(require_role(Role.admin,Role.operator))):
    miner=await session.get(Miner, miner_id)
    if not miner: raise HTTPException(404,'Miner not found')
    result=await get_plugin(miner.plugin).control(miner.ip, data.command, data.parameters)
    session.add(Event(miner_id=miner.id,type=EventType.ota if data.command=='ota' else EventType.alert,message=f'Control command {data.command}',payload=result)); await session.commit(); return result

@app.get('/api/telemetry/{miner_id}', response_model=list[TelemetryRead])
async def telemetry(miner_id:int, hours:int=24, session:AsyncSession=Depends(get_session)):
    since=datetime.now(timezone.utc)-timedelta(hours=hours)
    return (await session.execute(select(Telemetry).where(Telemetry.miner_id==miner_id,Telemetry.timestamp>=since).order_by(Telemetry.timestamp))).scalars().all()
@app.get('/api/history', response_model=list[EventRead])
@app.get('/api/events', response_model=list[EventRead])
async def events(session:AsyncSession=Depends(get_session)): return (await session.execute(select(Event).order_by(Event.created_at.desc()).limit(500))).scalars().all()
@app.get('/api/dashboard', response_model=DashboardStats)
@app.get('/api/stats', response_model=DashboardStats)
async def stats(session:AsyncSession=Depends(get_session)): return await dashboard(session)
@app.get('/api/plugins')
async def plugins(): return {'plugins':list_plugins() + ['axeos','mqtt','snmp','custom-json','bmminer']}
@app.post('/api/discovery')
async def discovery(data:DiscoveryRequest, session:AsyncSession=Depends(get_session), _=Depends(require_role(Role.admin,Role.operator))):
    snaps=await discover(data.cidr or settings.discovery_cidr)
    miners=[]
    for snap in snaps: miners.append(await upsert_snapshot(session, snap, data.approve))
    session.add(Event(type=EventType.discovery,message=f'Discovered {len(miners)} miners',payload={'count':len(miners)})); await session.commit()
    return {'count':len(miners),'miners':[m.id for m in miners]}
@app.post('/api/firmware')
async def firmware(file:UploadFile, version:str, model:str|None=None, session:AsyncSession=Depends(get_session), _=Depends(require_role(Role.admin,Role.operator))):
    Path(settings.firmware_dir).mkdir(parents=True,exist_ok=True); dest=Path(settings.firmware_dir)/file.filename
    with dest.open('wb') as fh: shutil.copyfileobj(file.file, fh)
    fw=Firmware(filename=file.filename,version=version,model=model,path=str(dest),checksum='pending'); session.add(fw); await session.commit(); return {'id':fw.id,'filename':fw.filename}
@app.post('/api/backups')
async def backup(_=Depends(require_role(Role.admin))):
    Path(settings.backup_dir).mkdir(parents=True,exist_ok=True); name=f'mininghub-{datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")}.zip'; target=Path(settings.backup_dir)/name
    with zipfile.ZipFile(target,'w') as z:
        for p in Path('data').rglob('*'):
            if p.is_file(): z.write(p, p.relative_to('data'))
    return {'backup':str(target)}
@app.post('/api/restore')
async def restore(file:UploadFile, _=Depends(require_role(Role.admin))):
    with zipfile.ZipFile(file.file) as z: z.extractall('data')
    return {'status':'restored'}
@app.websocket('/api/websocket')
async def websocket(ws:WebSocket):
    await ws.accept(); clients.add(ws)
    try:
        while True: await ws.send_json({'type':'heartbeat','time':datetime.now(timezone.utc).isoformat()}); await ws.receive_text()
    except WebSocketDisconnect: clients.discard(ws)

for path in ['/api/users','/api/settings','/api/alerts','/api/ota']:
    app.add_api_route(path, lambda: {'status':'available'}, methods=['GET'])
