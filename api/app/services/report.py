"""Report 业务服务。Phase 5 最小版本：list / getById / listByTestCase / add（仅供 jmeter_runner / debug_testcase / run_testcase 内部调用）。

Phase 6 补齐 download / clean / view。
"""

from __future__ import annotations

import logging
import os
import shutil
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import stamp_create, stamp_modify
from app.core.codes import Codes
from app.core.context import UserContext
from app.core.enums import ExecType
from app.core.exceptions import MysteriousException
from app.core.response import PageVO
from app.crud import report as crud
from app.models.report import Report
from app.models.report_metric_snapshot import ReportMetricSnapshot
from app.models.report_transaction_metric_snapshot import ReportTransactionMetricSnapshot
from app.models.report_transaction_snapshot import ReportTransactionSnapshot
from app.schemas.report import (
    ArtifactVO,
    ReportByTestCaseQuery,
    ReportParam,
    ReportQuery,
    ReportStatsVO,
    ReportVO,
)
from app.services import config as config_service
from app.services.prometheus import (
    DEFAULT_PROMETHEUS_RESOURCE_METRICS,
    _join_url,
    _parse_instance_map,
    _parse_offset_minutes,
    _query_prometheus_range,
    _resolve_grafana_instance,
    _to_epoch_ms,
    get_resource_metrics,
    get_resource_targets,
    resolve_grafana_instance,
)
from app.services.report_metrics import (
    DEFAULT_METRIC_WINDOWS,
    _find_jtl_file,
    _load_run_meta,
    _metric_snapshot_last_refresh,
    _normalize_to_relative,
    _parse_jtl_metrics,
    compare_reports,
    generate_metric_snapshots_for_report,
    get_jtl_metrics,
    schedule_metric_snapshot_generation,
)
from app.services.report_transactions import (
    generate_transaction_metric_snapshots_for_report,
    generate_transaction_snapshots_for_report,
    get_transaction_metrics,
    get_transaction_stats,
    get_transaction_trend,
    schedule_transaction_metric_snapshot_generation,
    schedule_transaction_snapshot_generation,
)


log = logging.getLogger(__name__)


def _to_vo(obj: Report, occupied_node_hosts: list[str] | None = None) -> ReportVO:
    vo = ReportVO.model_validate(obj)
    vo.occupied_node_hosts = occupied_node_hosts or []
    return vo


async def _to_vo_list_with_occupied_nodes(db: AsyncSession, items: list[Report]) -> list[ReportVO]:
    from app.services import execution_node

    host_map = await execution_node.active_hosts_by_report_ids(db, [item.id for item in items])
    return [_to_vo(item, host_map.get(item.id, [])) for item in items]


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
        service_name=param.service_name or "",
        total_threads=param.total_threads or 0,
        slave_count=param.slave_count or 0,
        grafana_instance=param.grafana_instance or "",
        artifact_dir=param.artifact_dir or "",
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
    if obj is None:
        return None
    return (await _to_vo_list_with_occupied_nodes(db, [obj]))[0]


async def get_report_list(db: AsyncSession, query: ReportQuery) -> PageVO[ReportVO]:
    page_vo: PageVO[ReportVO] = PageVO(page=query.page, size=query.size, total=0, list=[])
    total = await crud.count(db, name=query.name, region=query.region, exec_type=query.exec_type)
    if total <= 0:
        return page_vo
    page_vo.total = total
    offset = PageVO.offset(query.page, query.size)
    items = await crud.list_reports(
        db,
        name=query.name,
        region=query.region,
        exec_type=query.exec_type,
        offset=offset,
        limit=query.size,
    )
    page_vo.list = await _to_vo_list_with_occupied_nodes(db, items)
    return page_vo


async def get_report_list_by_test_case(
    db: AsyncSession, query: ReportByTestCaseQuery
) -> PageVO[ReportVO]:
    page_vo: PageVO[ReportVO] = PageVO(page=query.page, size=query.size, total=0, list=[])
    total = await crud.count(
        db,
        name=query.name,
        test_case_id=query.test_case_id,
        exec_type=query.exec_type,
    )
    if total <= 0:
        return page_vo
    page_vo.total = total
    offset = PageVO.offset(query.page, query.size)
    items = await crud.list_by_test_case(
        db,
        name=query.name,
        test_case_id=query.test_case_id,
        exec_type=query.exec_type,
        offset=offset,
        limit=query.size,
    )
    page_vo.list = await _to_vo_list_with_occupied_nodes(db, items)
    return page_vo


