"""配置：一律从环境变量 / .env 读取，不在代码里写地址和密钥。"""

from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthCredential(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    user_id: str = Field(min_length=1)
    role: str = Field(min_length=1)
    tenant: str = Field(min_length=1)


class PlanningProvider(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    base_url: str
    model: str
    api_key: SecretStr
    timeout_s: float = Field(default=15, gt=0, le=60)
    input_usd_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    output_usd_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql://caseflow:caseflow_dev_pw@127.0.0.1:5432/caseflow"
    redis_url: str = "redis://127.0.0.1:6379/0"
    opa_url: str = "http://127.0.0.1:8181"
    opa_timeout_s: float = 2.0
    mockcorp_url: str = "http://127.0.0.1:8001"
    approval_ttl_hours: int = Field(default=24, ge=1, le=168)
    execution_encryption_key: SecretStr = Field(default=SecretStr(""), repr=False)
    planning_providers: list[PlanningProvider] = Field(default_factory=list, repr=False)
    env: str = "dev"
    observation_enabled: bool = False
    otel_exporter_otlp_traces_endpoint: str = ""
    langfuse_base_url: str = "http://127.0.0.1:13000"
    langfuse_public_key: SecretStr = Field(default=SecretStr(""), repr=False)
    langfuse_secret_key: SecretStr = Field(default=SecretStr(""), repr=False)
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
