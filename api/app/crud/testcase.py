"""TestCase 的 CRUD 操作，对齐 Java TestCaseMapper + testcase.xml。"""

from __future__ import annotations

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.testcase import TestCase


async def get_by_id(db: AsyncSession, id: int) -> TestCase | None:
    return await db.get(TestCase, id)


async def get_by_name(db: AsyncSession, name: str) -> TestCase | None:
    stmt = select(TestCase).where(TestCase.name == name)
    return (await db.execute(stmt)).scalar_one_or_none()


async def add(db: AsyncSession, obj: TestCase) -> TestCase:
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def update(db: AsyncSession, obj: TestCase) -> bool:
    await db.commit()
    return True


async def delete(db: AsyncSession, id: int) -> bool:
    result = await db.execute(sql_delete(TestCase).where(TestCase.id == id))
    await db.commit()
    return result.rowcount > 0


async def count(
    db: AsyncSession,
    id: int | None = None,
    name: str | None = None,
    biz: str | None = None,
    service: str | None = None,
) -> int:
    """对齐 Java：id 精确匹配；name/biz/service LIKE %?%"""
    stmt = select(func.count()).select_from(TestCase)
    if id is not None:
        stmt = stmt.where(TestCase.id == id)
    if name is not None:
        stmt = stmt.where(TestCase.name.like(f"%{name}%"))
    if biz is not None:
        stmt = stmt.where(TestCase.biz.like(f"%{biz}%"))
    if service is not None:
        stmt = stmt.where(TestCase.service.like(f"%{service}%"))
    return (await db.execute(stmt)).scalar_one() or 0


async def list_testcases(
    db: AsyncSession,
    id: int | None,
    name: str | None,
    biz: str | None,
    service: str | None,
    offset: int,
    limit: int,
) -> list[TestCase]:
    stmt = select(TestCase)
    if id is not None:
        stmt = stmt.where(TestCase.id == id)
    if name is not None:
        stmt = stmt.where(TestCase.name.like(f"%{name}%"))
    if biz is not None:
        stmt = stmt.where(TestCase.biz.like(f"%{biz}%"))
    if service is not None:
        stmt = stmt.where(TestCase.service.like(f"%{service}%"))
    stmt = stmt.order_by(TestCase.modify_time.desc()).offset(offset).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def list_by_status(db: AsyncSession, status: int | None) -> list[TestCase]:
    """Phase 5 调度用：查 status=RUN_ING / 等"""
    stmt = select(TestCase)
    if status is not None:
        stmt = stmt.where(TestCase.status == status)
    return list((await db.execute(stmt)).scalars().all())
