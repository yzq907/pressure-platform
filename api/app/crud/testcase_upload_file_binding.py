"""用例公共上传接口文件绑定 CRUD。"""

from __future__ import annotations

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.testcase_upload_file_binding import TestcaseUploadFileBinding


async def get_by_id(db: AsyncSession, id: int) -> TestcaseUploadFileBinding | None:
    return await db.get(TestcaseUploadFileBinding, id)


async def get_by_test_case_id(db: AsyncSession, test_case_id: int) -> list[TestcaseUploadFileBinding]:
    stmt = select(TestcaseUploadFileBinding).where(TestcaseUploadFileBinding.test_case_id == test_case_id)
    return list((await db.execute(stmt)).scalars().all())


async def get_by_case_filename(
    db: AsyncSession,
    test_case_id: int,
    filename: str,
) -> TestcaseUploadFileBinding | None:
    stmt = select(TestcaseUploadFileBinding).where(
        TestcaseUploadFileBinding.test_case_id == test_case_id,
        TestcaseUploadFileBinding.filename == filename,
    ).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()


async def add(db: AsyncSession, obj: TestcaseUploadFileBinding) -> TestcaseUploadFileBinding:
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def update(db: AsyncSession, obj: TestcaseUploadFileBinding) -> bool:
    await db.commit()
    return True


async def delete(db: AsyncSession, id: int) -> bool:
    result = await db.execute(sql_delete(TestcaseUploadFileBinding).where(TestcaseUploadFileBinding.id == id))
    await db.commit()
    return result.rowcount > 0


async def count_by_filename(db: AsyncSession, filename: str) -> int:
    stmt = select(func.count()).select_from(TestcaseUploadFileBinding).where(
        TestcaseUploadFileBinding.filename == filename
    )
    return (await db.execute(stmt)).scalar_one() or 0


async def count_by_filenames(db: AsyncSession, filenames: list[str]) -> dict[str, int]:
    if not filenames:
        return {}
    stmt = (
        select(TestcaseUploadFileBinding.filename, func.count())
        .where(TestcaseUploadFileBinding.filename.in_(filenames))
        .group_by(TestcaseUploadFileBinding.filename)
    )
    return {filename: count for filename, count in (await db.execute(stmt)).all()}
