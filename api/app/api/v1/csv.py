"""/csv/* 路由。所有端点要求登录。"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import Codes
from app.core.context import UserContext
from app.core.exceptions import MysteriousException
from app.core.response import PageVO, Response, success
from app.db.session import get_db
from app.deps.auth import get_current_user_dep
from app.schemas.csv import CsvQuery, CsvVO
from app.services import csv as service

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/csv",
    tags=["csv"],
    dependencies=[Depends(get_current_user_dep)],
)


@router.post(
    "/upload/{testcase_id}",
    summary="上传 CSV 数据文件",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def upload_csv(
    testcase_id: int,
    csvFile: UploadFile = File(...),
    current: UserContext = Depends(get_current_user_dep),
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.upload_csv(db, testcase_id, csvFile, current)
    return success(ok)


@router.get(
    "/delete/{id}",
    summary="删除 CSV 文件",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def delete_csv(
    id: int,
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.delete_csv(db, id)
    return success(ok)


@router.get(
    "/list",
    summary="分页查询 CSV",
    response_model=Response[PageVO[CsvVO]],
    response_model_by_alias=True,
)
async def list_csvs(
    query: CsvQuery = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Response[PageVO[CsvVO]]:
    page = await service.get_csv_list(db, query)
    return success(page)


@router.get(
    "/getByTestCaseId",
    summary="查询用例关联的 CSV",
    response_model=Response[list[CsvVO]],
    response_model_by_alias=True,
)
async def get_by_testcase_id(
    testCaseId: int,
    db: AsyncSession = Depends(get_db),
) -> Response[list[CsvVO]]:
    items = await service.get_by_test_case_id(db, testCaseId)
    return success(items)


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


@router.get("/view/{id}", summary="CSV 文件预览")
async def view_csv(id: int, db: AsyncSession = Depends(get_db)) -> StreamingResponse:
    csv = await service.get_csv_vo(db, id)
    filepath = os.path.join(csv.csv_dir, csv.src_name)
    return _read_file_with_bom(filepath, csv.src_name)


@router.get("/download/{id}", summary="CSV 文件下载")
async def download_csv(id: int, db: AsyncSession = Depends(get_db)) -> FileResponse:
    csv = await service.get_csv_vo(db, id)
    filepath = os.path.join(csv.csv_dir, csv.src_name)
    if not os.path.exists(filepath):
        raise MysteriousException(Codes.FILE_NOT_EXIST)
    return FileResponse(
        filepath,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{quote(csv.src_name)}"'},
    )
