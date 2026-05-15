"""mysterious_jmx_http CRUD。"""

from __future__ import annotations

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.jmx_http import JmxHttp


async def get_by_id(db: AsyncSession, id: int) -> JmxHttp | None:
    return await db.get(JmxHttp, id)


async def get_by_jmx_id(db: AsyncSession, jmx_id: int) -> JmxHttp | None:
    stmt = select(JmxHttp).where(JmxHttp.jmx_id == jmx_id).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()


async def add(db: AsyncSession, obj: JmxHttp) -> JmxHttp:
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def update(db: AsyncSession, obj: JmxHttp) -> bool:
    await db.commit()
    return True


async def delete(db: AsyncSession, id: int) -> bool:
    result = await db.execute(sql_delete(JmxHttp).where(JmxHttp.id == id))
    await db.commit()
    return result.rowcount > 0


async def delete_by_jmx_id(db: AsyncSession, jmx_id: int) -> bool:
    result = await db.execute(sql_delete(JmxHttp).where(JmxHttp.jmx_id == jmx_id))
    await db.commit()
    return result.rowcount > 0
