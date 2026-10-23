"""AuditLog 相关 Pydantic schemas。"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

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


class AuditLogBatchCleanParam(CamelModel):
    ids: list[Annotated[int, Field(strict=True, gt=0)]] = Field(min_length=1, max_length=100)


class AuditLogCleanFailureVO(CamelModel):
    id: int
    message: str


class AuditLogBatchCleanVO(CamelModel):
    deleted_ids: list[int] = Field(default_factory=list)
    failures: list[AuditLogCleanFailureVO] = Field(default_factory=list)
    success_count: int = 0
    failed_count: int = 0
