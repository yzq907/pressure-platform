"""报告自动清理服务：按 REPORT_RETENTION_DAYS 配置，每日清理过期报告和审计日志。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import report as report_crud
from app.db.session import AsyncSessionLocal
from app.models.audit_log import AuditLog
from app.models.report import Report
from app.services import config as config_service
from app.services.report import clean_report

SHANGHAI = ZoneInfo("Asia/Shanghai")
log = logging.getLogger(__name__)

_cleanup_task: asyncio.Task | None = None


def _now() -> datetime:
    return datetime.now(SHANGHAI)


def _next_run_at(hour: int = 2, minute: int = 30) -> datetime:
    """计算下一次执行时间（默认每天凌晨 2:30）。"""
    now = _now()
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


async def _cleanup_expired_reports(db: AsyncSession) -> int:
    """执行一次过期报告清理，返回清理数量。"""
    try:
        days_str = await config_service.get_value(db, "REPORT_RETENTION_DAYS")
        days = int(days_str)
    except Exception:
        log.info("REPORT_RETENTION_DAYS 未配置或无效，跳过清理")
        return 0

    if days <= 0:
        log.info("REPORT_RETENTION_DAYS=%d，永久保存，跳过清理", days)
        return 0

    cutoff = _now() - timedelta(days=days)
    stmt = select(Report).where(Report.create_time < cutoff)
    result = await db.execute(stmt)
    expired = list(result.scalars().all())

    if not expired:
        log.info("没有超过 %d 天的过期报告", days)
        return 0

    count = 0
    for rpt in expired:
        try:
            await clean_report(db, rpt.id)
            count += 1
            log.info("清理过期报告 id=%d create_time=%s", rpt.id, rpt.create_time)
        except Exception:
            log.exception("清理报告 id=%d 失败", rpt.id)

    log.info("本次共清理 %d 条过期报告（ retention=%d 天）", count, days)
    return count


async def _cleanup_audit_logs(db: AsyncSession) -> int:
    """清理超过 AUDIT_RETENTION_DAYS 的审计日志。"""
    try:
        days_str = await config_service.get_value(db, "AUDIT_RETENTION_DAYS")
        days = int(days_str)
    except Exception:
        log.info("AUDIT_RETENTION_DAYS 未配置或无效，跳过审计日志清理")
        return 0

    if days <= 0:
        log.info("AUDIT_RETENTION_DAYS=%d，跳过审计日志清理", days)
        return 0

    cutoff = _now() - timedelta(days=days)
    stmt = sql_delete(AuditLog).where(AuditLog.create_time < cutoff)
    result = await db.execute(stmt)
    await db.commit()
    deleted = result.rowcount or 0
    if deleted > 0:
        log.info("清理 %d 条超过 %d 天的审计日志", deleted, days)
    return deleted


async def _cleanup_loop() -> None:
    """后台循环：每天执行一次过期报告清理和审计日志清理。"""
    log.info("Report cleanup scheduler started")
    while True:
        try:
            next_run = _next_run_at()
            sleep_seconds = (next_run - _now()).total_seconds()
            log.info("下次报告清理时间: %s（约 %.0f 秒后）", next_run, sleep_seconds)
            await asyncio.sleep(sleep_seconds)

            async with AsyncSessionLocal() as db:
                await _cleanup_expired_reports(db)
                await _cleanup_audit_logs(db)
        except asyncio.CancelledError:
            log.info("Report cleanup scheduler cancelled")
            break
        except Exception:
            log.exception("Report cleanup loop error, will retry tomorrow")
            await asyncio.sleep(3600)


def start_cleanup_scheduler() -> asyncio.Task:
    global _cleanup_task
    if _cleanup_task is not None and not _cleanup_task.done():
        return _cleanup_task
    _cleanup_task = asyncio.create_task(_cleanup_loop())
    return _cleanup_task


async def stop_cleanup_scheduler() -> None:
    global _cleanup_task
    if _cleanup_task:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
        _cleanup_task = None
