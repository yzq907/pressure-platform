"""Jmx CRUD 操作，对齐 Java JmxMapper + jmx.xml。"""

from __future__ import annotations

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.jmx import Jmx


async def get_by_id(db: AsyncSession, id: int) -> Jmx | None:
    return await db.get(Jmx, id)


async def get_by_test_case_id(db: AsyncSession, test_case_id: int) -> Jmx | None:
    """JMX 和用例 1:1，所以这里返回单条"""
    stmt = select(Jmx).where(Jmx.test_case_id == test_case_id).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()


async def add(db: AsyncSession, obj: Jmx) -> Jmx:
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def update(db: AsyncSession, obj: Jmx) -> bool:
    await db.commit()
    return True


async def delete(db: AsyncSession, id: int) -> bool:
    result = await db.execute(sql_delete(Jmx).where(Jmx.id == id))
    await db.commit()
    return result.rowcount > 0


async def count(
    db: AsyncSession,
    src_name: str | None = None,
    test_case_id: int | None = None,
) -> int:
    stmt = select(func.count()).select_from(Jmx)
    if src_name is not None:
        stmt = stmt.where(Jmx.src_name.like(f"%{src_name}%"))
    if test_case_id is not None:
        stmt = stmt.where(Jmx.test_case_id == test_case_id)
    return (await db.execute(stmt)).scalar_one() or 0


async def list_jmxs(
    db: AsyncSession,
    src_name: str | None,
    test_case_id: int | None,
    offset: int,
    limit: int,
) -> list[Jmx]:
    stmt = select(Jmx)
    if src_name is not None:
        stmt = stmt.where(Jmx.src_name.like(f"%{src_name}%"))
    if test_case_id is not None:
        stmt = stmt.where(Jmx.test_case_id == test_case_id)
    stmt = stmt.order_by(Jmx.modify_time.desc()).offset(offset).limit(limit)
    return list((await db.execute(stmt)).scalars().all())
