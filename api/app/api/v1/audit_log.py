"""/audit/* 审计日志路由。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import UserContext
from app.core.permissions import PERMISSION_AUDIT
from app.core.response import PageVO, Response, success
from app.db.session import get_db
from app.deps.auth import get_current_user_dep
from app.deps.permission import require_permission
from app.schemas.audit_log import AuditLogBatchCleanParam, AuditLogBatchCleanVO, AuditLogQuery, AuditLogVO
from app.services import audit_log as service

router = APIRouter(
    prefix="/audit",
    tags=["audit"],
    dependencies=[Depends(get_current_user_dep)],
)


@router.get(
    "/list",
    summary="审计日志列表",
    response_model=Response[PageVO[AuditLogVO]],
    response_model_by_alias=True,
)
async def get_audit_log_list(
    query: Annotated[AuditLogQuery, Depends()],
    current: Annotated[UserContext, Depends(require_permission(PERMISSION_AUDIT))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response[PageVO[AuditLogVO]]:
    page = await service.get_log_list(db, query)
    return success(page)


@router.post(
    "/cleanBatch",
    summary="批量删除审计日志",
    response_model=Response[AuditLogBatchCleanVO],
    response_model_by_alias=True,
)
async def clean_audit_logs(
    param: AuditLogBatchCleanParam,
    request: Request,
    current: Annotated[UserContext, Depends(require_permission(PERMISSION_AUDIT))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response[AuditLogBatchCleanVO]:
    result = await service.clean_logs(db, param.ids, current, request.client.host if request.client else "")
    return success(result)
