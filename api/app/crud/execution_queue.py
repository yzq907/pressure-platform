"""Execution queue CRUD."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_queue import ExecutionQueue

ACTIVE_STATUSES = ("pending", "running", "canceling")


async def get_by_id(db: AsyncSession, id: int) -> ExecutionQueue | None:
    return await db.get(ExecutionQueue, id)


async def get_by_report_id(db: AsyncSession, report_id: int) -> ExecutionQueue | None:
    stmt = select(ExecutionQueue).where(ExecutionQueue.report_id == report_id).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()


async def add(db: AsyncSession, obj: ExecutionQueue) -> ExecutionQueue:
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def update(db: AsyncSession, obj: ExecutionQueue) -> bool:
    await db.commit()
    return True


async def count(
    db: AsyncSession,
    *,
    status: str | None = None,
    test_case_id: int | None = None,
) -> int:
    stmt = select(func.count()).select_from(ExecutionQueue)
    if status:
        stmt = stmt.where(ExecutionQueue.status == status)
    if test_case_id is not None:
        stmt = stmt.where(ExecutionQueue.test_case_id == test_case_id)
    return (await db.execute(stmt)).scalar_one() or 0


async def list_queues(
    db: AsyncSession,
    *,
    status: str | None,
    test_case_id: int | None,
    offset: int,
    limit: int,
) -> list[ExecutionQueue]:
    stmt = select(ExecutionQueue)
    if status:
        stmt = stmt.where(ExecutionQueue.status == status)
    if test_case_id is not None:
        stmt = stmt.where(ExecutionQueue.test_case_id == test_case_id)
    stmt = stmt.order_by(ExecutionQueue.enqueue_time.asc(), ExecutionQueue.id.asc()).offset(offset).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def list_pending(db: AsyncSession, limit: int = 20) -> list[ExecutionQueue]:
    stmt = (
        select(ExecutionQueue)
        .where(ExecutionQueue.status == "pending")
        .order_by(ExecutionQueue.enqueue_time.asc(), ExecutionQueue.id.asc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


async def get_active_by_testcase(db: AsyncSession, test_case_id: int) -> ExecutionQueue | None:
    stmt = (
        select(ExecutionQueue)
        .where(
            ExecutionQueue.test_case_id == test_case_id,
            ExecutionQueue.status.in_(ACTIVE_STATUSES),
        )
        .order_by(ExecutionQueue.id.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_active_by_testcase_region(
    db: AsyncSession,
    test_case_id: int,
    region: str,
) -> ExecutionQueue | None:
    stmt = (
        select(ExecutionQueue)
        .where(
            ExecutionQueue.test_case_id == test_case_id,
            ExecutionQueue.region == region,
            ExecutionQueue.status.in_(ACTIVE_STATUSES),
        )
        .order_by(ExecutionQueue.id.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()
