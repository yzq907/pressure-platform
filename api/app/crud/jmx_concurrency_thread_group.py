"""mysterious_jmx_concurrency_thread_group CRUD。"""

from __future__ import annotations

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.jmx_concurrency_thread_group import JmxConcurrencyThreadGroup


async def get_by_id(db: AsyncSession, id: int) -> JmxConcurrencyThreadGroup | None:
    return await db.get(JmxConcurrencyThreadGroup, id)


async def get_by_jmx_id(db: AsyncSession, jmx_id: int) -> JmxConcurrencyThreadGroup | None:
    stmt = select(JmxConcurrencyThreadGroup).where(JmxConcurrencyThreadGroup.jmx_id == jmx_id).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()


async def add(db: AsyncSession, obj: JmxConcurrencyThreadGroup) -> JmxConcurrencyThreadGroup:
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def update(db: AsyncSession, obj: JmxConcurrencyThreadGroup) -> bool:
    await db.commit()
    return True


async def delete(db: AsyncSession, id: int) -> bool:
    result = await db.execute(sql_delete(JmxConcurrencyThreadGroup).where(JmxConcurrencyThreadGroup.id == id))
    await db.commit()
    return result.rowcount > 0


async def delete_by_jmx_id(db: AsyncSession, jmx_id: int) -> bool:
    result = await db.execute(
        sql_delete(JmxConcurrencyThreadGroup).where(JmxConcurrencyThreadGroup.jmx_id == jmx_id)
    )
    await db.commit()
    return result.rowcount > 0
