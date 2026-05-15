"""/report/* 路由。Phase 5 最小版本：list / listByTestCase / getById。
Phase 6 补齐 download / clean / view。
"""

from __future__ import annotations

import os
from urllib.parse import quote

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import Codes
from app.core.exceptions import MysteriousException
from app.core.response import PageVO, Response, success
from app.db.session import get_db
from app.deps.auth import get_current_user_dep
from app.schemas.report import ReportByTestCaseQuery, ReportQuery, ReportVO
from app.services import report as service

router = APIRouter(
    prefix="/report",
    tags=["report"],
    dependencies=[Depends(get_current_user_dep)],
)


@router.get(
    "/list",
    summary="报告列表",
    response_model=Response[PageVO[ReportVO]],
    response_model_by_alias=True,
)
async def get_report_list(
    query: ReportQuery = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Response[PageVO[ReportVO]]:
    page = await service.get_report_list(db, query)
    return success(page)


@router.get(
    "/listByTestCase",
    summary="用例执行的报告列表",
    response_model=Response[PageVO[ReportVO]],
    response_model_by_alias=True,
)
async def get_report_list_by_test_case(
    query: ReportByTestCaseQuery = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Response[PageVO[ReportVO]]:
    page = await service.get_report_list_by_test_case(db, query)
    return success(page)


@router.get(
    "/getById/{id}",
    summary="报告详情",
    response_model=Response[ReportVO | None],
    response_model_by_alias=True,
)
async def get_by_id(
    id: int,
    db: AsyncSession = Depends(get_db),
) -> Response[ReportVO | None]:
    obj = await service.get_by_id(db, id)
    return success(obj)


@router.get(
    "/clean/{id}",
    summary="清理报告（删 DB + 删磁盘）",
    response_model=Response[bool],
    response_model_by_alias=True,
)
async def clean_report(
    id: int,
    db: AsyncSession = Depends(get_db),
) -> Response[bool]:
    ok = await service.clean_report(db, id)
    return success(ok)


@router.get(
    "/download/{id}",
    summary="下载压测报告（zip）",
)
async def download_report(
    id: int,
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    zip_path = await service.download_report(db, id)
    filename = os.path.basename(zip_path)
    return FileResponse(
        zip_path,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{quote(filename)}"'},
    )


@router.get(
    "/view/{id}",
    summary="预览压测报告（返回 index.html URL）",
    response_model=Response[str],
    response_model_by_alias=True,
)
async def view_report(
    id: int,
    db: AsyncSession = Depends(get_db),
) -> Response[str]:
    url = await service.view_report(db, id)
    return success(url)


@router.get(
    "/getJMeterLog/{id}",
    summary="查看 JMeter 日志",
    response_model=Response[str],
    response_model_by_alias=True,
)
async def get_jmeter_log(
    id: int,
    db: AsyncSession = Depends(get_db),
) -> Response[str]:
    content = await service.get_jmeter_log(db, id)
    return success(content)
