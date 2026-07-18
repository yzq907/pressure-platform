"""JTL metric parsing and report metric snapshot generation."""

from __future__ import annotations

import asyncio
import contextlib
import csv
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import Codes
from app.core.enums import TestCaseStatus
from app.core.exceptions import MysteriousException
from app.crud import report as crud
from app.db import session as session_module
from app.models.report import Report
from app.models.report_metric_snapshot import ReportMetricSnapshot
from app.services import config as config_service

log = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_METRIC_WINDOWS = (5,)
_metric_snapshot_tasks: set[tuple[int, tuple[int, ...]]] = set()
_running_metric_snapshot_tasks: dict[int, asyncio.Task] = {}
_running_metric_snapshot_stop_events: dict[int, asyncio.Event] = {}
_running_metric_states: dict[tuple[int, int], _IncrementalMetricState] = {}
_DEFAULT_RUNNING_METRIC_REFRESH_SECONDS = 5.0
_RUNNING_METRIC_REFRESH_CONFIG_KEY = "REPORT_RUNNING_METRIC_REFRESH_SECONDS"


@dataclass
class _IncrementalMetricState:
    jtl_path: str
    window_sec: int = 5
    offset: int = 0
    pending_bytes: bytes = b""
    header: list[str] | None = None
    file_identity: tuple[int, int] | None = None
    buckets: dict[int, dict] = field(default_factory=dict)
    dirty_keys: set[int] = field(default_factory=set)
    latest_key: int = 0
    finalized_before_ms: int = 0
    tps_peak: float = 0.0


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


def _new_metric_bucket() -> dict:
    return {"elapsed": [], "fail": 0, "threads": 0, "thread_names": set(), "count": 0}


def _append_metric_row(
    buckets: dict[int, dict],
    row: dict[str, str | None],
    window_sec: int,
    run_meta: dict | None,
) -> int | None:
    try:
        timestamp_raw = row.get("timeStamp")
        elapsed_raw = row.get("elapsed")
        success_raw = row.get("success")
        if timestamp_raw in (None, "") or elapsed_raw in (None, "") or success_raw in (None, ""):
            return None
        timestamp_ms = int(timestamp_raw)
        elapsed = int(elapsed_raw)
        success = str(success_raw).lower() == "true"
        raw_threads = int(row.get("allThreads") or row.get("grpThreads") or "0")
        threads = _normalize_threads(raw_threads, run_meta)
        thread_name = (row.get("threadName") or "").strip()
    except (ValueError, TypeError):
        return None
    if timestamp_ms <= 0:
        return None

    key = timestamp_ms // (window_sec * 1000) * (window_sec * 1000)
    bucket = buckets.setdefault(key, _new_metric_bucket())
    bucket["elapsed"].append(elapsed)
    if not success:
        bucket["fail"] += 1
    bucket["threads"] = max(bucket["threads"], threads)
    if thread_name:
        bucket["thread_names"].add(thread_name)
    bucket["count"] += 1
    return key


def _bucket_to_metric(key: int, bucket: dict, window_sec: int, run_meta: dict | None) -> dict | None:
    count = bucket["count"]
    if count == 0:
        return None
    elapsed_sorted = sorted(bucket["elapsed"])
    active_threads = max(bucket["threads"], len(bucket.get("thread_names") or set()))
    total_threads = _meta_int(run_meta or {}, "total_threads", 0)
    if total_threads > 0:
        active_threads = min(active_threads, total_threads)
    return {
        "bucket_start_ms": key,
        "timestamp": datetime.fromtimestamp(key / 1000.0).strftime("%H:%M:%S"),
        "qps": round(count / window_sec, 1),
        "avg_rt": round(sum(elapsed_sorted) / count, 1),
        "p95_rt": round(_percentile(elapsed_sorted, 95), 1),
        "p99_rt": round(_percentile(elapsed_sorted, 99), 1),
        "error_rate": round(bucket["fail"] / count * 100, 2),
        "threads": active_threads,
        "sample_count": count,
        "fail_count": bucket["fail"],
    }


def _metrics_from_buckets(
    buckets: dict[int, dict],
    window_sec: int,
    run_meta: dict | None,
    keys: set[int] | None = None,
    previous_tps_peak: float = 0.0,
) -> tuple[list[dict], float]:
    selected_keys = sorted(keys if keys is not None else buckets)
    results = [
        metric
        for key in selected_keys
        if (metric := _bucket_to_metric(key, buckets[key], window_sec, run_meta)) is not None
    ]
    tps_peak = max(previous_tps_peak, max((item["qps"] for item in results), default=0.0))
    for item in results:
        item["tps_peak"] = tps_peak
    return results, tps_peak


