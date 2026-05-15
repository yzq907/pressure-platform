"""/jar/* 路由。所有端点要求登录。"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import Codes
from app.core.context import UserContext
from app.core.exceptions import MysteriousException
from app.core.response import PageVO, Response, success
from app.db.session import get_db
from app.deps.auth import get_current_user_dep
from app.schemas.jar import JarQuery, JarVO
from app.services import jar as service

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/jar",
    tags=["jar"],
    dependencies=[Depends(get_current_user_dep)],
)


@router.post(
    "/upload/{testcase_id}",
    summary="上传 JAR 依赖",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def upload_jar(
    testcase_id: int,
    jarFile: UploadFile = File(...),
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.upload_jar(db, testcase_id, jarFile, current)
    return success(ok)


@router.get(
    "/delete/{id}",
    summary="删除 JAR 文件",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def delete_jar(
    id: int,
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.delete_jar(db, id)
    return success(ok)


@router.get(
    "/list",
    summary="分页查询 JAR",
    response_model=Response[PageVO[JarVO]],
    response_model_by_alias=True,
)
async def list_jars(
    query: JarQuery = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Response[PageVO[JarVO]]:
    page = await service.get_jar_list(db, query)
    return success(page)


@router.get(
    "/getByTestCaseId",
    summary="查询用例关联的 JAR",
    response_model=Response[list[JarVO]],
    response_model_by_alias=True,
)
async def get_by_testcase_id(
    testCaseId: int,
    db: AsyncSession = Depends(get_db),
) -> Response[list[JarVO]]:
    items = await service.get_by_test_case_id(db, testCaseId)
    return success(items)


@router.get("/download/{id}", summary="JAR 文件下载")
async def download_jar(id: int, db: AsyncSession = Depends(get_db)) -> FileResponse:
    jar = await service.get_jar_vo(db, id)
    filepath = os.path.join(jar.jar_dir, jar.src_name)
    if not os.path.exists(filepath):
        raise MysteriousException(Codes.FILE_NOT_EXIST)
    return FileResponse(
        filepath,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{quote(jar.src_name)}"'},
    )
