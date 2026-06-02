"""Manual execution queue service."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import stamp_create
from app.core.codes import Codes
from app.core.context import UserContext
from app.core.enums import TestCaseStatus
from app.core.exceptions import MysteriousException
from app.core.response import PageVO
from app.crud import execution_queue as crud
from app.crud import testcase as testcase_crud
from app.db import session as session_module
from app.models.report import Report
from app.models.execution_queue import ExecutionQueue
from app.schemas.execution_queue import ExecutionQueueQuery, ExecutionQueueVO
from app.schemas.testcase import RunParam

log = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_CANCELING = "canceling"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_CANCELED = "canceled"
POLICY_QUEUE_WHEN_NO_SLAVE = "queue_when_no_slave"

_dispatcher_task: asyncio.Task | None = None


def _now() -> datetime:
    return datetime.now(SHANGHAI).replace(tzinfo=None)


def _to_vo(obj: ExecutionQueue) -> ExecutionQueueVO:
    return ExecutionQueueVO.model_validate(obj)


async def enqueue_manual_execution(
    db: AsyncSession,
    *,
    testcase_id: int,
    param: RunParam,
    user: UserContext,
    message: str,
    available_slave_count: int,
) -> ExecutionQueue:
    region = param.region or ""
    existing = await crud.get_active_by_testcase_region(db, testcase_id, region)
    if existing is not None:
        raise MysteriousException(Codes.TESTCASE_IS_RUNNING)

    testcase = await testcase_crud.get_by_id(db, testcase_id)
    if testcase is None:
        raise MysteriousException(Codes.TESTCASE_NOT_EXIST)

    payload = param.model_dump(by_alias=True)
    obj = ExecutionQueue(
        test_case_id=testcase_id,
        report_id=0,
        status=STATUS_PENDING,
        queue_policy=param.queue_policy,
        trigger_type="manual",
        run_param=json.dumps(payload, ensure_ascii=False),
        region=region,
        requested_slave_count=param.slave_count,
        available_slave_count=available_slave_count,
        allocated_slave_count=0,
        slave_hosts="",
        message=message,
        enqueue_time=_now(),
    )
    stamp_create(obj, user)
    testcase.status = TestCaseStatus.RUN_WAITING.value
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    log.info(
        "执行任务已入队: queue_id=%s testcase_id=%s region=%s requested_slaves=%s message=%s",
        obj.id,
        testcase_id,
        obj.region,
        obj.requested_slave_count,
        message,
    )
    return obj


async def cancel_pending_execution(db: AsyncSession, queue_id: int, user: UserContext) -> bool:
    queue = await crud.get_by_id(db, queue_id)
    if queue is None:
        raise MysteriousException(Codes.FAIL, message="执行队列任务不存在")
    if queue.status != STATUS_PENDING:
        raise MysteriousException(Codes.FAIL, message="只有待执行任务可以取消")

    queue.status = STATUS_CANCELED
    queue.finish_time = _now()
    queue.message = "用户取消待执行任务"
    queue.modifier = user.real_name or user.username
    queue.modifier_id = str(user.id)
    queue.modify_time = _now()
    testcase = await testcase_crud.get_by_id(db, queue.test_case_id)
    if testcase is not None:
        testcase.status = TestCaseStatus.WAIT_CANCEL.value
    await db.commit()
    log.info("待执行任务已取消: queue_id=%s testcase_id=%s", queue.id, queue.test_case_id)
    return True


async def get_execution_queue_list(
    db: AsyncSession,
    query: ExecutionQueueQuery,
) -> PageVO[ExecutionQueueVO]:
    page_vo: PageVO[ExecutionQueueVO] = PageVO(page=query.page, size=query.size, total=0, list=[])
    total = await crud.count(db, status=query.status, test_case_id=query.test_case_id)
    if total <= 0:
        return page_vo
    page_vo.total = total
    items = await crud.list_queues(
        db,
        status=query.status,
        test_case_id=query.test_case_id,
        offset=PageVO.offset(query.page, query.size),
        limit=query.size,
    )
    page_vo.list = [_to_vo(item) for item in items]
    return page_vo


async def dispatch_pending_once(limit: int = 10) -> int:
    """Run one dispatcher scan. Returns started task count."""
    from app.services import testcase as testcase_service

    started = 0
    async with session_module.AsyncSessionLocal() as db:
        queues = await crud.list_pending(db, limit=limit)
        for queue in queues:
            param = RunParam.model_validate(json.loads(queue.run_param or "{}"))
            should_queue, message, available_count = await testcase_service._should_queue_run(
                db,
                queue.test_case_id,
                param,
                allow_waiting_queue_id=queue.id,
            )
            queue.available_slave_count = available_count
            if should_queue:
                queue.message = message
                await db.commit()
                continue
            await db.refresh(queue)
            if queue.status != STATUS_PENDING:
                log.info("执行队列已不是待执行状态，跳过启动: queue_id=%s status=%s", queue.id, queue.status)
                continue

            try:
                report_id = await testcase_service._run_testcase_now(
                    db,
                    queue.test_case_id,
                    param,
                    _queue_user(queue),
                    queue_id=queue.id,
                )
            except MysteriousException as e:
                queue.message = e.override_message or e.code.message
                await db.commit()
                log.info("执行队列暂未启动: queue_id=%s message=%s", queue.id, queue.message)
                continue
            except Exception as e:  # noqa: BLE001
                queue.status = STATUS_FAILED
                queue.message = f"启动失败: {e}"
                queue.finish_time = _now()
                testcase = await testcase_crud.get_by_id(db, queue.test_case_id)
                if testcase is not None:
                    testcase.status = TestCaseStatus.RUN_FAILED.value
                await db.commit()
                log.exception("执行队列启动异常: queue_id=%s", queue.id)
                continue

            await db.refresh(queue)
            report = await db.get(Report, report_id)
            if report is not None and report.status != TestCaseStatus.RUN_ING.value:
                queue.status = STATUS_SUCCESS if report.status == TestCaseStatus.RUN_SUCCESS.value else STATUS_FAILED
                queue.finish_time = _now()
                queue.message = "执行完成" if queue.status == STATUS_SUCCESS else "执行失败"
            await db.commit()
            started += 1
            log.info("执行队列已启动: queue_id=%s report_id=%s", queue.id, report_id)
    return started


def _queue_user(queue: ExecutionQueue) -> UserContext:
    try:
        user_id = int(queue.creator_id or "0")
    except ValueError:
        user_id = 0
    return UserContext(
        id=user_id,
        username=queue.creator or "system",
        real_name=queue.creator or "system",
    )


async def sync_report_status(db: AsyncSession, report_id: int, status: TestCaseStatus) -> None:
    queue = await crud.get_by_report_id(db, report_id)
    if queue is None or queue.status not in (STATUS_RUNNING, STATUS_CANCELING):
        return
    queue.status = STATUS_SUCCESS if status == TestCaseStatus.RUN_SUCCESS else STATUS_FAILED
    queue.finish_time = _now()
    queue.message = "执行完成" if queue.status == STATUS_SUCCESS else "执行失败"


async def _dispatcher_loop(poll_interval: int) -> None:
    while True:
        try:
            await dispatch_pending_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("执行队列调度扫描异常")
        await asyncio.sleep(poll_interval)


def start_execution_queue_dispatcher(poll_interval: int = 10) -> None:
    global _dispatcher_task
    if _dispatcher_task is not None and not _dispatcher_task.done():
        return
    _dispatcher_task = asyncio.create_task(
        _dispatcher_loop(poll_interval),
        name="execution-queue-dispatcher",
    )
    log.info("执行队列调度器已启动 poll_interval=%s", poll_interval)


async def stop_execution_queue_dispatcher() -> None:
    global _dispatcher_task
    task = _dispatcher_task
    _dispatcher_task = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        log.info("执行队列调度器已停止")
