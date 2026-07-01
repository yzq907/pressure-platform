"""公共 CSV 资源 CRUD。"""

from __future__ import annotations

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.csv_resource import CsvResource


async def get_by_id(db: AsyncSession, id: int) -> CsvResource | None:
    return await db.get(CsvResource, id)


async def get_by_filename(db: AsyncSession, filename: str) -> CsvResource | None:
    stmt = select(CsvResource).where(CsvResource.filename == filename).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()


async def add(db: AsyncSession, obj: CsvResource) -> CsvResource:
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def update(db: AsyncSession, obj: CsvResource) -> bool:
    await db.commit()
    return True


async def delete_by_filename(db: AsyncSession, filename: str) -> bool:
    result = await db.execute(sql_delete(CsvResource).where(CsvResource.filename == filename))
    await db.commit()
    return result.rowcount > 0


async def count(db: AsyncSession, filename: str | None = None, file_type: str | None = None) -> int:
    stmt = select(func.count()).select_from(CsvResource)
    if filename:
        stmt = stmt.where(CsvResource.filename.like(f"%{filename}%"))
    if file_type:
        stmt = stmt.where(CsvResource.file_type == file_type)
    return (await db.execute(stmt)).scalar_one() or 0


async def list_resources(
    db: AsyncSession,
    filename: str | None,
    file_type: str | None,
    offset: int,
    limit: int,
) -> list[CsvResource]:
    stmt = select(CsvResource)
    if filename:
        stmt = stmt.where(CsvResource.filename.like(f"%{filename}%"))
    if file_type:
        stmt = stmt.where(CsvResource.file_type == file_type)
    stmt = stmt.order_by(CsvResource.modify_time.desc()).offset(offset).limit(limit)
    return list((await db.execute(stmt)).scalars().all())
