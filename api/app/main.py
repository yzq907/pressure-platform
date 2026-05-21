"""FastAPI 应用入口。"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.v1.audit_log import router as audit_log_router
from app.api.v1.config import router as config_router
from app.api.v1.csv import router as csv_router
from app.api.v1.health import router as health_router
from app.api.v1.jar import router as jar_router
from app.api.v1.jmx import router as jmx_router
from app.api.v1.node import router as node_router
from app.api.v1.report import router as report_router
from app.api.v1.scheduled_task import router as scheduled_task_router
from app.api.v1.testcase import router as testcase_router
from app.api.v1.user import router as user_router
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import setup_logging
from app.db.redis import dispose_redis
from app.db.session import AsyncSessionLocal, dispose_engine

setup_logging()
log = logging.getLogger(__name__)


async def _recover_stuck_tasks() -> None:
    """启动自愈：systemd restart 会清理整个 cgroup 的 jmeter 子进程，
    但 DB 中的 RUN_ING 状态可能残留，启动时统一重置为 FAILED。"""
    from app.core.enums import TestCaseStatus
    from app.crud import report as report_crud, testcase as testcase_crud

    async with AsyncSessionLocal() as db:
        stuck_reports = await report_crud.has_any_running(db)
        for rpt in stuck_reports:
            rpt.status = TestCaseStatus.RUN_FAILED.value
            rpt.response_data = "Master 重启，任务被终止"

        stuck_tcs = await testcase_crud.list_by_status(db, TestCaseStatus.RUN_ING.value)
        for tc in stuck_tcs:
            tc.status = TestCaseStatus.RUN_FAILED.value

        await db.commit()
        if stuck_reports or stuck_tcs:
            log.info(
                "启动自愈完成：修复 %d 条报告，%d 条用例",
                len(stuck_reports),
                len(stuck_tcs),
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.start_time = time.monotonic()
    log.info("Mysterious API starting on port %s", get_settings().server_port)

    # 初始化：创建 admin 用户（如不存在）
    async with AsyncSessionLocal() as db:
        from app.services.user import ensure_admin_user
        await ensure_admin_user(db)

    # 启动自愈：修复上次异常退出时残留的 RUN_ING 状态
    await _recover_stuck_tasks()

    try:
        # 启动定时任务调度器
        from app.services.scheduled_task import start_scheduler, stop_scheduler
        from app.services.report_cleanup import start_cleanup_scheduler, stop_cleanup_scheduler
        from app.services.node_heartbeat import start_heartbeat_scheduler, stop_heartbeat_scheduler
        from app.services.timeout_scanner import start_timeout_scanner, stop_timeout_scanner
        start_scheduler(poll_interval=60)
        start_cleanup_scheduler()
        start_heartbeat_scheduler()
        start_timeout_scanner()
        yield
    finally:
        log.info("Mysterious API shutting down")
        await stop_scheduler()
        await stop_cleanup_scheduler()
        await stop_heartbeat_scheduler()
        await stop_timeout_scanner()
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
    app.include_router(node_router)
    app.include_router(testcase_router)
    app.include_router(jmx_router)
    app.include_router(csv_router)
    app.include_router(jar_router)
    app.include_router(report_router)
    app.include_router(scheduled_task_router)
    app.include_router(audit_log_router)

    # 报告预览静态文件服务
    reports_dir = "/root/PyProject/mysterious-data"
    if Path(reports_dir).is_dir():
        app.mount("/reports", StaticFiles(directory=reports_dir), name="reports")

    return app


app = create_app()
