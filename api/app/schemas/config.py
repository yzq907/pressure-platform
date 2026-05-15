"""Config 相关 Pydantic schemas，对齐 Java ConfigParam / ConfigVO / ConfigQuery。"""

from __future__ import annotations

from app.schemas.base import BaseQuery, BaseVO, CamelModel


class ConfigParam(CamelModel):
    config_key: str | None = None
    config_value: str | None = None
    description: str | None = None


class ConfigVO(BaseVO):
    config_key: str = ""
    config_value: str = ""
    description: str = ""


class ConfigQuery(BaseQuery):
    config_key: str | None = None
