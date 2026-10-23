"""配置：一律从环境变量 / .env 读取，不在代码里写地址和密钥。"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql://caseflow:caseflow_dev_pw@127.0.0.1:5432/caseflow"
    redis_url: str = "redis://127.0.0.1:6379/0"
    opa_url: str = "http://127.0.0.1:8181"
    opa_timeout_s: float = 2.0
    env: str = "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()
