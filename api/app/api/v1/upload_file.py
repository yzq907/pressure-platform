"""/uploadFile/* 路由。用于管理 HTTP 文件上传接口依赖的资源文件。"""

from __future__ import annotations

import os
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import Codes
from app.core.context import UserContext
from app.core.exceptions import MysteriousException
from app.core.permissions import PERMISSION_TESTCASE
from app.core.response import PageVO, Response, success
from app.db.session import get_db
from app.deps.auth import get_current_user_dep
from app.deps.permission import require_permission
from app.schemas.upload_file import UploadFileQuery, UploadFileVO
from app.services import upload_file as service

router = APIRouter(
    prefix="/uploadFile",
    tags=["uploadFile"],
    dependencies=[Depends(get_current_user_dep), Depends(require_permission(PERMISSION_TESTCASE))],
)


@router.post(
    "/upload/{testcase_id}",
    summary="上传接口文件资源",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def upload_file(
    testcase_id: int,
    uploadFile: UploadFile = File(...),
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.upload_file(db, testcase_id, uploadFile, current)
    return success(ok)


@router.get(
    "/delete/{id}",
    summary="删除上传接口文件资源",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def delete_upload_file(id: int, db: AsyncSession = Depends(get_db)) -> Response[bool]:
    ok = await service.delete_upload_file(db, id)
    return success(ok)


@router.get(
    "/list",
    summary="分页查询上传接口文件资源",
    response_model=Response[PageVO[UploadFileVO]],
    response_model_by_alias=True,
)
async def list_upload_files(
    query: UploadFileQuery = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Response[PageVO[UploadFileVO]]:
    page = await service.get_upload_file_list(db, query)
    return success(page)


@router.get(
    "/getByTestCaseId",
    summary="查询用例关联的上传接口文件资源",
    response_model=Response[list[UploadFileVO]],
    response_model_by_alias=True,
)
async def get_by_testcase_id(
    testCaseId: int,
    db: AsyncSession = Depends(get_db),
) -> Response[list[UploadFileVO]]:
    items = await service.get_by_test_case_id(db, testCaseId)
    return success(items)


@router.get("/download/{id}", summary="上传接口文件资源下载")
async def download_upload_file(id: int, db: AsyncSession = Depends(get_db)) -> FileResponse:
    item = await service.get_upload_file_vo(db, id)
    filepath = os.path.join(item.file_dir, item.dst_name)
    if not os.path.exists(filepath):
        raise MysteriousException(Codes.FILE_NOT_EXIST)
    return FileResponse(
        filepath,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{quote(item.src_name)}"'},
    )
