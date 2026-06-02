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
    description: str | None = None,
    biz: str | None = None,
    service: str | None = None,
) -> int:
    """对齐 Java：id 精确匹配；name/description/biz/service LIKE %?%"""
    stmt = select(func.count()).select_from(TestCase)
    if id is not None:
        stmt = stmt.where(TestCase.id == id)
    if name is not None:
        stmt = stmt.where(TestCase.name.like(f"%{name}%"))
    if description is not None:
        stmt = stmt.where(TestCase.description.like(f"%{description}%"))
    if biz is not None:
        stmt = stmt.where(TestCase.biz.like(f"%{biz}%"))
    if service is not None:
        stmt = stmt.where(TestCase.service.like(f"%{service}%"))
    return (await db.execute(stmt)).scalar_one() or 0


def _apply_filters(
    stmt,
    id: int | None = None,
    name: str | None = None,
    description: str | None = None,
    biz: str | None = None,
    service: str | None = None,
):
    if id is not None:
        stmt = stmt.where(TestCase.id == id)
    if name is not None:
        stmt = stmt.where(TestCase.name.like(f"%{name}%"))
    if description is not None:
        stmt = stmt.where(TestCase.description.like(f"%{description}%"))
    if biz is not None:
        stmt = stmt.where(TestCase.biz.like(f"%{biz}%"))
    if service is not None:
        stmt = stmt.where(TestCase.service.like(f"%{service}%"))
    return stmt


async def count_by_status(
    db: AsyncSession,
    id: int | None = None,
    name: str | None = None,
    description: str | None = None,
    biz: str | None = None,
    service: str | None = None,
) -> dict[int, int]:
    """按状态聚合统计，过滤条件与 list/count 保持一致。"""
    stmt = select(TestCase.status, func.count()).select_from(TestCase)
    stmt = _apply_filters(stmt, id=id, name=name, description=description, biz=biz, service=service)
    stmt = stmt.group_by(TestCase.status)
    rows = (await db.execute(stmt)).all()
    return {int(status): int(total) for status, total in rows}


async def list_testcases(
    db: AsyncSession,
    id: int | None,
    name: str | None,
    description: str | None,
    biz: str | None,
    service: str | None,
    offset: int,
    limit: int,
) -> list[TestCase]:
    stmt = _apply_filters(
        select(TestCase),
        id=id,
        name=name,
        description=description,
        biz=biz,
        service=service,
    )
    stmt = stmt.order_by(TestCase.modify_time.desc()).offset(offset).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def list_by_status(db: AsyncSession, status: int | None) -> list[TestCase]:
    """Phase 5 调度用：查 status=RUN_ING / 等"""
    stmt = select(TestCase)
    if status is not None:
        stmt = stmt.where(TestCase.status == status)
    return list((await db.execute(stmt)).scalars().all())
