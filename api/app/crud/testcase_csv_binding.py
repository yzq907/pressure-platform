"""用例公共 CSV 绑定 CRUD。"""

from __future__ import annotations

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.testcase_csv_binding import TestcaseCsvBinding


async def get_by_id(db: AsyncSession, id: int) -> TestcaseCsvBinding | None:
    return await db.get(TestcaseCsvBinding, id)


async def get_by_test_case_id(db: AsyncSession, test_case_id: int) -> list[TestcaseCsvBinding]:
    stmt = select(TestcaseCsvBinding).where(TestcaseCsvBinding.test_case_id == test_case_id)
    return list((await db.execute(stmt)).scalars().all())


async def get_by_case_filename(
    db: AsyncSession,
    test_case_id: int,
    filename: str,
) -> TestcaseCsvBinding | None:
    stmt = select(TestcaseCsvBinding).where(
        TestcaseCsvBinding.test_case_id == test_case_id,
        TestcaseCsvBinding.filename == filename,
    ).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()


async def add(db: AsyncSession, obj: TestcaseCsvBinding) -> TestcaseCsvBinding:
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def update(db: AsyncSession, obj: TestcaseCsvBinding) -> bool:
    await db.commit()
    return True


async def delete(db: AsyncSession, id: int) -> bool:
    result = await db.execute(sql_delete(TestcaseCsvBinding).where(TestcaseCsvBinding.id == id))
    await db.commit()
    return result.rowcount > 0


async def count_by_filename(db: AsyncSession, filename: str) -> int:
    stmt = select(func.count()).select_from(TestcaseCsvBinding).where(TestcaseCsvBinding.filename == filename)
    return (await db.execute(stmt)).scalar_one() or 0


async def count_by_filenames(db: AsyncSession, filenames: list[str]) -> dict[str, int]:
    if not filenames:
        return {}
    stmt = (
        select(TestcaseCsvBinding.filename, func.count())
        .where(TestcaseCsvBinding.filename.in_(filenames))
        .group_by(TestcaseCsvBinding.filename)
    )
    return {filename: count for filename, count in (await db.execute(stmt)).all()}
