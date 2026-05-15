"""Csv CRUD 操作，对齐 Java CsvMapper + csv.xml。"""

from __future__ import annotations

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.csv import Csv


async def get_by_id(db: AsyncSession, id: int) -> Csv | None:
    return await db.get(Csv, id)


async def get_by_test_case_id(db: AsyncSession, test_case_id: int) -> list[Csv]:
    """一个用例下可以有多个 CSV，返回列表"""
    stmt = select(Csv).where(Csv.test_case_id == test_case_id)
    return list((await db.execute(stmt)).scalars().all())


async def get_exist_list(
    db: AsyncSession, test_case_id: int, src_name: str, csv_dir: str
) -> list[Csv]:
    """判存：是否已有同 name + path 的 CSV"""
    stmt = select(Csv).where(
        Csv.test_case_id == test_case_id,
        Csv.src_name == src_name,
        Csv.csv_dir == csv_dir,
    )
    return list((await db.execute(stmt)).scalars().all())


async def add(db: AsyncSession, obj: Csv) -> Csv:
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def update(db: AsyncSession, obj: Csv) -> bool:
    await db.commit()
    return True


async def delete(db: AsyncSession, id: int) -> bool:
    result = await db.execute(sql_delete(Csv).where(Csv.id == id))
    await db.commit()
    return result.rowcount > 0


async def count(
    db: AsyncSession,
    src_name: str | None = None,
    test_case_id: int | None = None,
) -> int:
    stmt = select(func.count()).select_from(Csv)
    if src_name is not None:
        stmt = stmt.where(Csv.src_name.like(f"%{src_name}%"))
    if test_case_id is not None:
        stmt = stmt.where(Csv.test_case_id == test_case_id)
    return (await db.execute(stmt)).scalar_one() or 0


async def list_csvs(
    db: AsyncSession,
    src_name: str | None,
    test_case_id: int | None,
    offset: int,
    limit: int,
) -> list[Csv]:
    stmt = select(Csv)
    if src_name is not None:
        stmt = stmt.where(Csv.src_name.like(f"%{src_name}%"))
    if test_case_id is not None:
        stmt = stmt.where(Csv.test_case_id == test_case_id)
    stmt = stmt.order_by(Csv.modify_time.desc()).offset(offset).limit(limit)
    return list((await db.execute(stmt)).scalars().all())
