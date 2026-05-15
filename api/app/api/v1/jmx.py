"""/jmx/* 路由（仅本地上传路径）。所有端点要求登录。"""

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
from app.schemas.jmx import JmxQuery, JmxVO
from app.services import jmx as service

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/jmx",
    tags=["jmx"],
    dependencies=[Depends(get_current_user_dep)],
)


@router.post(
    "/upload/{testcase_id}",
    summary="上传 JMX 脚本",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def upload_jmx(
    testcase_id: int,
    jmxFile: UploadFile = File(...),
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.upload_jmx(db, testcase_id, jmxFile, current)
    return success(ok)


@router.get(
    "/delete/{id}",
    summary="删除 JMX 脚本",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def delete_jmx(
    id: int,
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.delete_jmx(db, id)
    return success(ok)


@router.get(
    "/list",
    summary="分页查询 JMX",
    response_model=Response[PageVO[JmxVO]],
    response_model_by_alias=True,
)
async def list_jmxs(
    query: JmxQuery = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Response[PageVO[JmxVO]]:
    page = await service.get_jmx_list(db, query)
    return success(page)


def _file_response_for_jmx(jmx_dir: str, src_name: str) -> FileResponse:
    filepath = os.path.join(jmx_dir, src_name)
    if not os.path.exists(filepath):
        raise MysteriousException(Codes.FILE_NOT_EXIST)
    return FileResponse(
        filepath,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{quote(src_name)}"'},
    )


@router.get("/view/{id}", summary="JMX 脚本预览")
async def view_jmx(id: int, db: AsyncSession = Depends(get_db)) -> FileResponse:
    jmx = await service.get_jmx_vo(db, id)
    return _file_response_for_jmx(jmx.jmx_dir, jmx.src_name)


@router.get("/download/{id}", summary="JMX 脚本下载")
async def download_jmx(id: int, db: AsyncSession = Depends(get_db)) -> FileResponse:
    jmx = await service.get_jmx_vo(db, id)
    return _file_response_for_jmx(jmx.jmx_dir, jmx.src_name)


# ---------------------------------------------------------------------------
# Phase 4 — 在线编辑路由
# ---------------------------------------------------------------------------


@router.post(
    "/addOnline",
    summary="在线新增 JMX",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def add_online_jmx(
    jmx_vo: JmxVO,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.add_online_jmx(db, jmx_vo, current)
    return success(ok)


@router.get(
    "/getOnline/{id}",
    summary="获取在线 JMX 详情（含嵌套子表）",
    response_model=Response[JmxVO],
    response_model_by_alias=True,
)
async def get_online_jmx(
    id: int,
    db: AsyncSession = Depends(get_db),
) -> Response[JmxVO]:
    vo = await service.get_online_jmx(db, id)
    return success(vo)


@router.post(
    "/updateOnline/{id}",
    summary="更新在线 JMX",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def update_online_jmx(
    id: int,
    jmx_vo: JmxVO,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.update_online_jmx(db, id, jmx_vo, current)
    return success(ok)


@router.get(
    "/forceDelete/{id}",
    summary="强制删除 JMX（跳过 JAR/CSV 检查）",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def force_delete_jmx(
    id: int,
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.force_delete_jmx(db, id)
    return success(ok)
