"""mysterious_jmx_http_header CRUD。"""

from __future__ import annotations

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.jmx_http_header import JmxHttpHeader


async def get_by_id(db: AsyncSession, id: int) -> JmxHttpHeader | None:
    return await db.get(JmxHttpHeader, id)


async def get_by_http_id(db: AsyncSession, http_id: int) -> list[JmxHttpHeader]:
    stmt = select(JmxHttpHeader).where(JmxHttpHeader.http_id == http_id)
    return list((await db.execute(stmt)).scalars().all())


async def get_by_jmx_id(db: AsyncSession, jmx_id: int) -> list[JmxHttpHeader]:
    stmt = select(JmxHttpHeader).where(JmxHttpHeader.jmx_id == jmx_id)
    return list((await db.execute(stmt)).scalars().all())


async def add(db: AsyncSession, obj: JmxHttpHeader) -> JmxHttpHeader:
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def batch_add(db: AsyncSession, objs: list[JmxHttpHeader]) -> None:
    for o in objs:
        db.add(o)
    await db.commit()


async def update(db: AsyncSession, obj: JmxHttpHeader) -> bool:
    await db.commit()
    return True


async def delete(db: AsyncSession, id: int) -> bool:
    result = await db.execute(sql_delete(JmxHttpHeader).where(JmxHttpHeader.id == id))
    await db.commit()
    return result.rowcount > 0


async def delete_by_http_id(db: AsyncSession, http_id: int) -> None:
    await db.execute(sql_delete(JmxHttpHeader).where(JmxHttpHeader.http_id == http_id))
    await db.commit()


async def delete_by_jmx_id(db: AsyncSession, jmx_id: int) -> None:
    await db.execute(sql_delete(JmxHttpHeader).where(JmxHttpHeader.jmx_id == jmx_id))
    await db.commit()
