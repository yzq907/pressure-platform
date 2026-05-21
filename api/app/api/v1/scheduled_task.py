"""/scheduledTask/* 路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_helper import record as audit_record
from app.core.context import UserContext
from app.core.response import PageVO, Response, success
from app.db.session import get_db
from app.deps.auth import get_current_user_dep
from app.schemas.scheduled_task import (
    ScheduledTaskParam,
    ScheduledTaskQuery,
    ScheduledTaskVO,
)
from app.services import scheduled_task as service

router = APIRouter(
    prefix="/scheduledTask",
    tags=["scheduledTask"],
    dependencies=[Depends(get_current_user_dep)],
)


@router.post(
    "/add",
    summary="创建定时任务",
    response_model=Response[int],
    response_model_by_alias=True,
)
async def add_scheduled_task(
    param: ScheduledTaskParam,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[int]:
    id = await service.add_scheduled_task(db, param, current)
    return success(id)


@router.post(
    "/update/{id}",
    summary="修改定时任务",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def update_scheduled_task(
    id: int,
    param: ScheduledTaskParam,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.update_scheduled_task(db, id, param, current)
    return success(ok)


@router.get(
    "/delete/{id}",
    summary="删除定时任务",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def delete_scheduled_task(
    id: int,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.delete_scheduled_task(db, id)
    if ok:
        await audit_record(db, current, "DELETE", "scheduled_task", id, detail=f"删除定时任务 #{id}")
    return success(ok)


@router.get(
    "/toggle/{id}",
    summary="启用/禁用定时任务",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def toggle_scheduled_task(
    id: int,
    enabled: int,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.toggle_enabled(db, id, enabled, current)
    if ok:
        action_name = "启用" if enabled else "禁用"
        await audit_record(db, current, "UPDATE", "scheduled_task", id, detail=f"{action_name}定时任务 #{id}")
    return success(ok)


@router.get(
    "/trigger/{id}",
    summary="立即触发定时任务",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def trigger_scheduled_task(
    id: int,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.trigger_now(db, id, current)
    if ok:
        await audit_record(db, current, "EXECUTE", "scheduled_task", id, detail=f"立即触发定时任务 #{id}")
    return success(ok)


@router.get(
    "/list",
    summary="分页查询定时任务",
    response_model=Response[PageVO[ScheduledTaskVO]],
    response_model_by_alias=True,
)
async def list_scheduled_tasks(
    query: ScheduledTaskQuery = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Response[PageVO[ScheduledTaskVO]]:
    page = await service.get_scheduled_task_list(db, query)
    return success(page)


@router.get(
    "/listByTestCase/{test_case_id}",
    summary="查询用例的所有定时任务",
    response_model=Response[list[ScheduledTaskVO]],
    response_model_by_alias=True,
)
async def list_by_test_case(
    test_case_id: int,
    db: AsyncSession = Depends(get_db),
) -> Response[list[ScheduledTaskVO]]:
    tasks = await service.get_by_test_case(db, test_case_id)
    return success(tasks)


@router.get(
    "/trigger/{id}",
    summary="立即触发定时任务",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def trigger_scheduled_task(
    id: int,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.trigger_now(db, id, current)
    return success(ok)
