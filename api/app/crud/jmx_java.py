"""mysterious_jmx_java CRUD。"""

from __future__ import annotations

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.jmx_java import JmxJava


async def get_by_id(db: AsyncSession, id: int) -> JmxJava | None:
    return await db.get(JmxJava, id)


async def get_by_jmx_id(db: AsyncSession, jmx_id: int) -> list[JmxJava]:
    stmt = select(JmxJava).where(JmxJava.jmx_id == jmx_id)
    return list((await db.execute(stmt)).scalars().all())


async def add(db: AsyncSession, obj: JmxJava) -> JmxJava:
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def batch_add(db: AsyncSession, objs: list[JmxJava]) -> None:
    for o in objs:
        db.add(o)
    await db.commit()


async def update(db: AsyncSession, obj: JmxJava) -> bool:
    await db.commit()
    return True


async def delete(db: AsyncSession, id: int) -> bool:
    result = await db.execute(sql_delete(JmxJava).where(JmxJava.id == id))
    await db.commit()
    return result.rowcount > 0


async def delete_by_jmx_id(db: AsyncSession, jmx_id: int) -> None:
    await db.execute(sql_delete(JmxJava).where(JmxJava.jmx_id == jmx_id))
    await db.commit()
