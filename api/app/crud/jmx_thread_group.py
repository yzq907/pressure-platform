"""mysterious_jmx_thread_group CRUD。"""

from __future__ import annotations

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.jmx_thread_group import JmxThreadGroup


async def get_by_id(db: AsyncSession, id: int) -> JmxThreadGroup | None:
    return await db.get(JmxThreadGroup, id)


async def get_by_jmx_id(db: AsyncSession, jmx_id: int) -> JmxThreadGroup | None:
    stmt = select(JmxThreadGroup).where(JmxThreadGroup.jmx_id == jmx_id).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()


async def add(db: AsyncSession, obj: JmxThreadGroup) -> JmxThreadGroup:
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def update(db: AsyncSession, obj: JmxThreadGroup) -> bool:
    await db.commit()
    return True


async def delete(db: AsyncSession, id: int) -> bool:
    result = await db.execute(sql_delete(JmxThreadGroup).where(JmxThreadGroup.id == id))
    await db.commit()
    return result.rowcount > 0


async def delete_by_jmx_id(db: AsyncSession, jmx_id: int) -> bool:
    result = await db.execute(
        sql_delete(JmxThreadGroup).where(JmxThreadGroup.jmx_id == jmx_id)
    )
    await db.commit()
    return result.rowcount > 0
