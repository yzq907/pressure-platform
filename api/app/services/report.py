"""Report 业务服务。Phase 5 最小版本：list / getById / listByTestCase / add（仅供 jmeter_runner / debug_testcase / run_testcase 内部调用）。

Phase 6 补齐 download / clean / view。
"""

from __future__ import annotations

import logging
import os
import shutil
import zipfile
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import stamp_create, stamp_modify
from app.core.codes import Codes
from app.core.context import UserContext
from app.core.enums import ExecType
from app.core.exceptions import MysteriousException
from app.core.response import PageVO
from app.crud import report as crud
from app.models.report import Report
from app.schemas.report import ReportByTestCaseQuery, ReportParam, ReportQuery, ReportVO
from app.services import config as config_service

log = logging.getLogger(__name__)


def _to_vo(obj: Report) -> ReportVO:
    return ReportVO.model_validate(obj)


async def add_report(db: AsyncSession, param: ReportParam, user: UserContext) -> int:
    """供 debug_testcase / run_testcase 内部调用。"""
    if param is None:
        raise MysteriousException(Codes.PARAMS_EMPTY)
    obj = Report(
        name=param.name or "",
        description=param.description or "",
        test_case_id=param.test_case_id or 0,
        report_dir=param.report_dir or "",
        exec_type=param.exec_type if param.exec_type is not None else 1,
        status=param.status if param.status is not None else 0,
        response_data=param.response_data or "",
        jmeter_log_file_path=param.jmeter_log_file_path or "",
    )
    stamp_create(obj, user)
    await crud.add(db, obj)
    return obj.id


async def update_status(db: AsyncSession, id: int, status: int) -> bool:
    return await crud.update_status(db, id, status)


async def update_report(db: AsyncSession, obj: Report, user: UserContext | None) -> bool:
    """供 jmeter_runner callback 在完成时更新 response_data + status 等"""
    if user is not None:
        stamp_modify(obj, user)
    return await crud.update(db, obj)


async def get_by_id(db: AsyncSession, id: int) -> ReportVO | None:
    obj = await crud.get_by_id(db, id)
    return _to_vo(obj) if obj else None


async def get_report_list(db: AsyncSession, query: ReportQuery) -> PageVO[ReportVO]:
    page_vo: PageVO[ReportVO] = PageVO(page=query.page, size=query.size, total=0, list=[])
    total = await crud.count(db, name=query.name)
    if total <= 0:
        return page_vo
    page_vo.total = total
    offset = PageVO.offset(query.page, query.size)
    items = await crud.list_reports(db, name=query.name, offset=offset, limit=query.size)
    page_vo.list = [_to_vo(o) for o in items]
    return page_vo


async def get_report_list_by_test_case(
    db: AsyncSession, query: ReportByTestCaseQuery
) -> PageVO[ReportVO]:
    page_vo: PageVO[ReportVO] = PageVO(page=query.page, size=query.size, total=0, list=[])
    total = await crud.count(db, name=query.name, test_case_id=query.test_case_id)
    if total <= 0:
        return page_vo
    page_vo.total = total
    offset = PageVO.offset(query.page, query.size)
    items = await crud.list_by_test_case(
        db, name=query.name, test_case_id=query.test_case_id, offset=offset, limit=query.size
    )
    page_vo.list = [_to_vo(o) for o in items]
    return page_vo


async def get_debug_reports_by_test_case_id(
    db: AsyncSession, test_case_id: int, exec_type: int | None, limit: int
) -> list[ReportVO]:
    """供 testcase getJMeterResult 用：拉最近 N 条该用例的报告"""
    items = await crud.get_debug_reports_by_test_case_id(db, test_case_id, exec_type, limit)
    return [_to_vo(o) for o in items]


