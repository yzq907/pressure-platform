"""应用配置。所有可调参数都通过环境变量 MYSTERIOUS_* 传入，便于容器化部署。"""

from __future__ import annotations

from functools import lru_cache
import os
import sys
from pathlib import Path

from pydantic import Field

from pydantic_settings import BaseSettings, SettingsConfigDict


def default_log_dir() -> str:
    program_dir = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parents[2]
    )
    return str(program_dir / "logs")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MYSTERIOUS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    server_port: int = 4321
    server_host: str = "0.0.0.0"
    frontend_dist: str = ""
    data_dir: str = ""
    mysql_url: str = (
        "mysql+asyncmy://root:Test%40123456@localhost:3306/mysterious?charset=utf8mb4"
    )
    redis_url: str = "redis://localhost:6379/0"
    token_expire_hours: int = 12
    bcrypt_rounds: int = 12
    cors_origins: str = "*"
    upload_max_size_mb: int = 100
    log_dir: str = Field(default_factory=default_log_dir)
    log_max_bytes: int = 50 * 1024 * 1024
    log_backup_days: int = 7

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(_env_file=os.environ.get("MYSTERIOUS_CONFIG_FILE", ".env"))
