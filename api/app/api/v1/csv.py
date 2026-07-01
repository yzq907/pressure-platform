"""/csv/* 路由。所有端点要求登录。"""

from __future__ import annotations

import os
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, File, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import Codes
from app.core.context import UserContext
from app.core.exceptions import MysteriousException
from app.core.permissions import PERMISSION_CSV
from app.core.response import PageVO, Response, success
from app.db.session import get_db
from app.deps.auth import get_current_user_dep
from app.deps.permission import require_permission
from app.schemas.csv import CsvBindingParam, CsvBindingVO, CsvResourceQuery, CsvResourceVO, CsvStrategyParam
from app.services import csv as service

router = APIRouter(
    prefix="/csv",
    tags=["csv"],
    dependencies=[Depends(get_current_user_dep), Depends(require_permission(PERMISSION_CSV))],
)


@router.post(
    "/resource/upload",
    summary="上传公共 CSV 数据文件",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def upload_public_csv(
    overwrite: bool = Query(False),
    csvFile: UploadFile = File(...),
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.upload_public_csv(db, csvFile, current, overwrite=overwrite)
    return success(ok)


@router.get(
    "/resource/list",
    summary="分页查询公共 CSV",
    response_model=Response[PageVO[CsvResourceVO]],
    response_model_by_alias=True,
)
async def list_public_csvs(
    query: CsvResourceQuery = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Response[PageVO[CsvResourceVO]]:
    page = await service.get_public_csv_list(db, query)
    return success(page)


@router.get("/resource/view/{filename}", summary="公共 CSV 文件预览")
async def view_public_csv(filename: str, db: AsyncSession = Depends(get_db)) -> StreamingResponse:
    csv = await service.get_public_csv_vo(db, filename)
    filepath = os.path.join(csv.file_dir, csv.filename)
    return _read_file_with_bom(filepath, csv.filename)


@router.get("/resource/download/{filename}", summary="公共 CSV 文件下载")
async def download_public_csv(filename: str, db: AsyncSession = Depends(get_db)) -> FileResponse:
    csv = await service.get_public_csv_vo(db, filename)
    filepath = os.path.join(csv.file_dir, csv.filename)
    if not os.path.exists(filepath):
        raise MysteriousException(Codes.FILE_NOT_EXIST)
    return FileResponse(
        filepath,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{quote(csv.filename)}"'},
    )


@router.post(
    "/resource/update/{filename}",
    summary="更新公共 CSV 文件内容",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def update_public_csv(
    filename: str,
    content: str = Body(...),
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.update_public_csv_content(db, filename, content, current)
    return success(ok)


@router.get(
    "/resource/delete/{filename}",
    summary="删除公共 CSV 文件",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def delete_public_csv(
    filename: str,
    force: bool = Query(False),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.delete_public_csv(db, filename, force=force)
    return success(ok)


@router.post(
    "/binding/add",
    summary="绑定公共 CSV 到用例",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def add_public_csv_binding(
    param: CsvBindingParam,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.bind_public_csv(db, param, current)
    return success(ok)


@router.get(
    "/binding/getByTestCaseId",
    summary="查询用例绑定的公共 CSV",
    response_model=Response[list[CsvBindingVO]],
    response_model_by_alias=True,
)
async def get_public_csv_bindings(
    testCaseId: int,
    db: AsyncSession = Depends(get_db),
) -> Response[list[CsvBindingVO]]:
    items = await service.get_bindings_by_test_case_id(db, testCaseId)
    return success(items)


@router.get(
    "/binding/delete/{id}",
    summary="解绑公共 CSV",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def delete_public_csv_binding(id: int, db: AsyncSession = Depends(get_db)) -> Response[bool]:
    ok = await service.delete_binding(db, id)
    return success(ok)


@router.post(
    "/binding/updateStrategy/{id}",
    summary="更新公共 CSV 绑定分布式读取策略",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def update_public_csv_binding_strategy(
    id: int,
    param: CsvStrategyParam,
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.update_binding_strategy(db, id, param.distribution_strategy, current)
    return success(ok)


def _read_file_with_bom(filepath: str, src_name: str) -> StreamingResponse:
    """CSV view：响应体前面加 UTF-8 BOM (EF BB BF) 防 Excel 打开乱码。对齐 Java"""
    if not os.path.exists(filepath):
        raise MysteriousException(Codes.FILE_NOT_EXIST)

    def _gen():
        yield b"\xef\xbb\xbf"
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        _gen(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{quote(src_name)}"'},
    )

