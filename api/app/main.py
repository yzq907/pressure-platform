"""FastAPI 应用入口。"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.v1.audit_log import router as audit_log_router
from app.api.v1.ai_generation import router as ai_generation_router
from app.api.v1.config import router as config_router
from app.api.v1.csv import router as csv_router
from app.api.v1.health import router as health_router
from app.api.v1.jar import router as jar_router
from app.api.v1.jmx import router as jmx_router
from app.api.v1.node import router as node_router
from app.api.v1.report import router as report_router
from app.api.v1.role import router as role_router
from app.api.v1.scheduled_task import router as scheduled_task_router
from app.api.v1.testcase import router as testcase_router
from app.api.v1.upload_file import router as upload_file_router
from app.api.v1.user import router as user_router
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import setup_logging
from app.db.redis import dispose_redis
from app.db import session as session_module
from app.db.session import dispose_engine

setup_logging()
log = logging.getLogger(__name__)


async def _recover_stuck_tasks() -> None:
    """启动自愈：先停止可能残留的 JMeter 进程，再修复 DB 中的运行态。"""
    from app.core.enums import TestCaseStatus
    from app.models.execution_queue import ExecutionQueue
    from app.models.execution_run import ExecutionRun
    from app.crud import report as report_crud, testcase as testcase_crud
    from app.services import execution_node, jmeter_runner
    from sqlalchemy import select

    async with session_module.AsyncSessionLocal() as db:
        running_reports = await report_crud.has_any_running(db)
        running_report_ids = {rpt.id for rpt in running_reports}
        active_run_report_ids = set(
            (
                await db.execute(
                    select(ExecutionRun.report_id).where(
                        ExecutionRun.status.in_(("preparing", "running", "stopping"))
                    )
                )
            )
            .scalars()
            .all()
        )

    for report_id in sorted(running_report_ids | active_run_report_ids):
        try:
            stopped = await jmeter_runner.launch_stop(report_id)
            log.info("启动自愈停止残留执行: report_id=%s stopped=%s", report_id, stopped)
        except Exception:
            log.exception("启动自愈停止残留执行失败: report_id=%s", report_id)

    async with session_module.AsyncSessionLocal() as db:
        stuck_reports = await report_crud.has_any_running(db)
        for rpt in stuck_reports:
            rpt.status = TestCaseStatus.RUN_FAILED.value
            rpt.response_data = "Master 重启，任务被终止"

        stuck_tcs = await testcase_crud.list_by_status(db, TestCaseStatus.RUN_ING.value)
        for tc in stuck_tcs:
            tc.status = TestCaseStatus.RUN_FAILED.value

        stuck_queues = list(
            (
                await db.execute(
                    select(ExecutionQueue).where(ExecutionQueue.status.in_(("running", "canceling")))
                )
            )
            .scalars()
            .all()
        )
        for queue in stuck_queues:
            queue.status = "failed"
            queue.message = "Master 重启，执行队列任务被终止"

        stuck_runs = list(
            (
                await db.execute(
                    select(ExecutionRun).where(ExecutionRun.status.in_(("preparing", "running", "stopping")))
                )
            )
            .scalars()
            .all()
        )
        for run in stuck_runs:
            run.status = "lost"
            run.finished_at = datetime.now()
            run.message = "Master 重启，执行进程状态丢失"

        await db.commit()
        lost_leases = await execution_node.mark_active_lost(db, "Master 重启，压力机租约状态丢失")
        if stuck_reports or stuck_tcs or stuck_queues or stuck_runs:
            log.info(
                "启动自愈完成：修复 %d 条报告，%d 条用例，%d 条队列，%d 条执行记录，%d 条节点租约",
                len(stuck_reports),
                len(stuck_tcs),
                len(stuck_queues),
                len(stuck_runs),
                lost_leases,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.start_time = time.monotonic()
    log.info("Mysterious API starting on port %s", get_settings().server_port)

    from app.db.schema import (
        ensure_ai_generation_tables,
        ensure_config_value_text_column,
        ensure_csv_distribution_columns,
        ensure_execution_node_table,
        ensure_execution_queue_table,
        ensure_execution_run_table,
        ensure_report_metric_snapshot_table,
        ensure_report_transaction_metric_snapshot_table,
        ensure_report_transaction_snapshot_table,
        ensure_rbac_schema,
        ensure_report_snapshot_columns,
        ensure_scheduled_task_log_table,
        ensure_upload_file_table,
    )
    await ensure_config_value_text_column()
    await ensure_ai_generation_tables()
    await ensure_report_snapshot_columns()
    await ensure_csv_distribution_columns()
    await ensure_rbac_schema()
    await ensure_scheduled_task_log_table()
    await ensure_execution_queue_table()
    await ensure_execution_run_table()
    await ensure_execution_node_table()
    await ensure_report_metric_snapshot_table()
    await ensure_report_transaction_snapshot_table()
    await ensure_report_transaction_metric_snapshot_table()
    await ensure_upload_file_table()

    # 初始化：创建 admin 用户（如不存在）
    async with session_module.AsyncSessionLocal() as db:
        from app.services.config import ensure_default_configs
        from app.services.user import ensure_admin_user
        await ensure_default_configs(db)
        await ensure_admin_user(db)

    # 启动自愈：修复上次异常退出时残留的 RUN_ING 状态
    await _recover_stuck_tasks()

    try:
        # 启动定时任务调度器
        from app.services.scheduled_task import start_scheduler, stop_scheduler
        from app.services.report_cleanup import start_cleanup_scheduler, stop_cleanup_scheduler
        from app.services.node_heartbeat import start_heartbeat_scheduler, stop_heartbeat_scheduler
        from app.services.timeout_scanner import start_timeout_scanner, stop_timeout_scanner
        from app.services.execution_queue import start_execution_queue_dispatcher, stop_execution_queue_dispatcher
        start_scheduler(poll_interval=60)
        start_cleanup_scheduler()
        start_heartbeat_scheduler()
        start_timeout_scanner()
        start_execution_queue_dispatcher()
        yield
    finally:
        log.info("Mysterious API shutting down")
        await stop_scheduler()
        await stop_cleanup_scheduler()
        await stop_heartbeat_scheduler()
        await stop_timeout_scanner()
        await stop_execution_queue_dispatcher()
        await dispose_engine()
        await dispose_redis()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Mysterious 压测平台后端 (Python)",
        description="基于 FastAPI + SQLAlchemy 2.0 的分布式压测平台后端 API",
        version="0.1.0",
        openapi_url="/v2/api-docs",
        docs_url="/swagger-ui.html",
        redoc_url=None,
        lifespan=lifespan,
    )

    origins = settings.cors_origins_list
    is_wildcard = origins == ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=not is_wildcard,
        allow_methods=["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        max_age=3600,
    )

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(user_router)
    app.include_router(config_router)
    app.include_router(ai_generation_router)
    app.include_router(node_router)
    app.include_router(testcase_router)
    app.include_router(jmx_router)
    app.include_router(csv_router)
    app.include_router(upload_file_router)
    app.include_router(jar_router)
    app.include_router(report_router)
    app.include_router(role_router)
    app.include_router(scheduled_task_router)
    app.include_router(audit_log_router)

    # 报告预览静态文件服务
    reports_dir = "/root/PyProject/mysterious-data"
    if Path(reports_dir).is_dir():
        app.mount("/reports", StaticFiles(directory=reports_dir), name="reports")

    return app


app = create_app()
