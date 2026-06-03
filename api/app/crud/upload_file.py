"""上传接口文件 CRUD 操作。"""

from __future__ import annotations

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.upload_file import UploadFileResource


async def get_by_id(db: AsyncSession, id: int) -> UploadFileResource | None:
    return await db.get(UploadFileResource, id)


async def get_by_test_case_id(db: AsyncSession, test_case_id: int) -> list[UploadFileResource]:
    stmt = select(UploadFileResource).where(UploadFileResource.test_case_id == test_case_id)
    return list((await db.execute(stmt)).scalars().all())


async def get_exist_list(
    db: AsyncSession,
    test_case_id: int,
    src_name: str,
    file_dir: str,
) -> list[UploadFileResource]:
    stmt = select(UploadFileResource).where(
        UploadFileResource.test_case_id == test_case_id,
        UploadFileResource.src_name == src_name,
        UploadFileResource.file_dir == file_dir,
    )
    return list((await db.execute(stmt)).scalars().all())


async def add(db: AsyncSession, obj: UploadFileResource) -> UploadFileResource:
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def update(db: AsyncSession, obj: UploadFileResource) -> bool:
    await db.commit()
    return True


async def delete(db: AsyncSession, id: int) -> bool:
    result = await db.execute(sql_delete(UploadFileResource).where(UploadFileResource.id == id))
    await db.commit()
    return result.rowcount > 0


async def count(
    db: AsyncSession,
    src_name: str | None = None,
    test_case_id: int | None = None,
) -> int:
    stmt = select(func.count()).select_from(UploadFileResource)
    if src_name is not None:
        stmt = stmt.where(UploadFileResource.src_name.like(f"%{src_name}%"))
    if test_case_id is not None:
        stmt = stmt.where(UploadFileResource.test_case_id == test_case_id)
    return (await db.execute(stmt)).scalar_one() or 0


async def list_upload_files(
    db: AsyncSession,
    src_name: str | None,
    test_case_id: int | None,
    offset: int,
    limit: int,
) -> list[UploadFileResource]:
    stmt = select(UploadFileResource)
    if src_name is not None:
        stmt = stmt.where(UploadFileResource.src_name.like(f"%{src_name}%"))
    if test_case_id is not None:
        stmt = stmt.where(UploadFileResource.test_case_id == test_case_id)
    stmt = stmt.order_by(UploadFileResource.modify_time.desc()).offset(offset).limit(limit)
    return list((await db.execute(stmt)).scalars().all())
