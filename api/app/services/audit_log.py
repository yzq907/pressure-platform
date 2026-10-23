"""AuditLog 业务服务。"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import Codes
from app.core.context import UserContext
from app.core.exceptions import MysteriousException
from app.core.response import PageVO
from app.crud import audit_log as crud
from app.models.audit_log import AuditLog
from app.schemas.audit_log import (
    AuditLogBatchCleanVO,
    AuditLogCleanFailureVO,
    AuditLogParam,
    AuditLogQuery,
    AuditLogVO,
)

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


async def clean_logs(
    db: AsyncSession,
    ids: list[int],
    current: UserContext,
    ip: str,
) -> AuditLogBatchCleanVO:
    """在同一事务中删除指定日志并记录一次汇总审计。"""
    ordered_ids = list(dict.fromkeys(ids))
    try:
        existing = set(await crud.lock_existing_ids(db, ordered_ids))
        deleted_ids = [item_id for item_id in ordered_ids if item_id in existing]
        failures = [
            AuditLogCleanFailureVO(id=item_id, message="审计日志不存在或已被删除")
            for item_id in ordered_ids
            if item_id not in existing
        ]
        await crud.delete_ids(db, deleted_ids)
        if deleted_ids:
            db.add(
                AuditLog(
                    user_id=current.id,
                    username=current.username,
                    action="DELETE",
                    resource_type="audit",
                    resource_id=0,
                    resource_name="审计日志",
                    detail=f"批量删除审计日志 {len(deleted_ids)} 条，ID: {','.join(map(str, deleted_ids))}",
                    ip=ip or "",
                    create_time=datetime.now(SHANGHAI),
                )
            )
        await db.commit()
        return AuditLogBatchCleanVO(
            deleted_ids=deleted_ids,
            failures=failures,
            success_count=len(deleted_ids),
            failed_count=len(failures),
        )
    except Exception as exc:
        await db.rollback()
        log.exception("批量删除审计日志失败: %s", type(exc).__name__)
        raise MysteriousException(Codes.FAIL, "批量删除审计日志失败，请稍后重试") from exc
