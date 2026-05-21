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
        region=param.region or "",
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
    total = await crud.count(db, name=query.name, region=query.region)
    if total <= 0:
        return page_vo
    page_vo.total = total
    offset = PageVO.offset(query.page, query.size)
    items = await crud.list_reports(db, name=query.name, region=query.region, offset=offset, limit=query.size)
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


def _find_jtl_file(report_dir: str) -> str | None:
    """查找 JTL 文件。

    report_dir 运行时指向 data/ 或 jtl/ 子目录，
    取父目录（timestamp 级别）再找 jtl/ 子目录。
    """
    if not report_dir:
        return None
    # report_dir 以 /data/ 或 /jtl/ 结尾，取父目录
    parent = os.path.dirname(report_dir.rstrip(os.sep))
    jtl_dir = os.path.join(parent, "jtl")
    if not os.path.isdir(jtl_dir):
        return None
    for name in os.listdir(jtl_dir):
        if name.endswith(".jtl") or name.endswith(".xml"):
            return os.path.join(jtl_dir, name)
    return None


def _percentile(sorted_values: list[float], p: float) -> float:
    """计算已排序数组的百分位数。"""
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    k = (n - 1) * p / 100.0
    f = int(k)
    c = f + 1
    if c >= n:
        return sorted_values[-1]
    return sorted_values[f] + (k - f) * (sorted_values[c] - sorted_values[f])


def _parse_jtl_metrics(jtl_path: str, window_sec: int = 5) -> list[dict]:
    """解析 JTL 文件，按时间窗口聚合指标。"""
    import csv
    from datetime import datetime

    window_ms = window_sec * 1000
    buckets: dict[int, dict] = {}

    try:
        with open(jtl_path, encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    ts = int(row.get("timeStamp", "0"))
                    elapsed = int(row.get("elapsed", "0"))
                    success = row.get("success", "true").lower() == "true"
                    threads = int(row.get("allThreads", "0") or row.get("grpThreads", "0"))
                except (ValueError, TypeError):
                    continue
                if ts <= 0:
                    continue
                key = ts // window_ms * window_ms
                bucket = buckets.setdefault(
                    key,
                    {"elapsed": [], "fail": 0, "threads": 0, "count": 0},
                )
                bucket["elapsed"].append(elapsed)
                if not success:
                    bucket["fail"] += 1
                bucket["threads"] = threads
                bucket["count"] += 1
    except OSError as e:
        log.warning("读取 JTL 失败: %s", e)
        return []

    results = []
    for key in sorted(buckets):
        b = buckets[key]
        count = b["count"]
        if count == 0:
            continue
        elapsed_sorted = sorted(b["elapsed"])
        dt = datetime.fromtimestamp(key / 1000.0)
        results.append(
            {
                "timestamp": dt.strftime("%H:%M:%S"),
                "qps": round(count / window_sec, 1),
                "avg_rt": round(sum(elapsed_sorted) / count, 1),
                "p99_rt": round(_percentile(elapsed_sorted, 99), 1),
                "error_rate": round(b["fail"] / count * 100, 2),
                "threads": b["threads"],
            }
        )
    return results


async def get_jtl_metrics(
    db: AsyncSession, report_id: int, window_sec: int = 5
) -> list[dict]:
    """读取指定报告 JTL 文件，按窗口聚合返回监控指标。"""
    rpt = await crud.get_by_id(db, report_id)
    if rpt is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    jtl_path = _find_jtl_file(rpt.report_dir)
    if not jtl_path:
        return []

    return _parse_jtl_metrics(jtl_path, window_sec)


def _normalize_to_relative(items: list[dict]) -> list[dict]:
    """将绝对时间戳转换为相对偏移（从 0s 开始），方便两份报告叠加对比。"""
    if not items:
        return []
    # 原始 timestamp 是 "%H:%M:%S" 格式，先转成秒数
    from datetime import datetime

    def _to_seconds(ts: str) -> int:
        try:
            dt = datetime.strptime(ts, "%H:%M:%S")
            return dt.hour * 3600 + dt.minute * 60 + dt.second
        except ValueError:
            return 0

    base_sec = _to_seconds(items[0]["timestamp"])
    out = []
    for item in items:
        sec = _to_seconds(item["timestamp"]) - base_sec
        out.append({**item, "timestamp": f"{sec}s"})
    return out


async def compare_reports(
    db: AsyncSession, base_id: int, target_id: int, window_sec: int = 5
) -> dict:
    """对比两份报告的 JTL 指标，返回相对时间轴数据。"""
    base_rpt = await crud.get_by_id(db, base_id)
    target_rpt = await crud.get_by_id(db, target_id)
    if base_rpt is None or target_rpt is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    base_metrics = await get_jtl_metrics(db, base_id, window_sec)
    target_metrics = await get_jtl_metrics(db, target_id, window_sec)

    return {
        "base_name": base_rpt.name or f"报告 #{base_id}",
        "target_name": target_rpt.name or f"报告 #{target_id}",
        "base": _normalize_to_relative(base_metrics),
        "target": _normalize_to_relative(target_metrics),
    }


async def get_jmeter_result_by_report(db: AsyncSession, report_id: int) -> list:
    """读取指定报告 jmeter.log 的实时 summary 数据。（兼容旧接口）"""

    from app.schemas.testcase import JMeterResultVO
    import re

    _RESULT_RE = re.compile(
        r"\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2}),\d{3} INFO.*summary \+.* (\d+\.\d+)/s Avg: +(\d+)"
    )

    rpt = await crud.get_by_id(db, report_id)
    if rpt is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    log_path = rpt.jmeter_log_file_path
    if not log_path or not Path(log_path).exists():
        return []

    results: list = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _RESULT_RE.search(line)
                if m:
                    results.append(
                        JMeterResultVO(
                            currentTime=m.group(1),
                            throughput=float(m.group(2)),
                            avgResponseTime=float(m.group(3)),
                        )
                    )
    except OSError as e:
        log.warning("读取 jmeter.log 失败: %s", e)
        raise MysteriousException(Codes.FAIL, message="实时数据读取失败") from e
    return results
