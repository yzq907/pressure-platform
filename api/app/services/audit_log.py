"""AuditLog 业务服务。"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import PageVO
from app.crud import audit_log as crud
from app.models.audit_log import AuditLog
from app.schemas.audit_log import AuditLogParam, AuditLogQuery, AuditLogVO

SHANGHAI = ZoneInfo("Asia/Shanghai")
log = logging.getLogger(__name__)


def _to_vo(obj: AuditLog) -> AuditLogVO:
    return AuditLogVO.model_validate(obj)


async def add_log(
    db: AsyncSession,
    param: AuditLogParam,
) -> int:
    """记录一条审计日志。"""
    obj = AuditLog(
        user_id=param.user_id or 0,
        username=param.username or "",
        action=param.action or "",
        resource_type=param.resource_type or "",
        resource_id=param.resource_id or 0,
        resource_name=param.resource_name or "",
        detail=param.detail or "",
        ip=param.ip or "",
        create_time=datetime.now(SHANGHAI),
    )
    await crud.add(db, obj)
    return obj.id


async def get_log_list(
    db: AsyncSession,
    query: AuditLogQuery,
) -> PageVO[AuditLogVO]:
    page_vo: PageVO[AuditLogVO] = PageVO(page=query.page, size=query.size, total=0, list=[])
    total = await crud.count(
        db,
        username=query.username,
        action=query.action,
        resource_type=query.resource_type,
        resource_id=query.resource_id,
    )
    if total <= 0:
        return page_vo
    page_vo.total = total
    offset = PageVO.offset(query.page, query.size)
    items = await crud.list_logs(
        db,
        username=query.username,
        action=query.action,
        resource_type=query.resource_type,
        resource_id=query.resource_id,
        offset=offset,
        limit=query.size,
    )
    page_vo.list = [_to_vo(o) for o in items]
    return page_vo
