"""mysterious_jmx_stepping_thread_group CRUD。"""

from __future__ import annotations

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.jmx_stepping_thread_group import JmxSteppingThreadGroup


async def get_by_id(db: AsyncSession, id: int) -> JmxSteppingThreadGroup | None:
    return await db.get(JmxSteppingThreadGroup, id)


async def get_by_jmx_id(db: AsyncSession, jmx_id: int) -> JmxSteppingThreadGroup | None:
    stmt = select(JmxSteppingThreadGroup).where(JmxSteppingThreadGroup.jmx_id == jmx_id).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()


async def add(db: AsyncSession, obj: JmxSteppingThreadGroup) -> JmxSteppingThreadGroup:
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def update(db: AsyncSession, obj: JmxSteppingThreadGroup) -> bool:
    await db.commit()
    return True


async def delete(db: AsyncSession, id: int) -> bool:
    result = await db.execute(sql_delete(JmxSteppingThreadGroup).where(JmxSteppingThreadGroup.id == id))
    await db.commit()
    return result.rowcount > 0


async def delete_by_jmx_id(db: AsyncSession, jmx_id: int) -> bool:
    result = await db.execute(
        sql_delete(JmxSteppingThreadGroup).where(JmxSteppingThreadGroup.jmx_id == jmx_id)
    )
    await db.commit()
    return result.rowcount > 0