async def get_report_stats(db: AsyncSession, query: ReportQuery) -> ReportStatsVO:
    counts = await crud.count_by_status(
        db,
        name=query.name,
        region=query.region,
        exec_type=query.exec_type,
    )
    success = counts.get(2, 0)
    failed = counts.get(3, 0)
    executed = success + failed
    success_rate = 100.0 if executed == 0 else round(success / executed * 100, 1)
    return ReportStatsVO(
        total=sum(counts.values()),
        idle=counts.get(0, 0),
        running=counts.get(1, 0),
        success=success,
        failed=failed,
        success_rate=success_rate,
    )


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
    await db.execute(sql_delete(ReportMetricSnapshot).where(ReportMetricSnapshot.report_id == id))
    await db.execute(sql_delete(ReportTransactionMetricSnapshot).where(ReportTransactionMetricSnapshot.report_id == id))
    await db.execute(sql_delete(ReportTransactionSnapshot).where(ReportTransactionSnapshot.report_id == id))
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



async def get_grafana_url(db: AsyncSession, id: int) -> str:
    """根据报告时间窗口和服务实例生成 Grafana 资源监控跳转地址。"""
    report = await crud.get_by_id(db, id)
    if report is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    url = await config_service.get_value_or_default(db, "GRAFANA_DASHBOARD_URL", "")
    if not url:
        base_url = await config_service.get_value(db, "GRAFANA_BASE_URL")
        dashboard_path = await config_service.get_value(db, "GRAFANA_DASHBOARD_PATH")
        url = _join_url(base_url, dashboard_path)

    org_id = await config_service.get_value_or_default(db, "GRAFANA_ORG_ID", "")
    instance_var = await config_service.get_value_or_default(db, "GRAFANA_INSTANCE_VAR", "instance")
    instance = await _resolve_grafana_instance(db, report)
    from_offset = _parse_offset_minutes(
        await config_service.get_value_or_default(db, "GRAFANA_FROM_OFFSET_MINUTES", "15"),
        15,
    )
    to_offset = _parse_offset_minutes(
        await config_service.get_value_or_default(db, "GRAFANA_TO_OFFSET_MINUTES", "15"),
        15,
    )

    start = report.create_time
    end = report.modify_time or report.create_time
    if start and end and end < start:
        end = start

    params = {
        "from": str(_to_epoch_ms(start - timedelta(minutes=from_offset) if start else None)),
        "to": str(_to_epoch_ms(end + timedelta(minutes=to_offset) if end else None)),
    }
    if org_id:
        params["orgId"] = org_id
    if instance:
        params[f"var-{instance_var}"] = instance

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


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


def _find_report_root(report_dir: str) -> str | None:
    if not report_dir:
        return None
    return os.path.dirname(report_dir.rstrip(os.sep))


def _artifact_dir(report_dir: str) -> str | None:
    report_root = _find_report_root(report_dir)
    if not report_root:
        return None
    return os.path.join(report_root, "artifacts")


def _report_artifact_dir(report: Report) -> str | None:
    if report.artifact_dir:
        return report.artifact_dir
    return _artifact_dir(report.report_dir)


def _safe_artifact_path(artifact_dir: str | None, name: str) -> str:
    if not name or Path(name).name != name:
        raise MysteriousException(Codes.PARAM_WRONG, message="产物文件名不合法")

    if not artifact_dir:
        raise MysteriousException(Codes.REPORT_DIR_NOT_EXIST)

    root = Path(artifact_dir).resolve()
    path = (root / name).resolve()
    if path.parent != root:
        raise MysteriousException(Codes.PARAM_WRONG, message="产物文件名不合法")
    return str(path)


async def list_artifacts(db: AsyncSession, id: int) -> list[ArtifactVO]:
    report = await crud.get_by_id(db, id)
    if report is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    artifact_dir = _report_artifact_dir(report)
    if not artifact_dir or not os.path.isdir(artifact_dir):
        return []

    items: list[ArtifactVO] = []
    for path in sorted(Path(artifact_dir).iterdir(), key=lambda p: p.name):
        if not path.is_file():
            continue
        stat = path.stat()
        items.append(
            ArtifactVO(
                name=path.name,
                size=stat.st_size,
                modify_time=datetime.fromtimestamp(stat.st_mtime),
            )
        )
    return items


async def download_artifact(db: AsyncSession, id: int, name: str) -> str:
    report = await crud.get_by_id(db, id)
    if report is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    path = _safe_artifact_path(_report_artifact_dir(report), name)
    if not os.path.isfile(path):
        raise MysteriousException(Codes.FILE_NOT_EXIST)
    return path


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
