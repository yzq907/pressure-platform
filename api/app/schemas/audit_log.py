"""AuditLog 相关 Pydantic schemas。"""

from __future__ import annotations

from app.schemas.base import BaseQuery, BaseVO, CamelModel


class AuditLogParam(CamelModel):
    """供 service 内部调用"""

    user_id: int = 0
    username: str = ""
    action: str = ""
    resource_type: str = ""
    resource_id: int = 0
    resource_name: str = ""
    detail: str = ""
    ip: str = ""


class AuditLogVO(BaseVO):
    user_id: int = 0
    username: str = ""
    action: str = ""
    resource_type: str = ""
    resource_id: int = 0
    resource_name: str = ""
    detail: str = ""
    ip: str = ""


class AuditLogQuery(BaseQuery):
    username: str | None = None
    action: str | None = None
    resource_type: str | None = None
    resource_id: int | None = None
