from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_prefix='MININGHUB_')
    app_name: str = 'MiningHub'
    environment: str = 'production'
    database_url: str = 'sqlite+aiosqlite:///./data/mininghub.db'
    redis_url: str = 'redis://redis:6379/0'
    jwt_secret: str = 'change-this-secret-before-production'
    jwt_algorithm: str = 'HS256'
    access_token_minutes: int = 60
    discovery_cidr: str = '192.168.1.0/24'
    cors_origins: str = '*'
    backup_dir: str = './data/backups'
    firmware_dir: str = './data/firmware'

@lru_cache
def get_settings() -> Settings:
    return Settings()
