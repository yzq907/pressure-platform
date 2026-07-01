"""/uploadFile/* 路由。用于管理 HTTP 文件上传接口公共文件绑定。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import UserContext
from app.core.permissions import PERMISSION_TESTCASE
from app.core.response import Response, success
from app.db.session import get_db
from app.deps.auth import get_current_user_dep
from app.deps.permission import require_permission
from app.schemas.upload_file import UploadFileBindingParam, UploadFileBindingVO
from app.services import upload_file as service

router = APIRouter(
    prefix="/uploadFile",
    tags=["uploadFile"],
    dependencies=[Depends(get_current_user_dep), Depends(require_permission(PERMISSION_TESTCASE))],
)


@router.post(
    "/binding/add",
    summary="绑定公共上传接口文件到用例",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def add_public_upload_file_binding(
    param: UploadFileBindingParam,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.bind_public_upload_file(db, param, current)
    return success(ok)


@router.get(
    "/binding/getByTestCaseId",
    summary="查询用例绑定的公共上传接口文件",
    response_model=Response[list[UploadFileBindingVO]],
    response_model_by_alias=True,
)
async def get_public_upload_file_bindings(
    testCaseId: int,
    db: AsyncSession = Depends(get_db),
) -> Response[list[UploadFileBindingVO]]:
    items = await service.get_bindings_by_test_case_id(db, testCaseId)
    return success(items)


@router.get(
    "/binding/delete/{id}",
    summary="解绑公共上传接口文件",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def delete_public_upload_file_binding(id: int, db: AsyncSession = Depends(get_db)) -> Response[bool]:
    ok = await service.delete_binding(db, id)
    return success(ok)
