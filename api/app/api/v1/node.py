"""/node/* 路由。所有端点要求登录。Phase 5 补齐 enable / disable。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_helper import record as audit_record
from app.core.context import UserContext
from app.core.response import PageVO, Response, success
from app.db.session import get_db
from app.deps.auth import get_current_user_dep
from app.schemas.node import NodeParam, NodeQuery, NodeVO
from app.services import node as service

router = APIRouter(
    prefix="/node",
    tags=["node"],
    dependencies=[Depends(get_current_user_dep)],
)


@router.post(
    "/add",
    summary="新增节点",
    response_model=Response[int],
    response_model_by_alias=True,
)
async def add_node(
    param: NodeParam,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[int]:
    id = await service.add_node(db, param, current)
    return success(id)


@router.post(
    "/update/{id}",
    summary="修改节点",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def update_node(
    id: int,
    param: NodeParam,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.update_node(db, id, param, current)
    return success(ok)


@router.get(
    "/getById/{id}",
    summary="节点详情",
    response_model=Response[NodeVO | None],
    response_model_by_alias=True,
)
async def get_by_id(
    id: int,
    db: AsyncSession = Depends(get_db),
) -> Response[NodeVO | None]:
    node = await service.get_by_id(db, id)
    return success(node)


@router.get(
    "/delete/{id}",
    summary="删除节点",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def delete_node(
    id: int,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.delete_node(db, id)
    if ok:
        await audit_record(db, current, "DELETE", "node", id, detail=f"删除节点 #{id}")
    return success(ok)


@router.get(
    "/list",
    summary="分页查询节点",
    response_model=Response[PageVO[NodeVO]],
    response_model_by_alias=True,
)
async def list_nodes(
    query: NodeQuery = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Response[PageVO[NodeVO]]:
    page = await service.get_node_list(db, query)
    return success(page)


@router.get(
    "/regions",
    summary="获取所有启用 slave 的区域列表",
    response_model=Response[list[str]],
    response_model_by_alias=True,
)
async def list_regions(
    db: AsyncSession = Depends(get_db),
) -> Response[list[str]]:
    regions = await service.get_all_regions(db)
    return success(regions)


@router.get(
    "/enableSlaveCount",
    summary="获取已启用的 slave 节点数量，可选按区域过滤",
    response_model=Response[int],
    response_model_by_alias=True,
)
async def enable_slave_count(
    region: str | None = Query(None, description="区域名称，为空则查全部"),
    db: AsyncSession = Depends(get_db),
) -> Response[int]:
    cnt = await service.get_enable_slave_count(db, region=region)
    return success(cnt)


@router.get(
    "/enable/{id}",
    summary="启用 slave 节点",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def enable_node(
    id: int,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.enable_node(db, id, current)
    if ok:
        await audit_record(db, current, "UPDATE", "node", id, detail=f"启用节点 #{id}")
    return success(ok)


@router.get(
    "/disable/{id}",
    summary="禁用 slave 节点",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def disable_node(
    id: int,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.disable_node(db, id, current)
    if ok:
        await audit_record(db, current, "UPDATE", "node", id, detail=f"禁用节点 #{id}")
    return success(ok)
