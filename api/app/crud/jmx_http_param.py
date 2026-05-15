"""mysterious_jmx_http_param CRUD。"""

from __future__ import annotations

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.jmx_http_param import JmxHttpParam


async def get_by_id(db: AsyncSession, id: int) -> JmxHttpParam | None:
    return await db.get(JmxHttpParam, id)


async def get_by_http_id(db: AsyncSession, http_id: int) -> list[JmxHttpParam]:
    stmt = select(JmxHttpParam).where(JmxHttpParam.http_id == http_id)
    return list((await db.execute(stmt)).scalars().all())


async def get_by_jmx_id(db: AsyncSession, jmx_id: int) -> list[JmxHttpParam]:
    stmt = select(JmxHttpParam).where(JmxHttpParam.jmx_id == jmx_id)
    return list((await db.execute(stmt)).scalars().all())


async def add(db: AsyncSession, obj: JmxHttpParam) -> JmxHttpParam:
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def batch_add(db: AsyncSession, objs: list[JmxHttpParam]) -> None:
    for o in objs:
        db.add(o)
    await db.commit()


async def update(db: AsyncSession, obj: JmxHttpParam) -> bool:
    await db.commit()
    return True


async def delete(db: AsyncSession, id: int) -> bool:
    result = await db.execute(sql_delete(JmxHttpParam).where(JmxHttpParam.id == id))
    await db.commit()
    return result.rowcount > 0


async def delete_by_http_id(db: AsyncSession, http_id: int) -> None:
    await db.execute(sql_delete(JmxHttpParam).where(JmxHttpParam.http_id == http_id))
    await db.commit()


async def delete_by_jmx_id(db: AsyncSession, jmx_id: int) -> None:
    await db.execute(sql_delete(JmxHttpParam).where(JmxHttpParam.jmx_id == jmx_id))
    await db.commit()
