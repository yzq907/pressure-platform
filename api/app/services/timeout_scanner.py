"""执行超时扫描服务：每 60 秒检查所有 RUN_ING 报告，超时则 kill 进程并标记失败。

对齐需求：testcase.timeout_seconds（默认 7200s = 2h，范围 600-86400），
以 report.create_time + timeout_seconds 为截止时间。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import TestCaseStatus
from app.db.session import AsyncSessionLocal
from app.models.report import Report
from app.models.testcase import TestCase
from app.services import jmeter_runner

SHANGHAI = ZoneInfo("Asia/Shanghai")
log = logging.getLogger(__name__)

_scanner_task: asyncio.Task | None = None

# 扫描间隔（秒）
_SCAN_INTERVAL = 60


def _now() -> datetime:
    return datetime.now(SHANGHAI)


def _as_local_naive(dt: datetime) -> datetime:
    """统一转成上海时区 naive datetime，避免 aware/naive 比较异常。"""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(SHANGHAI).replace(tzinfo=None)


async def _scan_and_timeout(db: AsyncSession) -> int:
    """执行一轮超时扫描，返回被超时终止的报告数量。"""
    stmt = (
        select(Report, TestCase)
        .join(TestCase, Report.test_case_id == TestCase.id)
        .where(Report.status == TestCaseStatus.RUN_ING.value)
    )
    result = await db.execute(stmt)
    rows = list(result.all())

    if not rows:
        return 0

    timed_out_count = 0
    now = _as_local_naive(_now())

    for rpt, tc in rows:
        # 取 testcase 的 timeout_seconds，兜底 7200
        timeout_sec = tc.timeout_seconds or 7200
        # create_time 通常是 naive datetime（不带 tz），按本地时间比较
        create_time = rpt.create_time
        if create_time is None:
            continue
        deadline = _as_local_naive(create_time) + timedelta(seconds=timeout_sec)

        if now < deadline:
            continue

        # 已超时
        log.warning(
            "报告超时: report_id=%d testcase_id=%d create_time=%s timeout=%ds deadline=%s",
            rpt.id, tc.id, create_time, timeout_sec, deadline,
        )

        # 1) 尝试 kill 子进程（如果还在内存登记中）
        stopped = await jmeter_runner.launch_stop(rpt.id)
        log.info("超时 kill 结果: report_id=%d stopped=%s", rpt.id, stopped)

        # 2) 直接更新 DB（即使进程已不在内存中，也要把状态改回来）
        tc.status = TestCaseStatus.RUN_FAILED.value
        rpt.status = TestCaseStatus.RUN_FAILED.value
        rpt.response_data = f"执行超时（超过 {timeout_sec} 秒），已被系统自动终止"
        await db.commit()
        timed_out_count += 1

    if timed_out_count > 0:
        log.info("本轮超时扫描结束，共终止 %d 条超时报告", timed_out_count)
    return timed_out_count


async def _scanner_loop() -> None:
    """后台循环：每 _SCAN_INTERVAL 秒执行一次超时扫描。"""
    log.info("Timeout scanner started (interval=%ds)", _SCAN_INTERVAL)
    while True:
        try:
            await asyncio.sleep(_SCAN_INTERVAL)
            async with AsyncSessionLocal() as db:
                await _scan_and_timeout(db)
        except asyncio.CancelledError:
            log.info("Timeout scanner cancelled")
            break
        except Exception:
            log.exception("Timeout scanner loop error, will retry")


def start_timeout_scanner() -> asyncio.Task:
    global _scanner_task
    if _scanner_task is not None and not _scanner_task.done():
        return _scanner_task
    _scanner_task = asyncio.create_task(_scanner_loop())
    return _scanner_task


async def stop_timeout_scanner() -> None:
    global _scanner_task
    if _scanner_task:
        _scanner_task.cancel()
        try:
            await _scanner_task
        except asyncio.CancelledError:
            pass
        _scanner_task = None