def _parse_jtl_metrics(
    jtl_path: str,
    window_sec: int = 5,
    run_meta: dict | None = None,
) -> list[dict]:
    """解析 JTL 文件，按时间窗口聚合指标。"""
    buckets: dict[int, dict] = {}

    try:
        with open(jtl_path, encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                _append_metric_row(buckets, row, window_sec, run_meta)
    except OSError as e:
        log.warning("读取 JTL 失败: %s", e)
        return []

    results, _ = _metrics_from_buckets(buckets, window_sec, run_meta)
    return results


def _reset_incremental_metric_state(state: _IncrementalMetricState) -> None:
    state.offset = 0
    state.pending_bytes = b""
    state.header = None
    state.buckets.clear()
    state.dirty_keys.clear()
    state.latest_key = 0
    state.finalized_before_ms = 0
    state.tps_peak = 0.0


def _parse_incremental_jtl_metrics(
    state: _IncrementalMetricState,
    run_meta: dict | None = None,
) -> list[dict]:
    try:
        stat = os.stat(state.jtl_path)
    except OSError:
        return []

    identity = (stat.st_dev, stat.st_ino)
    if state.file_identity is not None and (state.file_identity != identity or stat.st_size < state.offset):
        _reset_incremental_metric_state(state)
    state.file_identity = identity

    try:
        with open(state.jtl_path, "rb") as source:
            source.seek(state.offset)
            chunk = source.read()
            state.offset = source.tell()
    except OSError as exc:
        log.warning("增量读取 JTL 失败: %s", exc)
        return []
    if not chunk:
        return []

    payload = state.pending_bytes + chunk
    parts = payload.split(b"\n")
    state.pending_bytes = parts.pop()
    for raw_line in parts:
        line = raw_line.rstrip(b"\r").decode("utf-8", errors="replace")
        if not line:
            continue
        try:
            values = next(csv.reader([line]))
        except csv.Error:
            continue
        if state.header is None:
            state.header = values
            continue
        if len(values) != len(state.header):
            continue
        row = dict(zip(state.header, values, strict=True))
        try:
            row_key = int(row.get("timeStamp") or 0) // (state.window_sec * 1000) * (state.window_sec * 1000)
        except (TypeError, ValueError):
            continue
        if row_key < state.finalized_before_ms:
            continue
        key = _append_metric_row(
            state.buckets,
            row,
            state.window_sec,
            run_meta,
        )
        if key is not None:
            state.dirty_keys.add(key)
            state.latest_key = max(state.latest_key, key)

    metrics, state.tps_peak = _metrics_from_buckets(
        state.buckets,
        state.window_sec,
        run_meta,
        state.dirty_keys,
        state.tps_peak,
    )
    return metrics


def _ack_incremental_jtl_metrics(state: _IncrementalMetricState, metrics: list[dict]) -> None:
    state.dirty_keys.difference_update(int(item["bucket_start_ms"]) for item in metrics)
    keep_from = state.latest_key - state.window_sec * 1000
    state.finalized_before_ms = max(state.finalized_before_ms, keep_from)
    for key in [bucket_key for bucket_key in state.buckets if bucket_key < keep_from]:
        state.buckets.pop(key, None)


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
    metrics = [_snapshot_to_metric(row) for row in rows]
    tps_peak = max((item["qps"] for item in metrics), default=0.0)
    for item in metrics:
        item["tps_peak"] = tps_peak
    return metrics


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


async def _refresh_running_metric_snapshots(
    report_id: int,
    windows: tuple[int, ...],
    jtl_path: str | None,
) -> tuple[int, bool, float]:
    async with session_module.AsyncSessionLocal() as db:
        rpt = await crud.get_by_id(db, report_id)
        if rpt is None:
            return 0, False, _DEFAULT_RUNNING_METRIC_REFRESH_SECONDS
        refresh_seconds = await _running_metric_refresh_seconds(db)
        if rpt.status != TestCaseStatus.RUN_ING.value:
            return 0, False, refresh_seconds
        resolved_path = jtl_path or _find_jtl_file(rpt.report_dir)
        if not resolved_path:
            return 0, True, refresh_seconds

        run_meta = _load_run_meta(rpt.report_dir)
        total = 0
        for window_sec in windows:
            key = (report_id, window_sec)
            state = _running_metric_states.get(key)
            if state is None or state.jtl_path != resolved_path:
                state = _IncrementalMetricState(resolved_path, window_sec)
                _running_metric_states[key] = state
            metrics = await asyncio.to_thread(_parse_incremental_jtl_metrics, state, run_meta)
            if not metrics:
                continue
            await _save_metric_snapshots(db, report_id, window_sec, metrics)
            _ack_incremental_jtl_metrics(state, metrics)
            total += len(metrics)
        return total, True, refresh_seconds


async def _run_running_metric_snapshot_generation(
    report_id: int,
    windows: tuple[int, ...],
    jtl_path: str | None,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        try:
            count, is_running, refresh_seconds = await _refresh_running_metric_snapshots(
                report_id,
                windows,
                jtl_path,
            )
            if count:
                log.debug("运行中报告指标增量刷新完成: report_id=%s rows=%s", report_id, count)
            if not is_running:
                return
        except Exception:
            log.exception("运行中报告指标增量刷新失败: report_id=%s", report_id)
            refresh_seconds = _DEFAULT_RUNNING_METRIC_REFRESH_SECONDS
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=refresh_seconds)


def _on_running_metric_snapshot_task_done(report_id: int, task: asyncio.Task) -> None:
    if _running_metric_snapshot_tasks.get(report_id) is task:
        _running_metric_snapshot_tasks.pop(report_id, None)
        _running_metric_snapshot_stop_events.pop(report_id, None)
    for key in [state_key for state_key in _running_metric_states if state_key[0] == report_id]:
        _running_metric_states.pop(key, None)
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        log.warning(
            "运行中报告指标跟踪任务失败: report_id=%s",
            report_id,
            exc_info=(type(exc), exc, exc.__traceback__),
        )


def schedule_running_metric_snapshot_generation(
    report_id: int,
    windows: tuple[int, ...] = DEFAULT_METRIC_WINDOWS,
    *,
    jtl_path: str | None = None,
) -> bool:
    normalized_windows = tuple(sorted({int(window) for window in windows if int(window) > 0}))
    if not normalized_windows or report_id in _running_metric_snapshot_tasks:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False

    stop_event = asyncio.Event()
    task = loop.create_task(
        _run_running_metric_snapshot_generation(report_id, normalized_windows, jtl_path, stop_event),
        name=f"report-running-metric-{report_id}",
    )
    _running_metric_snapshot_tasks[report_id] = task
    _running_metric_snapshot_stop_events[report_id] = stop_event
    task.add_done_callback(lambda done_task: _on_running_metric_snapshot_task_done(report_id, done_task))
    return True


async def stop_running_metric_snapshot_generation(report_id: int) -> None:
    task = _running_metric_snapshot_tasks.get(report_id)
    stop_event = _running_metric_snapshot_stop_events.get(report_id)
    if task is not None and stop_event is not None:
        stop_event.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    for key in [state_key for state_key in _running_metric_states if state_key[0] == report_id]:
        _running_metric_states.pop(key, None)


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


def _parse_running_metric_refresh_seconds(raw: str | None) -> float:
    try:
        value = float(raw or _DEFAULT_RUNNING_METRIC_REFRESH_SECONDS)
    except (TypeError, ValueError):
        return _DEFAULT_RUNNING_METRIC_REFRESH_SECONDS
    return max(1.0, value)


async def _running_metric_refresh_seconds(db: AsyncSession) -> float:
    raw = await config_service.get_value_or_default(
        db,
        _RUNNING_METRIC_REFRESH_CONFIG_KEY,
        str(int(_DEFAULT_RUNNING_METRIC_REFRESH_SECONDS)),
    )
    return _parse_running_metric_refresh_seconds(raw)


async def _maybe_refresh_running_metric_snapshot(db: AsyncSession, rpt: Report, window_sec: int) -> None:
    if rpt.status != TestCaseStatus.RUN_ING.value:
        return
    schedule_running_metric_snapshot_generation(rpt.id, (window_sec,))


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
        log.warning(
            "报告指标快照生成失败: report_id=%s", report_id, exc_info=(type(exc), exc, exc.__traceback__)
        )


async def get_jtl_metrics(db: AsyncSession, report_id: int, window_sec: int = 5) -> list[dict]:
    """读取指定报告指标快照。缺快照时触发后台补算，不在请求路径解析 JTL。"""
    rpt = await crud.get_by_id(db, report_id)
    if rpt is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    snapshots = await _list_metric_snapshots(db, report_id, window_sec)
    if snapshots:
        await _maybe_refresh_running_metric_snapshot(db, rpt, window_sec)
        return snapshots

    if rpt.status == TestCaseStatus.RUN_ING.value:
        schedule_running_metric_snapshot_generation(report_id, (window_sec,))
        return []

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


async def compare_reports(db: AsyncSession, base_id: int, target_id: int, window_sec: int = 5) -> dict:
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
