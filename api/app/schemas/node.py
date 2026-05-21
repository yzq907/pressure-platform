"""Node 相关 Pydantic schemas，对齐 Java NodeParam / NodeVO / NodeQuery。"""

from __future__ import annotations

from datetime import datetime

from pydantic import field_validator

from app.schemas.base import BaseQuery, BaseVO, CamelModel


class NodeParam(CamelModel):
    name: str | None = None
    description: str | None = None
    type: int | None = None
    host: str | None = None
    username: str | None = None
    password: str | None = None
    port: int | None = None
    region: str | None = None


class NodeVO(BaseVO):
    name: str = ""
    description: str = ""
    type: int = 0
    host: str = ""
    username: str = ""
    password: str = ""
    port: int = 0
    status: int = 0
    region: str = ""
    health_status: int = 0
    last_heartbeat: str | None = None
    cpu_usage: float = 0.0
    mem_usage: float = 0.0
    load_avg: float = 0.0

    @field_validator("last_heartbeat", mode="before")
    @classmethod
    def _fmt_dt(cls, v):
        if isinstance(v, datetime):
            return v.strftime("%Y-%m-%d %H:%M:%S")
        return v


class NodeQuery(BaseQuery):
    name: str | None = None
    host: str | None = None


class NodeQuery(BaseQuery):
    name: str | None = None
    host: str | None = None
