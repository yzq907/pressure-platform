"""定时任务 Service：CRUD + 后台调度循环。"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import stamp_create, stamp_modify
from app.core.codes import Codes
from app.core.context import UserContext
from app.core.exceptions import MysteriousException
from app.core.response import PageVO
from app.crud import scheduled_task as crud
from app.db.session import AsyncSessionLocal
from app.models.scheduled_task import ScheduledTask
from app.schemas.scheduled_task import (
    ScheduleData,
    ScheduledTaskParam,
    ScheduledTaskQuery,
    ScheduledTaskVO,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
log = logging.getLogger(__name__)


# ---- helpers ----

def _now() -> datetime:
    return datetime.now(SHANGHAI)


def _today() -> datetime:
    return _now().replace(hour=0, minute=0, second=0, microsecond=0)


def _compute_next_run(schedule_type: str, schedule_data: ScheduleData) -> datetime | None:
    """计算下次执行时间。返回 None 表示无法计算（如 once 时间已过且已执行）。"""
    now = _now()

    if schedule_type == "once":
        try:
            dt = datetime.strptime(schedule_data.time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
        except ValueError:
            dt = now + timedelta(minutes=1)
        return dt if dt > now else dt  # 即使是过去的时间也返回，由 trigger_next_run 处理

    time_str = schedule_data.time or "00:00"
    try:
        hour, minute = map(int, time_str.split(":"))
    except (ValueError, TypeError):
        hour, minute = 0, 0

    if schedule_type == "daily":
        candidate = _today().replace(hour=hour, minute=minute)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if schedule_type == "weekly":
        days = schedule_data.days_of_week or []
        if not days:
            return now + timedelta(minutes=1)
        today_wday = now.isoweekday()  # 1=Mon, 7=Sun
        # 找到最近的未来匹配日
        for delta in range(8):
            check_day = now + timedelta(days=delta)
            if check_day.isoweekday() in days:
                candidate = check_day.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if candidate > now or delta > 0:
                    return candidate
        return now + timedelta(days=7)

    if schedule_type == "monthly":
        dom = schedule_data.day_of_month or 1
        dom = max(1, min(28, dom))
        candidate = _today().replace(day=dom, hour=hour, minute=minute)
        if candidate <= now:
            # 下个月
            month = now.month + 1
            year = now.year
            if month > 12:
                month = 1
                year += 1
            candidate = candidate.replace(year=year, month=month, day=dom)
        return candidate

    return None


async def _trigger_next_run(db: AsyncSession, task: ScheduledTask) -> None:
    """执行后推进 next_run_at；once 类型自动禁用。"""
    task.last_run_at = _now()
    if task.schedule_type == "once":
        task.enabled = 0
        task.next_run_at = None
    else:
        sd = ScheduleData.model_validate(json.loads(task.schedule_data))
        task.next_run_at = _compute_next_run(task.schedule_type, sd)
    await db.commit()


# ---- CRUD service functions ----

def _to_vo(obj: ScheduledTask) -> ScheduledTaskVO:
    return ScheduledTaskVO.model_validate(obj)


async def add_scheduled_task(
    db: AsyncSession, param: ScheduledTaskParam, user: UserContext
) -> int:
    sd = param.schedule_data
    next_run = _compute_next_run(param.schedule_type, sd)
    obj = ScheduledTask(
        test_case_id=param.test_case_id,
        schedule_type=param.schedule_type,
        schedule_data=sd.model_dump_json(by_alias=True),
        run_param=param.run_param.model_dump_json(by_alias=True),
        enabled=1,
        next_run_at=next_run,
    )
    stamp_create(obj, user)
    await crud.add(db, obj)
    log.info("ScheduledTask created: id=%d type=%s next=%s", obj.id, obj.schedule_type, obj.next_run_at)
    return obj.id


async def update_scheduled_task(
    db: AsyncSession, id: int, param: ScheduledTaskParam, user: UserContext
) -> bool:
    obj = await crud.get_by_id(db, id)
    if obj is None:
        raise MysteriousException(Codes.SCHEDULED_TASK_NOT_EXIST)
    sd = param.schedule_data
    obj.test_case_id = param.test_case_id
    obj.schedule_type = param.schedule_type
    obj.schedule_data = sd.model_dump_json(by_alias=True)
    obj.run_param = param.run_param.model_dump_json(by_alias=True)
    obj.next_run_at = _compute_next_run(param.schedule_type, sd)
    stamp_modify(obj, user)
    return await crud.update(db, obj)


async def delete_scheduled_task(db: AsyncSession, id: int) -> bool:
    ok = await crud.delete(db, id)
    if not ok:
        raise MysteriousException(Codes.SCHEDULED_TASK_NOT_EXIST)
    return True


async def toggle_enabled(db: AsyncSession, id: int, enabled: int, user: UserContext) -> bool:
    obj = await crud.get_by_id(db, id)
    if obj is None:
        raise MysteriousException(Codes.SCHEDULED_TASK_NOT_EXIST)
    obj.enabled = enabled
    if enabled == 1 and obj.schedule_type != "once":
        sd = ScheduleData.model_validate(json.loads(obj.schedule_data))
        obj.next_run_at = _compute_next_run(obj.schedule_type, sd)
    stamp_modify(obj, user)
    return await crud.update(db, obj)


async def get_scheduled_task_list(
    db: AsyncSession, query: ScheduledTaskQuery
) -> PageVO[ScheduledTaskVO]:
    total = await crud.count(db, test_case_id=query.test_case_id, enabled=query.enabled)
    page_vo: PageVO[ScheduledTaskVO] = PageVO(page=query.page, size=query.size, total=total, list=[])
    if total <= 0:
        return page_vo
    offset = PageVO.offset(query.page, query.size)
    items = await crud.list_tasks(
        db, test_case_id=query.test_case_id, enabled=query.enabled, offset=offset, limit=query.size
    )
    page_vo.list = [_to_vo(o) for o in items]
    return page_vo


async def get_by_test_case(db: AsyncSession, test_case_id: int) -> list[ScheduledTaskVO]:
    items = await crud.get_by_test_case_id(db, test_case_id)
    return [_to_vo(o) for o in items]


async def trigger_now(db: AsyncSession, id: int, user: UserContext) -> bool:
    """立即触发一次定时任务执行。"""
    obj = await crud.get_by_id(db, id)
    if obj is None:
        raise MysteriousException(Codes.SCHEDULED_TASK_NOT_EXIST)

    run_param_dict = json.loads(obj.run_param)
    from app.schemas.testcase import RunParam

    run_param = RunParam.model_validate(run_param_dict)

    # 区域可用性检查
    region = run_param.region.strip() if run_param.region else ""
    if region:
        from app.crud import node as node_crud
        slaves = await node_crud.list_enable_slaves(db, region=region)
        healthy = [s for s in slaves if s.health_status == 1]
        if not healthy:
            raise MysteriousException(
                Codes.FAIL,
                message=f"区域「{region}」暂无可用压力机，无法执行",
            )

    from app.services.testcase import run_testcase
    await run_testcase(db, obj.test_case_id, run_param, user)

    obj.last_run_at = _now()
    if obj.schedule_type == "once":
        obj.enabled = 0
        obj.next_run_at = None
    else:
        sd = ScheduleData.model_validate(json.loads(obj.schedule_data))
        obj.next_run_at = _compute_next_run(obj.schedule_type, sd)
    await db.commit()

    log.info("ScheduledTask triggered: id=%d testcase=%d", obj.id, obj.test_case_id)
    return True


# ---- Scheduler background loop ----

_scheduler_task: asyncio.Task | None = None


async def _scheduler_loop(poll_interval: int = 60) -> None:
    """后台轮询到期任务并执行。"""
    log.info("Scheduler loop started (interval=%ds)", poll_interval)
    while True:
        try:
            await asyncio.sleep(poll_interval)
            async with AsyncSessionLocal() as db:
                due = await crud.get_due_tasks(db, _now())
                for task in due:
                    await _execute_scheduled(db, task)
        except asyncio.CancelledError:
            log.info("Scheduler loop cancelled")
            break
        except Exception:
            log.exception("Scheduler loop error, will retry")


async def _execute_scheduled(db: AsyncSession, task: ScheduledTask) -> None:
    """执行一个到期任务。"""
    try:
        run_param_dict = json.loads(task.run_param)
        from app.core.context import UserContext
        from app.schemas.testcase import RunParam

        run_param = RunParam.model_validate(run_param_dict)
        user = UserContext(
            id=int(task.creator_id) if task.creator_id.isdigit() else 0,
            username=task.creator or "scheduler",
            real_name=task.creator or "定时调度",
        )

        from app.crud import testcase as tc_crud
        from app.core.enums import TestCaseStatus
        tc = await tc_crud.get_by_id(db, task.test_case_id)
        if tc is None or tc.status == TestCaseStatus.RUN_ING.value:
            log.warning("Scheduler: skip task %d (testcase %d status=%s)",
                        task.id, task.test_case_id, tc.status if tc else "deleted")
            await _trigger_next_run(db, task)
            return

        # 区域可用性检查：指定了区域但无健康 slave 时跳过
        region = run_param.region.strip() if run_param.region else ""
        if region:
            from app.crud import node as node_crud
            slaves = await node_crud.list_enable_slaves(db, region=region)
            healthy = [s for s in slaves if s.health_status == 1]
            if not healthy:
                log.warning(
                    "Scheduler: skip task %d (testcase=%d) — region '%s' has no healthy slaves",
                    task.id, task.test_case_id, region,
                )
                await _trigger_next_run(db, task)
                return

        from app.services.testcase import run_testcase
        await run_testcase(db, task.test_case_id, run_param, user)
        log.info("Scheduler: task %d fired (testcase=%d type=%s)",
                 task.id, task.test_case_id, task.schedule_type)
    except Exception:
        log.exception("Scheduler: task %d failed", task.id)
    finally:
        await _trigger_next_run(db, task)


def start_scheduler(poll_interval: int = 60) -> asyncio.Task:
    global _scheduler_task
    if _scheduler_task is not None and not _scheduler_task.done():
        return _scheduler_task
    _scheduler_task = asyncio.create_task(_scheduler_loop(poll_interval))
    return _scheduler_task


async def stop_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task:
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass
        _scheduler_task = None
