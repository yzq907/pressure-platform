"""应用配置。所有可调参数都通过环境变量 MYSTERIOUS_* 传入，便于容器化部署。"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MYSTERIOUS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    server_port: int = 4321
    mysql_url: str = (
        "mysql+asyncmy://root:Test%40123456@localhost:3306/mysterious?charset=utf8mb4"
    )
    redis_url: str = "redis://localhost:6379/0"
    token_expire_hours: int = 12
    bcrypt_rounds: int = 12
    cors_origins: str = "*"
    upload_max_size_mb: int = 100
    log_dir: str = "./logs"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
