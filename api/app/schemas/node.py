"""Node 相关 Pydantic schemas，对齐 Java NodeParam / NodeVO / NodeQuery。"""

from __future__ import annotations

from app.schemas.base import BaseQuery, BaseVO, CamelModel


class NodeParam(CamelModel):
    name: str | None = None
    description: str | None = None
    type: int | None = None
    host: str | None = None
    username: str | None = None
    password: str | None = None
    port: int | None = None


class NodeVO(BaseVO):
    name: str = ""
    description: str = ""
    type: int = 0
    host: str = ""
    username: str = ""
    password: str = ""
    port: int = 0
    status: int = 0


class NodeQuery(BaseQuery):
    name: str | None = None
    host: str | None = None
