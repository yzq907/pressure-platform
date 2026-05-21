"""/user/* 路由，对齐 Java UserController。

注意：和 Java 端一样，`/user/**` 整体在认证白名单内，所有这 6 个接口都不要求登录。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_helper import record as audit_record
from app.core.context import UserContext
from app.core.response import PageVO, Response, success
from app.db.session import get_db
from app.deps.auth import get_current_user_dep
from app.schemas.user import UpdatePasswordParam, UserParam, UserQuery, UserVO
from app.services import user as user_service

router = APIRouter(prefix="/user", tags=["user"])


@router.post(
    "/add",
    summary="新增用户",
    response_model=Response[int],
    response_model_by_alias=True,
)
async def add_user(
    param: UserParam,
    db: AsyncSession = Depends(get_db),
) -> Response[int]:
    user_id = await user_service.add_user(db, param)
    return success(user_id)


@router.get(
    "/delete/{id}",
    summary="删除用户",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def delete_user(
    id: int,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await user_service.delete_user(db, id, current)
    if ok:
        await audit_record(db, current, "DELETE", "user", id, detail=f"删除用户 #{id}")
    return success(ok)


@router.post(
    "/update/{id}",
    summary="修改用户",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def update_user(
    id: int,
    param: UserParam,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await user_service.update_user(db, id, param, current)
    return success(ok)


@router.get(
    "/getById/{id}",
    summary="查询用户详情",
    response_model=Response[UserVO | None],
    response_model_by_alias=True,
)
async def get_by_id(
    id: int,
    db: AsyncSession = Depends(get_db),
) -> Response[UserVO | None]:
    user = await user_service.get_by_id(db, id)
    return success(user)


@router.post(
    "/login",
    summary="用户登录",
    response_model=Response[str],
    response_model_by_alias=True,
)
async def login(
    param: UserParam,
    db: AsyncSession = Depends(get_db),
) -> Response[str]:
    token = await user_service.login(db, param)
    return success(token)


@router.get(
    "/list",
    summary="分页查询用户",
    response_model=Response[PageVO[UserVO]],
    response_model_by_alias=True,
)
async def list_users(
    query: UserQuery = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Response[PageVO[UserVO]]:
    page = await user_service.get_user_list(db, query)
    return success(page)


@router.post(
    "/updatePassword",
    summary="修改当前用户密码",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def update_password(
    param: UpdatePasswordParam,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await user_service.update_password(db, param, current)
    if ok:
        await audit_record(db, current, "UPDATE", "user", current.id, detail="修改密码")
    return success(ok)
