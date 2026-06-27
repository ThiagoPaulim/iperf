from datetime import datetime, timedelta, timezone
import jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from .config import get_settings
from .database import get_session
from .models import User, Role

pwd_context=CryptContext(schemes=['bcrypt'], deprecated='auto')
oauth2_scheme=OAuth2PasswordBearer(tokenUrl='/api/auth/login')

def hash_password(password:str)->str: return pwd_context.hash(password)
def verify_password(password:str, hashed:str)->bool: return pwd_context.verify(password, hashed)
def create_token(username:str, role:Role)->str:
    s=get_settings(); exp=datetime.now(timezone.utc)+timedelta(minutes=s.access_token_minutes)
    return jwt.encode({'sub':username,'role':role.value,'exp':exp}, s.jwt_secret, algorithm=s.jwt_algorithm)
async def current_user(token:str=Depends(oauth2_scheme), session:AsyncSession=Depends(get_session))->User:
    s=get_settings()
    try: payload=jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm]); username=payload.get('sub')
    except Exception as exc: raise HTTPException(status.HTTP_401_UNAUTHORIZED,'Invalid token') from exc
    user=(await session.execute(select(User).where(User.username==username, User.is_active==True))).scalar_one_or_none()
    if not user: raise HTTPException(status.HTTP_401_UNAUTHORIZED,'Inactive user')
    return user
def require_role(*roles:Role):
    async def dep(user:User=Depends(current_user)):
        if user.role not in roles: raise HTTPException(status.HTTP_403_FORBIDDEN,'Insufficient permissions')
        return user
    return dep
