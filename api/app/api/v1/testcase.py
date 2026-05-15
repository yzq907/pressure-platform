"""/testcase/* 路由。Phase 5 补齐 debug/run/stop/syncNode/getFull/getJMeterResult。
所有端点要求登录。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import UserContext
from app.core.response import PageVO, Response, success
from app.db.session import get_db
from app.deps.auth import get_current_user_dep
from app.schemas.testcase import (
    BatchDeleteParam,
    JMeterResultVO,
    RunParam,
    TestCaseFullVO,
    TestCaseParam,
    TestCaseQuery,
    TestCaseVO,
)
from app.services import testcase as service

router = APIRouter(
    prefix="/testcase",
    tags=["testcase"],
    dependencies=[Depends(get_current_user_dep)],
)


@router.post(
    "/add",
    summary="新增用例",
    response_model=Response[int],
    response_model_by_alias=True,
)
async def add_testcase(
    param: TestCaseParam,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[int]:
    id = await service.add_testcase(db, param, current)
    return success(id)


@router.post(
    "/update/{id}",
    summary="修改用例",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def update_testcase(
    id: int,
    param: TestCaseParam,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.update_testcase(db, id, param, current)
    return success(ok)


@router.get(
    "/delete/{id}",
    summary="删除用例",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def delete_testcase(
    id: int,
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.delete_testcase(db, id)
    return success(ok)


@router.post(
    "/batchDelete",
    summary="批量删除用例",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def batch_delete_testcase(
    param: BatchDeleteParam,
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.batch_delete_testcase(db, param.ids)
    return success(ok)


@router.get(
    "/list",
    summary="分页查询用例",
    response_model=Response[PageVO[TestCaseVO]],
    response_model_by_alias=True,
)
async def list_testcases(
    query: TestCaseQuery = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Response[PageVO[TestCaseVO]]:
    page = await service.get_testcase_list(db, query)
    return success(page)


@router.get(
    "/debug/{id}",
    summary="调试用例",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def debug_testcase(
    id: int,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.debug_testcase(db, id, current)
    return success(ok)


@router.post(
    "/run/{id}",
    summary="执行用例",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def run_testcase(
    id: int,
    param: RunParam,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.run_testcase(db, id, param, current)
    return success(ok)


@router.get(
    "/stop/{id}",
    summary="结束执行",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def stop_testcase(
    id: int,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.stop_testcase(db, id, current)
    return success(ok)


@router.get(
    "/getFull/{id}",
    summary="查询所有依赖信息",
    response_model=Response[TestCaseFullVO | None],
    response_model_by_alias=True,
)
async def get_full(
    id: int,
    db: AsyncSession = Depends(get_db),
) -> Response[TestCaseFullVO | None]:
    obj = await service.get_full_vo(db, id)
    return success(obj)


@router.get(
    "/syncNode/{node_id}",
    summary="同步新增压力机测试数据",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def sync_node(
    node_id: int,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.sync_node(db, node_id, current)
    return success(ok)


@router.get(
    "/getJMeterResult/{id}",
    summary="查看日志实时数据",
    response_model=Response[list[JMeterResultVO]],
    response_model_by_alias=True,
)
async def get_jmeter_result(
    id: int,
    db: AsyncSession = Depends(get_db),
) -> Response[list[JMeterResultVO]]:
    items = await service.get_jmeter_result(db, id)
    return success(items)
