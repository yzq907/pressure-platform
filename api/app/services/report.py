"""Report 业务服务。Phase 5 最小版本：list / getById / listByTestCase / add（仅供 jmeter_runner / debug_testcase / run_testcase 内部调用）。

Phase 6 补齐 download / clean / view。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import zipfile
import asyncio
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy import delete as sql_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import stamp_create, stamp_modify
from app.core.codes import Codes
from app.core.context import UserContext
from app.core.enums import ExecType, TestCaseStatus
from app.core.exceptions import MysteriousException
from app.core.response import PageVO
from app.crud import report as crud, testcase as testcase_crud
from app.db import session as session_module
from app.models.report import Report
from app.models.execution_run import ExecutionRun
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
    ResourceMetricsVO,
    ResourceTargetVO,
    TransactionMetricPointVO,
    TransactionMetricsVO,
    TransactionStatsVO,
    TransactionTrendVO,
    TrendPointVO,
)
from app.services import config as config_service

log = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_METRIC_WINDOWS = (5,)
_metric_snapshot_tasks: set[tuple[int, tuple[int, ...]]] = set()
_transaction_snapshot_tasks: set[int] = set()
_transaction_metric_snapshot_tasks: set[tuple[int, tuple[int, ...]]] = set()
_METRIC_RUNNING_REFRESH_INTERVAL_SECONDS = 10.0
_metric_snapshot_last_refresh: dict[tuple[int, int], float] = {}
DEFAULT_PROMETHEUS_RESOURCE_METRICS = ("cpu", "memory", "load", "networkIn", "networkOut", "diskRead", "diskWrite", "gc")
TRANSACTION_METRIC_SNAPSHOT_VERSION = "tm_v2"


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


def _to_epoch_ms(dt: datetime | None) -> int:
    if dt is None:
        dt = datetime.now(SHANGHAI)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI)
    return int(dt.timestamp() * 1000)


def _parse_offset_minutes(raw: str, default: int) -> int:
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def _join_url(base_url: str, dashboard_path: str) -> str:
    return f"{base_url.rstrip('/')}/{dashboard_path.lstrip('/')}"


def _parse_instance_map(raw: str) -> dict[str, str]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if k and v}


def _parse_resource_group_map(raw: str) -> dict[str, list[ResourceTargetVO]]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}

    groups: dict[str, list[ResourceTargetVO]] = {}
    for group_key, group_value in data.items():
        if not group_key:
            continue
        raw_targets = group_value
        if isinstance(group_value, dict):
            raw_targets = group_value.get("targets")
        if not isinstance(raw_targets, list):
            continue

        targets: list[ResourceTargetVO] = []
        for item in raw_targets:
            if not isinstance(item, dict):
                continue
            instance = str(item.get("instance") or "").strip()
            if not instance:
                continue
            service = str(item.get("service") or item.get("name") or group_key).strip()
            name = str(item.get("name") or service or instance).strip()
            role = str(item.get("role") or "相关服务").strip()
            targets.append(
                ResourceTargetVO(
                    name=name,
                    role=role,
                    service=service,
                    instance=instance,
                )
            )
        if targets:
            groups[str(group_key)] = targets
    return groups


def _quote_prom_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _prometheus_resource_queries(instance: str, instance_label: str) -> dict[str, str]:
    selector = f'{instance_label}="{_quote_prom_label(instance)}"'
    return {
        "cpu": (
            "100 - (avg(rate(node_cpu_seconds_total{"
            f'{selector},mode="idle"'
            "}[1m])) * 100)"
        ),
        "memory": (
            "(1 - node_memory_MemAvailable_bytes{"
            f"{selector}"
            "} / node_memory_MemTotal_bytes{"
            f"{selector}"
            "}) * 100"
        ),
        "load": f"node_load1{{{selector}}}",
        "networkIn": (
            "sum(rate(node_network_receive_bytes_total{"
            f'{selector},device!="lo"'
            "}[1m]))"
        ),
        "networkOut": (
            "sum(rate(node_network_transmit_bytes_total{"
            f'{selector},device!="lo"'
            "}[1m]))"
        ),
        "diskRead": f"sum(rate(node_disk_read_bytes_total{{{selector}}}[1m]))",
        "diskWrite": f"sum(rate(node_disk_written_bytes_total{{{selector}}}[1m]))",
        "gc": f"sum(rate(jvm_gc_pause_seconds_sum{{{selector}}}[1m]))",
    }


def _parse_prometheus_matrix(payload: dict) -> list[dict]:
    if payload.get("status") != "success":
        raise MysteriousException(Codes.FAIL, message="Prometheus 查询失败")
    results = ((payload.get("data") or {}).get("result") or [])
    points: list[dict] = []
    for result in results:
        for raw_ts, raw_value in result.get("values") or []:
            try:
                ts = float(raw_ts)
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            dt = datetime.fromtimestamp(ts, tz=SHANGHAI)
            points.append({
                "timestamp": dt.strftime("%H:%M:%S"),
                "timestamp_ms": int(ts * 1000),
                "value": round(value, 4),
            })
    return points


async def _query_prometheus_range(
    base_url: str,
    query: str,
    start: int,
    end: int,
    step: int,
    timeout: int,
) -> list[dict]:
    params = urlencode({
        "query": query,
        "start": start,
        "end": end,
        "step": step,
    })
    url = f"{base_url.rstrip('/')}/api/v1/query_range?{params}"

    def _fetch() -> dict:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    try:
        payload = await asyncio.to_thread(_fetch)
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        log.warning("Prometheus 查询失败: %s", e)
        raise MysteriousException(Codes.FAIL, message="Prometheus 查询失败或超时") from e
    return _parse_prometheus_matrix(payload)


async def resolve_grafana_instance(
    db: AsyncSession,
    service_name: str = "",
    testcase_name: str = "",
    report_name: str = "",
) -> str:
    """按服务名等候选 key 解析 Grafana instance，例如 EMM-API -> 10.10.27.42:9200。"""
    mapping = _parse_instance_map(
        await config_service.get_value_or_default(db, "GRAFANA_INSTANCE_MAP", "")
    )
    candidates = [
        service_name,
        testcase_name,
        report_name,
    ]
    for key in candidates:
        if key and key in mapping:
            return mapping[key]
    return await config_service.get_value_or_default(db, "GRAFANA_DEFAULT_INSTANCE", "")


async def _resolve_grafana_instance(db: AsyncSession, report: Report) -> str:
    """优先使用报告快照；老报告没有快照时回退到当前用例信息。"""
    if report.grafana_instance:
        return report.grafana_instance

    testcase = await testcase_crud.get_by_id(db, report.test_case_id)
    return await resolve_grafana_instance(
        db,
        service_name=report.service_name or (testcase.service if testcase else ""),
        testcase_name=testcase.name if testcase else "",
        report_name=report.name,
    )


async def _resolve_prometheus_instance(db: AsyncSession, report: Report) -> str:
    mapping = _parse_instance_map(await config_service.get_value_or_default(db, "PROMETHEUS_INSTANCE_MAP", ""))
    testcase = await testcase_crud.get_by_id(db, report.test_case_id)
    candidates = [
        report.service_name or (testcase.service if testcase else ""),
        testcase.name if testcase else "",
        report.name,
    ]
    for key in candidates:
        if key and key in mapping:
            return mapping[key]
    default = await config_service.get_value_or_default(db, "PROMETHEUS_DEFAULT_INSTANCE", "")
    if default:
        return default
    if report.grafana_instance:
        return report.grafana_instance
    return await _resolve_grafana_instance(db, report)


async def _prometheus_candidates(db: AsyncSession, report: Report) -> list[str]:
    testcase = await testcase_crud.get_by_id(db, report.test_case_id)
    candidates = [
        report.service_name or (testcase.service if testcase else ""),
        testcase.name if testcase else "",
        report.name,
    ]
    return [item for item in candidates if item]


async def get_resource_targets(db: AsyncSession, id: int) -> list[ResourceTargetVO]:
    report = await crud.get_by_id(db, id)
    if report is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    groups = _parse_resource_group_map(
        await config_service.get_value_or_default(db, "PROMETHEUS_RESOURCE_GROUP_MAP", "")
    )
    candidates = await _prometheus_candidates(db, report)
    for key in candidates:
        if key in groups:
            return groups[key]

    instance = await _resolve_prometheus_instance(db, report)
    if not instance:
        return []
    service = candidates[0] if candidates else ""
    return [
        ResourceTargetVO(
            name=service or instance,
            role="被压服务",
            service=service or "",
            instance=instance,
        )
    ]


def _report_metric_time_range(report: Report, from_offset: int, to_offset: int) -> tuple[int, int]:
    start = report.create_time
    end = report.modify_time or report.create_time
    if start and end and end < start:
        end = start
    from_ms = _to_epoch_ms(start - timedelta(minutes=from_offset) if start else None)
    to_ms = _to_epoch_ms(end + timedelta(minutes=to_offset) if end else None)
    return from_ms, to_ms


def _stable_prometheus_end_sec(start_sec: int, requested_end_sec: int, step: int) -> int:
    """Avoid querying future or just-scraped Prometheus points.

    rate(...[1m]) is unstable at the live edge. Keep at least one minute or one
    step behind current time while preserving a valid query range.
    """
    stable_lag = max(60, step)
    stable_end = int(time.time()) - stable_lag
    capped_end = min(requested_end_sec, stable_end)
    return max(start_sec + step, capped_end)


async def _resource_metric_time_range(
    db: AsyncSession,
    report: Report,
    from_offset: int,
    to_offset: int,
) -> tuple[int, int]:
    run = (
        await db.execute(select(ExecutionRun).where(ExecutionRun.report_id == report.id))
    ).scalar_one_or_none()
    if run is not None and (run.started_at or run.finished_at):
        start = run.started_at or report.create_time
        end = run.finished_at or run.started_at or report.modify_time or report.create_time
        if start and end and end < start:
            end = start
        from_ms = _to_epoch_ms(start - timedelta(minutes=from_offset) if start else None)
        to_ms = _to_epoch_ms(end + timedelta(minutes=to_offset) if end else None)
        return from_ms, to_ms
    return _report_metric_time_range(report, from_offset, to_offset)


async def get_resource_metrics(
    db: AsyncSession,
    id: int,
    step: int | None = None,
    instance: str | None = None,
) -> ResourceMetricsVO:
    report = await crud.get_by_id(db, id)
    if report is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    base_url = await config_service.get_value_or_default(db, "PROMETHEUS_BASE_URL", "")
    if not base_url:
        raise MysteriousException(Codes.FAIL, message="未配置 PROMETHEUS_BASE_URL，无法查询平台内资源曲线")

    resolved_instance = (instance or "").strip() or await _resolve_prometheus_instance(db, report)
    if not resolved_instance:
        raise MysteriousException(Codes.FAIL, message="当前报告未匹配到 Prometheus instance")

    instance_label = await config_service.get_value_or_default(db, "PROMETHEUS_INSTANCE_LABEL", "instance")
    timeout = max(1, _parse_offset_minutes(
        await config_service.get_value_or_default(db, "PROMETHEUS_TIMEOUT_SECONDS", "8"),
        8,
    ))
    from_offset = _parse_offset_minutes(
        await config_service.get_value_or_default(
            db,
            "PROMETHEUS_FROM_OFFSET_MINUTES",
            await config_service.get_value_or_default(db, "GRAFANA_FROM_OFFSET_MINUTES", "15"),
        ),
        15,
    )
    to_offset = _parse_offset_minutes(
        await config_service.get_value_or_default(
            db,
            "PROMETHEUS_TO_OFFSET_MINUTES",
            await config_service.get_value_or_default(db, "GRAFANA_TO_OFFSET_MINUTES", "15"),
        ),
        15,
    )
    if step is None:
        step = _parse_offset_minutes(
            await config_service.get_value_or_default(db, "PROMETHEUS_STEP_SECONDS", "30"),
            30,
        )
    step = max(5, int(step or 30))
    from_ms, to_ms = await _resource_metric_time_range(db, report, from_offset, to_offset)
    start_sec = from_ms // 1000
    end_sec = _stable_prometheus_end_sec(start_sec, to_ms // 1000, step)

    queries = _prometheus_resource_queries(resolved_instance, instance_label or "instance")
    series: dict[str, list[dict]] = {}
    first_error: MysteriousException | None = None
    failed_count = 0
    query_results = await asyncio.gather(
        *(
            _query_prometheus_range(base_url, queries[name], start_sec, end_sec, step, timeout)
            for name in DEFAULT_PROMETHEUS_RESOURCE_METRICS
        ),
        return_exceptions=True,
    )
    for name, result in zip(DEFAULT_PROMETHEUS_RESOURCE_METRICS, query_results, strict=True):
        if isinstance(result, MysteriousException):
            failed_count += 1
            if first_error is None:
                first_error = result
            log.info("Prometheus 指标查询失败: report_id=%s metric=%s", id, name)
            series[name] = []
            continue
        if isinstance(result, Exception):
            raise result
        series[name] = result
    if first_error is not None and failed_count == len(DEFAULT_PROMETHEUS_RESOURCE_METRICS):
        raise first_error

    return ResourceMetricsVO(
        report_id=id,
        instance=resolved_instance,
        from_ms=from_ms,
        to_ms=to_ms,
        step=step,
        series=series,
    )


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


def _find_statistics_file(report_dir: str) -> str | None:
    if not report_dir:
        return None
    candidates = [
        os.path.join(report_dir, "statistics.json"),
        os.path.join(report_dir, "data", "statistics.json"),
    ]
    report_root = _find_report_root(report_dir)
    if report_root:
        candidates.append(os.path.join(report_root, "data", "statistics.json"))
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


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


def _load_run_meta(report_dir: str) -> dict:
    report_root = _find_report_root(report_dir)
    if not report_root:
        return {}
    meta_path = os.path.join(report_root, "run_meta.json")
    try:
        with open(meta_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _meta_int(meta: dict, key: str, default: int = 0) -> int:
    try:
        return int(meta.get(key) or default)
    except (TypeError, ValueError):
        return default


def _normalize_threads(raw_threads: int, run_meta: dict | None) -> int:
    """分布式 JTL 的 allThreads 通常是单台压力机线程数，这里换算成总线程数。"""
    if not run_meta:
        return raw_threads
    slave_count = _meta_int(run_meta, "slave_count", 1)
    per_slave_threads = _meta_int(run_meta, "per_slave_threads", 0)
    total_threads = _meta_int(run_meta, "total_threads", 0)
    if slave_count <= 1:
        return raw_threads

    # 如果 JTL 已经给出全局线程数，不再重复乘；否则按实际压力机数换算。
    threads = raw_threads
    if per_slave_threads <= 0 or raw_threads <= per_slave_threads:
        threads = raw_threads * slave_count
    if total_threads > 0:
        threads = min(threads, total_threads)
    return threads


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


def _parse_jtl_metrics(
    jtl_path: str,
    window_sec: int = 5,
    run_meta: dict | None = None,
) -> list[dict]:
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
                    raw_threads = int(row.get("allThreads", "0") or row.get("grpThreads", "0"))
                    threads = _normalize_threads(raw_threads, run_meta)
                    thread_name = (row.get("threadName") or "").strip()
                except (ValueError, TypeError):
                    continue
                if ts <= 0:
                    continue
                key = ts // window_ms * window_ms
                bucket = buckets.setdefault(
                    key,
                    {"elapsed": [], "fail": 0, "threads": 0, "thread_names": set(), "count": 0},
                )
                bucket["elapsed"].append(elapsed)
                if not success:
                    bucket["fail"] += 1
                bucket["threads"] = max(bucket["threads"], threads)
                if thread_name:
                    bucket["thread_names"].add(thread_name)
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
        active_threads = max(b["threads"], len(b.get("thread_names") or set()))
        total_threads = _meta_int(run_meta or {}, "total_threads", 0)
        if total_threads > 0:
            active_threads = min(active_threads, total_threads)
        results.append(
            {
                "bucket_start_ms": key,
                "timestamp": dt.strftime("%H:%M:%S"),
                "qps": round(count / window_sec, 1),
                "avg_rt": round(sum(elapsed_sorted) / count, 1),
                "p95_rt": round(_percentile(elapsed_sorted, 95), 1),
                "p99_rt": round(_percentile(elapsed_sorted, 99), 1),
                "error_rate": round(b["fail"] / count * 100, 2),
                "threads": active_threads,
                "sample_count": count,
                "fail_count": b["fail"],
            }
        )
    tps_peak = max((item["qps"] for item in results), default=0.0)
    for item in results:
        item["tps_peak"] = tps_peak
    return results


def _snapshot_to_metric(row: ReportMetricSnapshot) -> dict:
    return {
        "bucket_start_ms": row.bucket_start_ms,
        "timestamp": row.timestamp,
        "qps": row.qps,
        "avg_rt": row.avg_rt,
        "p95_rt": row.p95_rt,
        "p99_rt": row.p99_rt,
        "error_rate": row.error_rate,
        "threads": row.threads,
        "sample_count": row.sample_count,
        "fail_count": row.fail_count,
        "tps_peak": row.tps_peak,
    }


async def _list_metric_snapshots(
    db: AsyncSession,
    report_id: int,
    window_sec: int,
) -> list[dict]:
    stmt = (
        select(ReportMetricSnapshot)
        .where(
            ReportMetricSnapshot.report_id == report_id,
            ReportMetricSnapshot.window_sec == window_sec,
        )
        .order_by(ReportMetricSnapshot.bucket_start_ms.asc())
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return [_snapshot_to_metric(row) for row in rows]


async def _save_metric_snapshots(
    db: AsyncSession,
    report_id: int,
    window_sec: int,
    metrics: list[dict],
) -> None:
    now = datetime.now(SHANGHAI).replace(tzinfo=None)
    for item in metrics:
        values = {
            "report_id": report_id,
            "window_sec": window_sec,
            "bucket_start_ms": int(item.get("bucket_start_ms") or 0),
            "timestamp": str(item.get("timestamp") or ""),
            "qps": float(item.get("qps") or 0),
            "avg_rt": float(item.get("avg_rt") or 0),
            "p95_rt": float(item.get("p95_rt") or 0),
            "p99_rt": float(item.get("p99_rt") or 0),
            "error_rate": float(item.get("error_rate") or 0),
            "threads": int(item.get("threads") or 0),
            "sample_count": int(item.get("sample_count") or 0),
            "fail_count": int(item.get("fail_count") or 0),
            "tps_peak": float(item.get("tps_peak") or 0),
            "creator": "system",
            "creator_id": "0",
            "modifier": "system",
            "modifier_id": "0",
            "create_time": now,
            "modify_time": now,
        }
        await db.execute(_metric_snapshot_upsert_stmt(db, values))
    await db.commit()


def _metric_snapshot_upsert_stmt(db: AsyncSession, values: dict):
    update_values = {
        "timestamp": values["timestamp"],
        "qps": values["qps"],
        "avg_rt": values["avg_rt"],
        "p95_rt": values["p95_rt"],
        "p99_rt": values["p99_rt"],
        "error_rate": values["error_rate"],
        "threads": values["threads"],
        "sample_count": values["sample_count"],
        "fail_count": values["fail_count"],
        "tps_peak": values["tps_peak"],
        "modifier": values["modifier"],
        "modifier_id": values["modifier_id"],
        "modify_time": values["modify_time"],
    }
    dialect = db.get_bind().dialect.name
    if dialect == "mysql":
        stmt = mysql_insert(ReportMetricSnapshot).values(**values)
        return stmt.on_duplicate_key_update(**update_values)
    if dialect == "sqlite":
        stmt = sqlite_insert(ReportMetricSnapshot).values(**values)
        return stmt.on_conflict_do_update(
            index_elements=["report_id", "window_sec", "bucket_start_ms"],
            set_=update_values,
        )

    return ReportMetricSnapshot.__table__.insert().values(**values)


def _round2(value: float) -> float:
    return round(float(value or 0), 2)


def _to_int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _to_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _normalize_transaction_rows(rows: list[dict]) -> list[dict]:
    total_samples = 0
    for row in rows:
        if str(row.get("name") or "") == "Total":
            total_samples = int(row.get("samples") or 0)
            break
    if total_samples <= 0:
        total_samples = sum(int(row.get("samples") or 0) for row in rows)

    normalized: list[dict] = []
    for row in rows:
        samples = int(row.get("samples") or 0)
        failed = int(row.get("failed") or 0)
        success = max(0, int(row.get("success") if row.get("success") is not None else samples - failed))
        normalized.append(
            {
                "name": str(row.get("name") or ""),
                "samples": samples,
                "success": success,
                "failed": failed,
                "success_rate": _round2((success / samples * 100) if samples else 0),
                "tps": _round2(_to_float(row.get("tps"))),
                "avg_rt": _round2(_to_float(row.get("avg_rt"))),
                "max_rt": _round2(_to_float(row.get("max_rt"))),
                "min_rt": _round2(_to_float(row.get("min_rt"))),
                "ratio": _round2((samples / total_samples * 100) if total_samples else 0),
            }
        )
    return sorted(normalized, key=lambda item: (0 if item["name"] == "Total" else 1, item["name"]))


def _parse_statistics_json_transactions(statistics_path: str) -> list[dict]:
    try:
        with open(statistics_path, encoding="utf-8", errors="replace") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("读取 JMeter statistics.json 失败: %s", e)
        return []
    if not isinstance(payload, dict):
        return []

    rows: list[dict] = []
    for key, value in payload.items():
        if not isinstance(value, dict):
            continue
        name = str(value.get("transaction") or key or "").strip()
        if not name:
            continue
        samples = _to_int(value.get("sampleCount"))
        failed = _to_int(value.get("errorCount"))
        rows.append(
            {
                "name": name,
                "samples": samples,
                "success": max(0, samples - failed),
                "failed": failed,
                "tps": value.get("throughput"),
                "avg_rt": value.get("meanResTime"),
                "max_rt": value.get("maxResTime"),
                "min_rt": value.get("minResTime"),
            }
        )
    return _normalize_transaction_rows(rows)


def _parse_jtl_transaction_stats(jtl_path: str) -> list[dict]:
    import csv

    groups: dict[str, dict] = {}
    total = {
        "name": "Total",
        "elapsed": [],
        "failed": 0,
        "samples": 0,
        "start_ts": 0,
        "end_ts": 0,
    }

    def _touch_duration(target: dict, ts: int, elapsed: int) -> None:
        if target["start_ts"] <= 0 or ts < target["start_ts"]:
            target["start_ts"] = ts
        end_ts = ts + max(0, elapsed)
        if end_ts > target["end_ts"]:
            target["end_ts"] = end_ts

    try:
        with open(jtl_path, encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    ts = int(row.get("timeStamp", "0"))
                    elapsed = int(row.get("elapsed", "0"))
                    success = str(row.get("success", "true")).lower() == "true"
                except (ValueError, TypeError):
                    continue
                if ts <= 0:
                    continue
                label = str(row.get("label") or row.get("Label") or "未命名事务").strip() or "未命名事务"
                group = groups.setdefault(
                    label,
                    {
                        "name": label,
                        "elapsed": [],
                        "failed": 0,
                        "samples": 0,
                        "start_ts": 0,
                        "end_ts": 0,
                    },
                )
                for target in (group, total):
                    target["elapsed"].append(elapsed)
                    target["samples"] += 1
                    if not success:
                        target["failed"] += 1
                    _touch_duration(target, ts, elapsed)
    except OSError as e:
        log.warning("读取 JTL 事务统计失败: %s", e)
        return []

    def _to_row(target: dict) -> dict:
        elapsed_values = target["elapsed"]
        samples = int(target["samples"] or 0)
        duration_sec = max((int(target["end_ts"] or 0) - int(target["start_ts"] or 0)) / 1000.0, 1.0)
        return {
            "name": target["name"],
            "samples": samples,
            "failed": int(target["failed"] or 0),
            "tps": samples / duration_sec if samples else 0,
            "avg_rt": (sum(elapsed_values) / samples) if samples else 0,
            "max_rt": max(elapsed_values) if elapsed_values else 0,
            "min_rt": min(elapsed_values) if elapsed_values else 0,
        }

    rows = [_to_row(total)]
    rows.extend(_to_row(groups[name]) for name in sorted(groups))
    return _normalize_transaction_rows(rows)


def _transaction_snapshot_to_vo(row: ReportTransactionSnapshot) -> TransactionStatsVO:
    return TransactionStatsVO(
        name=row.transaction_name,
        samples=row.samples,
        success=row.success_count,
        failed=row.fail_count,
        success_rate=row.success_rate,
        tps=row.tps,
        avg_rt=row.avg_rt,
        max_rt=row.max_rt,
        min_rt=row.min_rt,
        ratio=row.ratio,
    )


async def _list_transaction_snapshots(db: AsyncSession, report_id: int) -> list[TransactionStatsVO]:
    rows = list(
        (
            await db.execute(
                select(ReportTransactionSnapshot)
                .where(ReportTransactionSnapshot.report_id == report_id)
                .order_by(
                    (ReportTransactionSnapshot.transaction_name == "Total").desc(),
                    ReportTransactionSnapshot.transaction_name.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    return [_transaction_snapshot_to_vo(row) for row in rows]


async def _save_transaction_snapshots(db: AsyncSession, report_id: int, rows: list[dict]) -> None:
    now = datetime.now(SHANGHAI).replace(tzinfo=None)
    for item in rows:
        values = {
            "report_id": report_id,
            "transaction_name": str(item.get("name") or ""),
            "samples": int(item.get("samples") or 0),
            "success_count": int(item.get("success") or 0),
            "fail_count": int(item.get("failed") or 0),
            "success_rate": float(item.get("success_rate") or 0),
            "tps": float(item.get("tps") or 0),
            "avg_rt": float(item.get("avg_rt") or 0),
            "max_rt": float(item.get("max_rt") or 0),
            "min_rt": float(item.get("min_rt") or 0),
            "ratio": float(item.get("ratio") or 0),
            "creator": "system",
            "creator_id": "0",
            "modifier": "system",
            "modifier_id": "0",
            "create_time": now,
            "modify_time": now,
        }
        await db.execute(_transaction_snapshot_upsert_stmt(db, values))
    await db.commit()


def _transaction_snapshot_upsert_stmt(db: AsyncSession, values: dict):
    update_values = {
        "samples": values["samples"],
        "success_count": values["success_count"],
        "fail_count": values["fail_count"],
        "success_rate": values["success_rate"],
        "tps": values["tps"],
        "avg_rt": values["avg_rt"],
        "max_rt": values["max_rt"],
        "min_rt": values["min_rt"],
        "ratio": values["ratio"],
        "modifier": values["modifier"],
        "modifier_id": values["modifier_id"],
        "modify_time": values["modify_time"],
    }
    dialect = db.get_bind().dialect.name
    if dialect == "mysql":
        stmt = mysql_insert(ReportTransactionSnapshot).values(**values)
        return stmt.on_duplicate_key_update(**update_values)
    if dialect == "sqlite":
        stmt = sqlite_insert(ReportTransactionSnapshot).values(**values)
        return stmt.on_conflict_do_update(
            index_elements=["report_id", "transaction_name"],
            set_=update_values,
        )
    return ReportTransactionSnapshot.__table__.insert().values(**values)


async def generate_transaction_snapshots_for_report(db: AsyncSession, report_id: int) -> int:
    rpt = await crud.get_by_id(db, report_id)
    if rpt is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    rows: list[dict] = []
    statistics_path = _find_statistics_file(rpt.report_dir)
    if statistics_path:
        rows = await asyncio.to_thread(_parse_statistics_json_transactions, statistics_path)
    if not rows:
        jtl_path = _find_jtl_file(rpt.report_dir)
        if jtl_path:
            rows = await asyncio.to_thread(_parse_jtl_transaction_stats, jtl_path)
    if not rows:
        return 0
    await _save_transaction_snapshots(db, report_id, rows)
    return len(rows)


def schedule_transaction_snapshot_generation(report_id: int) -> bool:
    if report_id in _transaction_snapshot_tasks:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _transaction_snapshot_tasks.add(report_id)
    task = loop.create_task(
        _generate_transaction_snapshots_background(report_id),
        name=f"report-transaction-snapshot-{report_id}",
    )
    task.add_done_callback(lambda done_task: _on_transaction_snapshot_task_done(report_id, done_task))
    return True


async def _generate_transaction_snapshots_background(report_id: int) -> None:
    async with session_module.AsyncSessionLocal() as db:
        count = await generate_transaction_snapshots_for_report(db, report_id)
    log.info("报告交易统计快照生成完成: report_id=%s rows=%s", report_id, count)


def _on_transaction_snapshot_task_done(report_id: int, task: asyncio.Task) -> None:
    _transaction_snapshot_tasks.discard(report_id)
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        log.warning("报告交易统计快照生成任务被取消: report_id=%s", report_id)
        return
    if exc is not None:
        log.warning("报告交易统计快照生成失败: report_id=%s", report_id, exc_info=(type(exc), exc, exc.__traceback__))


async def get_transaction_stats(db: AsyncSession, report_id: int) -> list[TransactionStatsVO]:
    rpt = await crud.get_by_id(db, report_id)
    if rpt is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    snapshots = await _list_transaction_snapshots(db, report_id)
    if snapshots:
        return snapshots

    await generate_transaction_snapshots_for_report(db, report_id)
    return await _list_transaction_snapshots(db, report_id)


def _parse_jtl_transaction_metrics(
    jtl_path: str,
    window_sec: int = 60,
    run_meta: dict | None = None,
) -> list[dict]:
    import csv

    window_sec = max(1, int(window_sec or 60))
    window_ms = window_sec * 1000
    buckets: dict[tuple[str, int], dict] = {}

    try:
        with open(jtl_path, encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    ts = int(row.get("timeStamp", "0"))
                    elapsed = int(row.get("elapsed", "0"))
                    success = str(row.get("success", "true")).lower() == "true"
                    raw_group_threads = _to_int(row.get("grpThreads"))
                    raw_all_threads = _to_int(row.get("allThreads"))
                    raw_threads = raw_group_threads if raw_group_threads > 0 else raw_all_threads
                    threads = _normalize_threads(raw_threads, run_meta)
                except (ValueError, TypeError):
                    continue
                if ts <= 0:
                    continue
                label = str(row.get("label") or row.get("Label") or "未命名事务").strip() or "未命名事务"
                bucket_start = ts // window_ms * window_ms
                bucket = buckets.setdefault(
                    (label, bucket_start),
                    {
                        "transaction_name": label,
                        "bucket_start_ms": bucket_start,
                        "elapsed": [],
                        "fail_count": 0,
                        "sample_count": 0,
                        "thread_names": set(),
                        "threads": 0,
                    },
                )
                bucket["elapsed"].append(elapsed)
                bucket["sample_count"] += 1
                bucket["threads"] = max(bucket["threads"], threads)
                thread_name = str(row.get("threadName") or "").strip()
                if thread_name:
                    bucket["thread_names"].add(thread_name)
                if not success:
                    bucket["fail_count"] += 1
    except OSError as e:
        log.warning("读取 JTL 交易曲线失败: %s", e)
        return []

    results: list[dict] = []
    for _key, bucket in sorted(buckets.items(), key=lambda item: (item[0][0], item[0][1])):
        count = int(bucket["sample_count"] or 0)
        if count <= 0:
            continue
        elapsed_sorted = sorted(bucket["elapsed"])
        fail_count = int(bucket["fail_count"] or 0)
        dt = datetime.fromtimestamp(bucket["bucket_start_ms"] / 1000.0)
        active_threads = max(
            int(bucket.get("threads") or 0),
            len(bucket.get("thread_names") or set()),
        )
        total_threads = _meta_int(run_meta or {}, "total_threads", 0)
        if total_threads > 0:
            active_threads = min(active_threads, total_threads)
        results.append(
            {
                "transaction_name": bucket["transaction_name"],
                "bucket_start_ms": bucket["bucket_start_ms"],
                "timestamp": dt.strftime("%H:%M:%S"),
                "qps": round(count / window_sec, 2),
                "avg_rt": round(sum(elapsed_sorted) / count, 1),
                "p95_rt": round(_percentile(elapsed_sorted, 95), 1),
                "p99_rt": round(_percentile(elapsed_sorted, 99), 1),
                "error_rate": round(fail_count / count * 100, 2),
                "sample_count": count,
                "fail_count": fail_count,
                "active_threads": active_threads,
            }
        )
    return results


def _transaction_metric_snapshot_to_point(row: ReportTransactionMetricSnapshot) -> TransactionMetricPointVO:
    return TransactionMetricPointVO(
        timestamp=row.timestamp,
        qps=row.qps,
        avg_rt=row.avg_rt,
        p95_rt=row.p95_rt,
        p99_rt=row.p99_rt,
        error_rate=row.error_rate,
        sample_count=row.sample_count,
        fail_count=row.fail_count,
        active_threads=row.active_threads,
    )


async def _list_transaction_metric_snapshots(
    db: AsyncSession,
    report_id: int,
    window_sec: int,
) -> TransactionMetricsVO:
    rows = list(
        (
            await db.execute(
                select(ReportTransactionMetricSnapshot)
                .where(
                    ReportTransactionMetricSnapshot.report_id == report_id,
                    ReportTransactionMetricSnapshot.window_sec == window_sec,
                )
                .order_by(
                    ReportTransactionMetricSnapshot.transaction_name.asc(),
                    ReportTransactionMetricSnapshot.bucket_start_ms.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    sample_totals: dict[str, int] = {}
    series: dict[str, list[TransactionMetricPointVO]] = {}
    for row in rows:
        sample_totals[row.transaction_name] = sample_totals.get(row.transaction_name, 0) + row.sample_count
        series.setdefault(row.transaction_name, []).append(_transaction_metric_snapshot_to_point(row))
    transactions = sorted(series, key=lambda name: (-sample_totals.get(name, 0), name))
    ordered_series = {name: series[name] for name in transactions}
    return TransactionMetricsVO(
        report_id=report_id,
        window=window_sec,
        transactions=transactions,
        series=ordered_series,
    )


async def _save_transaction_metric_snapshots(
    db: AsyncSession,
    report_id: int,
    window_sec: int,
    metrics: list[dict],
) -> None:
    now = datetime.now(SHANGHAI).replace(tzinfo=None)
    for item in metrics:
        values = {
            "report_id": report_id,
            "transaction_name": str(item.get("transaction_name") or ""),
            "window_sec": window_sec,
            "bucket_start_ms": int(item.get("bucket_start_ms") or 0),
            "timestamp": str(item.get("timestamp") or ""),
            "qps": float(item.get("qps") or 0),
            "avg_rt": float(item.get("avg_rt") or 0),
            "p95_rt": float(item.get("p95_rt") or 0),
            "p99_rt": float(item.get("p99_rt") or 0),
            "error_rate": float(item.get("error_rate") or 0),
            "sample_count": int(item.get("sample_count") or 0),
            "fail_count": int(item.get("fail_count") or 0),
            "active_threads": int(item.get("active_threads") or 0),
            "creator": "system",
            "creator_id": "0",
            "modifier": "system",
            "modifier_id": TRANSACTION_METRIC_SNAPSHOT_VERSION,
            "create_time": now,
            "modify_time": now,
        }
        await db.execute(_transaction_metric_snapshot_upsert_stmt(db, values))
    await db.commit()


def _transaction_metric_snapshot_upsert_stmt(db: AsyncSession, values: dict):
    update_values = {
        "timestamp": values["timestamp"],
        "qps": values["qps"],
        "avg_rt": values["avg_rt"],
        "p95_rt": values["p95_rt"],
        "p99_rt": values["p99_rt"],
        "error_rate": values["error_rate"],
        "sample_count": values["sample_count"],
        "fail_count": values["fail_count"],
        "active_threads": values["active_threads"],
        "modifier": values["modifier"],
        "modifier_id": values["modifier_id"],
        "modify_time": values["modify_time"],
    }
    dialect = db.get_bind().dialect.name
    if dialect == "mysql":
        stmt = mysql_insert(ReportTransactionMetricSnapshot).values(**values)
        return stmt.on_duplicate_key_update(**update_values)
    if dialect == "sqlite":
        stmt = sqlite_insert(ReportTransactionMetricSnapshot).values(**values)
        return stmt.on_conflict_do_update(
            index_elements=["report_id", "transaction_name", "window_sec", "bucket_start_ms"],
            set_=update_values,
        )
    return ReportTransactionMetricSnapshot.__table__.insert().values(**values)


async def _transaction_metric_snapshots_need_refresh(
    db: AsyncSession,
    report_id: int,
    window_sec: int,
) -> bool:
    rows = list(
        (
            await db.execute(
                select(ReportTransactionMetricSnapshot)
                .where(
                    ReportTransactionMetricSnapshot.report_id == report_id,
                    ReportTransactionMetricSnapshot.window_sec == window_sec,
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return False
    if any(row.modifier_id != TRANSACTION_METRIC_SNAPSHOT_VERSION for row in rows):
        return True
    return all(row.sample_count > 0 and row.active_threads <= 0 for row in rows)


async def generate_transaction_metric_snapshots_for_report(
    db: AsyncSession,
    report_id: int,
    windows: tuple[int, ...] = (60,),
) -> int:
    rpt = await crud.get_by_id(db, report_id)
    if rpt is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)
    jtl_path = _find_jtl_file(rpt.report_dir)
    if not jtl_path:
        return 0

    run_meta = _load_run_meta(rpt.report_dir)
    total = 0
    for window_sec in sorted({max(1, int(w)) for w in windows}):
        metrics = await asyncio.to_thread(_parse_jtl_transaction_metrics, jtl_path, window_sec, run_meta)
        await _save_transaction_metric_snapshots(db, report_id, window_sec, metrics)
        total += len(metrics)
    return total


def schedule_transaction_metric_snapshot_generation(
    report_id: int,
    windows: tuple[int, ...] = (60,),
) -> bool:
    normalized_windows = tuple(sorted({int(w) for w in windows if int(w) > 0}))
    if not normalized_windows:
        return False
    key = (report_id, normalized_windows)
    if key in _transaction_metric_snapshot_tasks:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _transaction_metric_snapshot_tasks.add(key)
    task = loop.create_task(
        _generate_transaction_metric_snapshots_background(report_id, normalized_windows, key),
        name=f"report-transaction-metric-snapshot-{report_id}",
    )
    task.add_done_callback(lambda done_task: _on_transaction_metric_snapshot_task_done(report_id, key, done_task))
    return True


async def _generate_transaction_metric_snapshots_background(
    report_id: int,
    windows: tuple[int, ...],
    key: tuple[int, tuple[int, ...]],
) -> None:
    async with session_module.AsyncSessionLocal() as db:
        count = await generate_transaction_metric_snapshots_for_report(db, report_id, windows)
    log.info("报告交易曲线快照生成完成: report_id=%s windows=%s rows=%s", report_id, windows, count)


def _on_transaction_metric_snapshot_task_done(
    report_id: int,
    key: tuple[int, tuple[int, ...]],
    task: asyncio.Task,
) -> None:
    _transaction_metric_snapshot_tasks.discard(key)
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        log.warning("报告交易曲线快照生成任务被取消: report_id=%s", report_id)
        return
    if exc is not None:
        log.warning("报告交易曲线快照生成失败: report_id=%s", report_id, exc_info=(type(exc), exc, exc.__traceback__))


async def get_transaction_metrics(db: AsyncSession, report_id: int, window_sec: int = 60) -> TransactionMetricsVO:
    rpt = await crud.get_by_id(db, report_id)
    if rpt is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)
    window_sec = max(1, int(window_sec or 60))

    snapshots = await _list_transaction_metric_snapshots(db, report_id, window_sec)
    if snapshots.transactions:
        if not await _transaction_metric_snapshots_need_refresh(db, report_id, window_sec):
            return snapshots
        log.info("报告交易曲线快照版本过旧，重新解析 JTL 回填: report_id=%s window=%s", report_id, window_sec)
        await generate_transaction_metric_snapshots_for_report(db, report_id, (window_sec,))
        snapshots = await _list_transaction_metric_snapshots(db, report_id, window_sec)
        return snapshots

    await generate_transaction_metric_snapshots_for_report(db, report_id, (window_sec,))
    return await _list_transaction_metric_snapshots(db, report_id, window_sec)


async def get_transaction_trend(db: AsyncSession, report_id: int, window_sec: int = 60) -> TransactionTrendVO:
    transaction_metrics = await get_transaction_metrics(db, report_id, window_sec)
    metric_window = max(1, int(window_sec or 60))
    overall_metrics = await _list_metric_snapshots(db, report_id, metric_window)
    if not overall_metrics:
        await generate_metric_snapshots_for_report(db, report_id, (metric_window,))
        overall_metrics = await _list_metric_snapshots(db, report_id, metric_window)

    timestamps = sorted({
        point.timestamp
        for points in transaction_metrics.series.values()
        for point in points
    } | {str(item.get("timestamp") or "") for item in overall_metrics if item.get("timestamp")})

    transaction_tps: dict[str, list[TrendPointVO]] = {}
    avg_rt: dict[str, list[TrendPointVO]] = {}
    active_threads: dict[str, list[TrendPointVO]] = {}
    total_samples_by_ts: dict[str, int] = {}
    for name, points in transaction_metrics.series.items():
        transaction_tps[name] = [
            TrendPointVO(timestamp=point.timestamp, value=point.qps)
            for point in points
        ]
        for point in points:
            total_samples_by_ts[point.timestamp] = total_samples_by_ts.get(point.timestamp, 0) + point.sample_count
        avg_rt[name] = [
            TrendPointVO(timestamp=point.timestamp, value=point.avg_rt)
            for point in points
        ]
        active_threads[name] = [
            TrendPointVO(timestamp=point.timestamp, value=float(point.active_threads))
            for point in points
        ]

    if total_samples_by_ts:
        total_tps = [
            TrendPointVO(timestamp=timestamp, value=round(total_samples_by_ts.get(timestamp, 0) / metric_window, 2))
            for timestamp in timestamps
        ]
    else:
        total_tps = [
            TrendPointVO(timestamp=str(item.get("timestamp") or ""), value=float(item.get("qps") or 0))
            for item in overall_metrics
        ]

    return TransactionTrendVO(
        report_id=report_id,
        window=metric_window,
        timestamps=timestamps,
        transactions=transaction_metrics.transactions,
        total_tps=total_tps,
        transaction_tps=transaction_tps,
        avg_rt=avg_rt,
        active_threads=active_threads,
    )


async def generate_metric_snapshots_for_report(
    db: AsyncSession,
    report_id: int,
    windows: tuple[int, ...] = DEFAULT_METRIC_WINDOWS,
) -> int:
    rpt = await crud.get_by_id(db, report_id)
    if rpt is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)
    jtl_path = _find_jtl_file(rpt.report_dir)
    if not jtl_path:
        return 0

    total = 0
    run_meta = _load_run_meta(rpt.report_dir)
    for window_sec in windows:
        metrics = await asyncio.to_thread(_parse_jtl_metrics, jtl_path, window_sec, run_meta)
        await _save_metric_snapshots(db, report_id, window_sec, metrics)
        total += len(metrics)
    return total


def schedule_metric_snapshot_generation(
    report_id: int,
    windows: tuple[int, ...] = DEFAULT_METRIC_WINDOWS,
) -> bool:
    """后台生成报告指标快照。

    请求曲线接口时只查快照表；如果快照缺失，用这个函数异步补算，避免大 JTL
    在接口请求路径里同步解析。
    """
    normalized_windows = tuple(sorted({int(w) for w in windows if int(w) > 0}))
    if not normalized_windows:
        return False
    key = (report_id, normalized_windows)
    if key in _metric_snapshot_tasks:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _metric_snapshot_tasks.add(key)
    task = loop.create_task(
        _generate_metric_snapshots_background(report_id, normalized_windows, key),
        name=f"report-metric-snapshot-{report_id}",
    )
    task.add_done_callback(lambda done_task: _on_metric_snapshot_task_done(report_id, key, done_task))
    return True


def _maybe_refresh_running_metric_snapshot(rpt: Report, window_sec: int) -> None:
    if rpt.status != TestCaseStatus.RUN_ING.value:
        return
    key = (rpt.id, window_sec)
    now = time.monotonic()
    last_refresh = _metric_snapshot_last_refresh.get(key, 0.0)
    if now - last_refresh < _METRIC_RUNNING_REFRESH_INTERVAL_SECONDS:
        return
    _metric_snapshot_last_refresh[key] = now
    schedule_metric_snapshot_generation(rpt.id, (window_sec,))


async def _generate_metric_snapshots_background(
    report_id: int,
    windows: tuple[int, ...],
    key: tuple[int, tuple[int, ...]],
) -> None:
    async with session_module.AsyncSessionLocal() as db:
        count = await generate_metric_snapshots_for_report(db, report_id, windows)
    log.info("报告指标快照生成完成: report_id=%s windows=%s rows=%s", report_id, windows, count)


def _on_metric_snapshot_task_done(
    report_id: int,
    key: tuple[int, tuple[int, ...]],
    task: asyncio.Task,
) -> None:
    _metric_snapshot_tasks.discard(key)
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        log.warning("报告指标快照生成任务被取消: report_id=%s", report_id)
        return
    if exc is not None:
        log.warning("报告指标快照生成失败: report_id=%s", report_id, exc_info=(type(exc), exc, exc.__traceback__))


async def get_jtl_metrics(
    db: AsyncSession, report_id: int, window_sec: int = 5
) -> list[dict]:
    """读取指定报告指标快照。缺快照时触发后台补算，不在请求路径解析 JTL。"""
    rpt = await crud.get_by_id(db, report_id)
    if rpt is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    snapshots = await _list_metric_snapshots(db, report_id, window_sec)
    if snapshots:
        _maybe_refresh_running_metric_snapshot(rpt, window_sec)
        return snapshots

    if rpt.status == TestCaseStatus.RUN_ING.value:
        await generate_metric_snapshots_for_report(db, report_id, (window_sec,))
        return await _list_metric_snapshots(db, report_id, window_sec)

    schedule_metric_snapshot_generation(report_id, (window_sec,))
    return []


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
