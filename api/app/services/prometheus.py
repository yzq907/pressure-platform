"""Prometheus and Grafana resource metric helpers for reports."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import Codes
from app.core.exceptions import MysteriousException
from app.crud import report as crud, testcase as testcase_crud
from app.models.execution_run import ExecutionRun
from app.models.report import Report
from app.schemas.report import ResourceMetricsVO, ResourceTargetVO
from app.services import config as config_service

log = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_PROMETHEUS_RESOURCE_METRICS = (
    "cpu",
    "memory",
    "load",
    "networkIn",
    "networkOut",
    "diskRead",
    "diskWrite",
    "diskUtil",
    "gc",
)
DISK_PROMETHEUS_RESOURCE_METRICS = ("diskRead", "diskWrite", "diskUtil")
RESOURCE_METRICS_CACHE_TTL_SECONDS = 45
_RESOURCE_METRICS_CACHE_MAX_SIZE = 256
_RESOURCE_METRICS_CACHE: dict[tuple, tuple[float, ResourceMetricsVO]] = {}

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

        group_name = str(group_key).strip()
        targets: list[ResourceTargetVO] = []
        for index, item in enumerate(raw_targets, start=1):
            if isinstance(item, str):
                instance = item.strip()
                if not instance:
                    continue
                target_name = group_name if len(raw_targets) == 1 else f"{group_name}-{index}"
                targets.append(
                    ResourceTargetVO(
                        name=target_name,
                        role="相关服务",
                        service=group_name,
                        instance=instance,
                    )
                )
                continue

            if isinstance(item, dict):
                instance = str(item.get("instance") or "").strip()
                if not instance:
                    continue
                service = str(item.get("service") or item.get("name") or group_name).strip()
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
            groups[group_name] = targets
    return groups

def _quote_prom_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')

def _prometheus_resource_queries(
    instance: str,
    instance_label: str,
    *,
    exclude_device_mapper: bool = True,
) -> dict[str, str]:
    selector = f'{instance_label}="{_quote_prom_label(instance)}"'
    disk_exclude_pattern = (
        "^(dm-|loop|ram|fd|sr).*" if exclude_device_mapper else "^(loop|ram|fd|sr).*"
    )
    disk_selector = f'{selector},device!~"{disk_exclude_pattern}"'
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
        "diskRead": f"sum(irate(node_disk_read_bytes_total{{{disk_selector}}}[1m]))",
        "diskWrite": f"sum(irate(node_disk_written_bytes_total{{{disk_selector}}}[1m]))",
        "diskUtil": (
            "max(irate(node_disk_io_time_seconds_total{"
            f"{disk_selector}"
            "}[1m])) * 100"
        ),
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
    force_refresh: bool = False,
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
    cache_key = (
        id,
        base_url.rstrip("/"),
        resolved_instance,
        instance_label or "instance",
        start_sec,
        to_ms // 1000,
        step,
    )
    now = time.monotonic()
    cached = _RESOURCE_METRICS_CACHE.get(cache_key)
    if not force_refresh and cached is not None:
        expires_at, cached_metrics = cached
        if expires_at > now:
            return cached_metrics
        _RESOURCE_METRICS_CACHE.pop(cache_key, None)

    queries = _prometheus_resource_queries(resolved_instance, instance_label or "instance")
    fallback_queries = _prometheus_resource_queries(
        resolved_instance,
        instance_label or "instance",
        exclude_device_mapper=False,
    )
    series: dict[str, list[dict]] = {}
    first_error: MysteriousException | None = None
    failed_count = 0
    empty_disk_metrics: list[str] = []
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
        if name in DISK_PROMETHEUS_RESOURCE_METRICS and not result:
            empty_disk_metrics.append(name)
    if first_error is not None and failed_count == len(DEFAULT_PROMETHEUS_RESOURCE_METRICS):
        raise first_error
    if empty_disk_metrics:
        fallback_results = await asyncio.gather(
            *(
                _query_prometheus_range(
                    base_url,
                    fallback_queries[name],
                    start_sec,
                    end_sec,
                    step,
                    timeout,
                )
                for name in empty_disk_metrics
            ),
            return_exceptions=True,
        )
        for name, result in zip(empty_disk_metrics, fallback_results, strict=True):
            if isinstance(result, MysteriousException):
                log.info("Prometheus 磁盘 fallback 查询失败: report_id=%s metric=%s", id, name)
                continue
            if isinstance(result, Exception):
                raise result
            if result:
                series[name] = result

    metrics = ResourceMetricsVO(
        report_id=id,
        instance=resolved_instance,
        from_ms=from_ms,
        to_ms=to_ms,
        step=step,
        series=series,
    )
    if len(_RESOURCE_METRICS_CACHE) >= _RESOURCE_METRICS_CACHE_MAX_SIZE:
        oldest_key = min(_RESOURCE_METRICS_CACHE, key=lambda key: _RESOURCE_METRICS_CACHE[key][0])
        _RESOURCE_METRICS_CACHE.pop(oldest_key, None)
    _RESOURCE_METRICS_CACHE[cache_key] = (now + RESOURCE_METRICS_CACHE_TTL_SECONDS, metrics)
    return metrics