async def clean_report(db: AsyncSession, id: int) -> bool:
    """清理报告：先删 DB 记录，再删磁盘目录（有问题无法回滚，但 Java 也是这个顺序）。

    删除磁盘时取 report_dir 的父目录（../timestamp/ 级别），
    因为 report_dir 指向的是 data/ 或 jtl/ 子目录。
    """
    report = await crud.get_by_id(db, id)
    if report is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    log.info("清理测试报告, id: %s", id)
    await crud.delete(db, id)

    report_dir = report.report_dir or ""
    clean_dir = _parent_timestamp_dir(report_dir)
    if clean_dir and os.path.exists(clean_dir):
        shutil.rmtree(clean_dir, ignore_errors=True)
    return True


def _parent_timestamp_dir(report_dir: str) -> str | None:
    """对齐 Java 逻辑：去掉末尾 / 后找最后一个 /，取父目录。"""
    if not report_dir:
        return None
    normalized = report_dir.rstrip("/")
    last_idx = normalized.rfind("/")
    if last_idx <= 0:
        return None
    return normalized[:last_idx]


async def download_report(db: AsyncSession, id: int) -> str:
    """下载报告：将 report_dir 下的 data/ 目录打包成 zip 返回 zip 文件路径。

    - DEBUG 报告不可下载
    - 目录不存在 / 为空报错
    - zip 已存在则直接返回，不重新打包
    """
    report = await crud.get_by_id(db, id)
    if report is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    if report.exec_type == ExecType.DEBUG.value:
        raise MysteriousException(Codes.DEBUG_REPORT_NOT_DOWNLOAD)

    report_dir = report.report_dir or ""
    if not os.path.exists(report_dir):
        raise MysteriousException(Codes.REPORT_DIR_NOT_EXIST)

    if not os.listdir(report_dir):
        raise MysteriousException(Codes.REPORT_DIR_IS_EMPTY)

    # reportDir 以 /data/ 结尾，取前面部分
    report_path = report_dir[: report_dir.rfind("data")]
    src_path = report_path + "data"
    zip_path = report_path + report.name + ".zip"

    if not os.path.exists(zip_path):
        _compress_directory(src_path, zip_path)

    return zip_path


def _compress_directory(src_dir: str, dest_zip: str) -> None:
    """将 src_dir 压缩为 dest_zip（保留目录结构）。"""
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(src_dir):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, src_dir)
                zf.write(file_path, arcname)


async def view_report(db: AsyncSession, id: int) -> str:
    """预览报告：返回 index.html 的完整 URL。

    - 只有 RUNNING(exec_type=2) 类型报告可预览
    - 目录不存在 / 为空报错
    """
    report = await crud.get_by_id(db, id)
    if report is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    if report.exec_type != ExecType.EXEC.value:
        raise MysteriousException(Codes.DEBUG_REPORT_NOT_VIEW)

    report_dir = report.report_dir or ""
    if not os.path.exists(report_dir):
        raise MysteriousException(Codes.REPORT_DIR_NOT_EXIST)

    if not os.listdir(report_dir):
        raise MysteriousException(Codes.REPORT_DIR_IS_EMPTY)

    # 构造相对路径：去掉 mysterious-data 前缀或 /data 前缀
    if "mysterious-data" in report_dir:
        relative = report_dir.split("mysterious-data")[1]
    else:
        relative = report_dir.lstrip("/data")

    if not relative.endswith("/"):
        relative += "/"

    host = await config_service.get_value(db, "MASTER_HOST_PORT")
    return f"http://{host}/reports{relative}index.html"


async def get_jmeter_log(db: AsyncSession, id: int) -> str:
    """返回报告的 jmeter.log 文件内容。"""
    report = await crud.get_by_id(db, id)
    if report is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    log_path = report.jmeter_log_file_path
    if not log_path or not Path(log_path).exists():
        raise MysteriousException(Codes.FILE_NOT_EXIST)

    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError as e:
        log.warning("读取 jmeter.log 失败: %s", e)
        raise MysteriousException(Codes.FAIL, message="日志读取失败") from e
