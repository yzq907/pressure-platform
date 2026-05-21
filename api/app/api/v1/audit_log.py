"""/audit/* 审计日志路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import UserContext
from app.core.response import PageVO, Response, success
from app.db.session import get_db
from app.deps.auth import get_current_user_dep
from app.schemas.audit_log import AuditLogQuery, AuditLogVO
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
    query: AuditLogQuery = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Response[PageVO[AuditLogVO]]:
    page = await service.get_log_list(db, query)
    return success(page)
