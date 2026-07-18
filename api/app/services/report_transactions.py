"""Report transaction statistics and transaction trend snapshots."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import Codes
from app.core.exceptions import MysteriousException
from app.crud import report as crud
from app.db import session as session_module
from app.models.report_transaction_metric_snapshot import ReportTransactionMetricSnapshot
from app.models.report_transaction_snapshot import ReportTransactionSnapshot
from app.schemas.report import (
    TransactionMetricPointVO,
    TransactionMetricsVO,
    TransactionStatsVO,
    TransactionTrendVO,
    TrendPointVO,
)
from app.services.report_metrics import (
    _find_jtl_file,
    _find_statistics_file,
    _list_metric_snapshots,
    _load_run_meta,
    _meta_int,
    _normalize_threads,
    _percentile,
    _round2,
    _to_float,
    _to_int,
    schedule_metric_snapshot_generation,
)

log = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
_transaction_snapshot_tasks: set[int] = set()
_transaction_metric_snapshot_tasks: set[tuple[int, tuple[int, ...]]] = set()
TRANSACTION_METRIC_SNAPSHOT_VERSION = "tm_v2"

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
        "elapsed_sum": 0,
        "max_elapsed": 0,
        "min_elapsed": None,
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
                        "elapsed_sum": 0,
                        "max_elapsed": 0,
                        "min_elapsed": None,
                        "failed": 0,
                        "samples": 0,
                        "start_ts": 0,
                        "end_ts": 0,
                    },
                )
                for target in (group, total):
                    target["elapsed_sum"] += elapsed
                    target["max_elapsed"] = max(int(target["max_elapsed"] or 0), elapsed)
                    min_elapsed = target["min_elapsed"]
                    target["min_elapsed"] = elapsed if min_elapsed is None else min(min_elapsed, elapsed)
                    target["samples"] += 1
                    if not success:
                        target["failed"] += 1
                    _touch_duration(target, ts, elapsed)
    except OSError as e:
        log.warning("读取 JTL 事务统计失败: %s", e)
        return []

    def _to_row(target: dict) -> dict:
        samples = int(target["samples"] or 0)
        duration_sec = max((int(target["end_ts"] or 0) - int(target["start_ts"] or 0)) / 1000.0, 1.0)
        return {
            "name": target["name"],
            "samples": samples,
            "failed": int(target["failed"] or 0),
            "tps": samples / duration_sec if samples else 0,
            "avg_rt": (int(target["elapsed_sum"] or 0) / samples) if samples else 0,
            "max_rt": int(target["max_elapsed"] or 0),
            "min_rt": int(target["min_elapsed"] or 0),
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
    await db.execute(
        sql_delete(ReportTransactionSnapshot).where(ReportTransactionSnapshot.report_id == report_id)
    )
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
    jtl_path = _find_jtl_file(rpt.report_dir)
    if jtl_path:
        rows = await asyncio.to_thread(_parse_jtl_transaction_stats, jtl_path)
    if not rows:
        statistics_path = _find_statistics_file(rpt.report_dir)
        if statistics_path:
            rows = await asyncio.to_thread(_parse_statistics_json_transactions, statistics_path)
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

    schedule_transaction_snapshot_generation(report_id)
    return []

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
    await db.execute(
        sql_delete(ReportTransactionMetricSnapshot).where(
            ReportTransactionMetricSnapshot.report_id == report_id,
            ReportTransactionMetricSnapshot.window_sec == window_sec,
        )
    )
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
        if await _transaction_metric_snapshots_need_refresh(db, report_id, window_sec):
            log.info("报告交易曲线快照版本过旧，后台解析 JTL 回填: report_id=%s window=%s", report_id, window_sec)
            schedule_transaction_metric_snapshot_generation(report_id, (window_sec,))
        return snapshots

    schedule_transaction_metric_snapshot_generation(report_id, (window_sec,))
    return snapshots

async def get_transaction_trend(db: AsyncSession, report_id: int, window_sec: int = 60) -> TransactionTrendVO:
    transaction_metrics = await get_transaction_metrics(db, report_id, window_sec)
    metric_window = max(1, int(window_sec or 60))
    has_transaction_points = any(transaction_metrics.series.values())
    overall_metrics: list[dict] = []
    if not has_transaction_points:
        overall_metrics = await _list_metric_snapshots(db, report_id, metric_window)
        if not overall_metrics:
            schedule_metric_snapshot_generation(report_id, (metric_window,))

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
