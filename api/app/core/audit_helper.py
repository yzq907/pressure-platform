"""审计日志辅助工具，简化各模块的审计记录。"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import UserContext
from app.schemas.audit_log import AuditLogParam
from app.services import audit_log as service

log = logging.getLogger(__name__)


async def record(
    db: AsyncSession,
    user: UserContext | None,
    action: str,
    resource_type: str,
    resource_id: int = 0,
    resource_name: str = "",
    detail: str = "",
    ip: str = "",
) -> None:
    """记录一条审计日志。失败仅日志，不影响主流程。"""
    try:
        param = AuditLogParam(
            user_id=user.id if user else 0,
            username=user.username if user else "system",
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_name=resource_name,
            detail=detail,
            ip=ip,
        )
        await service.add_log(db, param)
    except Exception:
        log.exception("审计日志记录失败")
