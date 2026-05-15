"""Jar CRUD 操作，对齐 Java JarMapper + jar.xml。"""

from __future__ import annotations

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.jar import Jar


async def get_by_id(db: AsyncSession, id: int) -> Jar | None:
    return await db.get(Jar, id)


async def get_by_test_case_id(db: AsyncSession, test_case_id: int) -> list[Jar]:
    stmt = select(Jar).where(Jar.test_case_id == test_case_id)
    return list((await db.execute(stmt)).scalars().all())


async def get_exist_list(
    db: AsyncSession, test_case_id: int, src_name: str, jar_dir: str
) -> list[Jar]:
    stmt = select(Jar).where(
        Jar.test_case_id == test_case_id,
        Jar.src_name == src_name,
        Jar.jar_dir == jar_dir,
    )
    return list((await db.execute(stmt)).scalars().all())


async def add(db: AsyncSession, obj: Jar) -> Jar:
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def update(db: AsyncSession, obj: Jar) -> bool:
    await db.commit()
    return True


async def delete(db: AsyncSession, id: int) -> bool:
    result = await db.execute(sql_delete(Jar).where(Jar.id == id))
    await db.commit()
    return result.rowcount > 0


async def count(
    db: AsyncSession,
    src_name: str | None = None,
    test_case_id: int | None = None,
) -> int:
    stmt = select(func.count()).select_from(Jar)
    if src_name is not None:
        stmt = stmt.where(Jar.src_name.like(f"%{src_name}%"))
    if test_case_id is not None:
        stmt = stmt.where(Jar.test_case_id == test_case_id)
    return (await db.execute(stmt)).scalar_one() or 0


async def list_jars(
    db: AsyncSession,
    src_name: str | None,
    test_case_id: int | None,
    offset: int,
    limit: int,
) -> list[Jar]:
    stmt = select(Jar)
    if src_name is not None:
        stmt = stmt.where(Jar.src_name.like(f"%{src_name}%"))
    if test_case_id is not None:
        stmt = stmt.where(Jar.test_case_id == test_case_id)
    stmt = stmt.order_by(Jar.modify_time.desc()).offset(offset).limit(limit)
    return list((await db.execute(stmt)).scalars().all())
