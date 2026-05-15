"""Config 的 CRUD 操作，对齐 Java ConfigMapper + config.xml。"""

from __future__ import annotations

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.config import Config


async def get_by_id(db: AsyncSession, id: int) -> Config | None:
    return await db.get(Config, id)


async def get_by_key(db: AsyncSession, key: str) -> Config | None:
    stmt = select(Config).where(Config.config_key == key)
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_value(db: AsyncSession, key: str) -> str | None:
    stmt = select(Config.config_value).where(Config.config_key == key)
    return (await db.execute(stmt)).scalar_one_or_none()


async def add(db: AsyncSession, obj: Config) -> Config:
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def update(db: AsyncSession, obj: Config) -> bool:
    await db.commit()
    return True


async def delete(db: AsyncSession, id: int) -> bool:
    result = await db.execute(sql_delete(Config).where(Config.id == id))
    await db.commit()
    return result.rowcount > 0


async def count(db: AsyncSession, config_key: str | None = None) -> int:
    stmt = select(func.count()).select_from(Config)
    if config_key is not None:
        stmt = stmt.where(Config.config_key.like(f"%{config_key}%"))
    return (await db.execute(stmt)).scalar_one() or 0


async def list_configs(
    db: AsyncSession,
    config_key: str | None,
    offset: int,
    limit: int,
) -> list[Config]:
    stmt = select(Config)
    if config_key is not None:
        stmt = stmt.where(Config.config_key.like(f"%{config_key}%"))
    stmt = stmt.order_by(Config.modify_time.desc()).offset(offset).limit(limit)
    return list((await db.execute(stmt)).scalars().all())
