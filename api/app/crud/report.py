"""Report CRUD 操作，对齐 Java ReportMapper + report.xml。"""

from __future__ import annotations

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.report import Report


async def get_by_id(db: AsyncSession, id: int) -> Report | None:
    return await db.get(Report, id)


async def add(db: AsyncSession, obj: Report) -> Report:
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def update(db: AsyncSession, obj: Report) -> bool:
    await db.commit()
    return True


async def update_status(db: AsyncSession, id: int, status: int) -> bool:
    obj = await db.get(Report, id)
    if obj is None:
        return False
    obj.status = status
    await db.commit()
    return True


async def delete(db: AsyncSession, id: int) -> bool:
    result = await db.execute(sql_delete(Report).where(Report.id == id))
    await db.commit()
    return result.rowcount > 0


async def count(
    db: AsyncSession,
    name: str | None = None,
    test_case_id: int | None = None,
) -> int:
    stmt = select(func.count()).select_from(Report)
    if name is not None:
        stmt = stmt.where(Report.name.like(f"%{name}%"))
    if test_case_id is not None:
        stmt = stmt.where(Report.test_case_id == test_case_id)
    return (await db.execute(stmt)).scalar_one() or 0


async def list_reports(
    db: AsyncSession,
    name: str | None,
    offset: int,
    limit: int,
) -> list[Report]:
    stmt = select(Report)
    if name is not None:
        stmt = stmt.where(Report.name.like(f"%{name}%"))
    stmt = stmt.order_by(Report.modify_time.desc()).offset(offset).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def list_by_test_case(
    db: AsyncSession,
    name: str | None,
    test_case_id: int | None,
    offset: int,
    limit: int,
) -> list[Report]:
    stmt = select(Report)
    if name is not None:
        stmt = stmt.where(Report.name.like(f"%{name}%"))
    if test_case_id is not None:
        stmt = stmt.where(Report.test_case_id == test_case_id)
    stmt = stmt.order_by(Report.modify_time.desc()).offset(offset).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def get_debug_reports_by_test_case_id(
    db: AsyncSession,
    test_case_id: int,
    exec_type: int | None,
    limit: int,
) -> list[Report]:
    """对齐 Java getDebugReportListByTestCaseId(testCaseId, execType, limit)。
    exec_type=None 表示不限定（debug + run 都查）；用于 getJMeterResult。"""
    stmt = select(Report).where(Report.test_case_id == test_case_id)
    if exec_type is not None:
        stmt = stmt.where(Report.exec_type == exec_type)
    stmt = stmt.order_by(Report.modify_time.desc()).offset(0).limit(limit)
    return list((await db.execute(stmt)).scalars().all())
