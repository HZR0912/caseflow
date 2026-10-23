"""配置：一律从环境变量 / .env 读取，不在代码里写地址和密钥。"""

from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthCredential(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    user_id: str = Field(min_length=1)
    role: str = Field(min_length=1)
    tenant: str = Field(min_length=1)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql://caseflow:caseflow_dev_pw@127.0.0.1:5432/caseflow"
    redis_url: str = "redis://127.0.0.1:6379/0"
    opa_url: str = "http://127.0.0.1:8181"
    opa_timeout_s: float = 2.0
    env: str = "dev"
    auth_credentials: list[AuthCredential] = Field(default_factory=list, repr=False)

    @model_validator(mode="after")
    def unique_credentials(self) -> Settings:
        hashes = [credential.token_sha256 for credential in self.auth_credentials]
        if len(hashes) != len(set(hashes)):
            raise ValueError("AUTH_CREDENTIALS contains duplicate token digests")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
