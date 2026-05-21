"""AuditLog CRUD 操作。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog


async def add(db: AsyncSession, obj: AuditLog) -> AuditLog:
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def get_by_id(db: AsyncSession, id: int) -> AuditLog | None:
    return await db.get(AuditLog, id)


async def list_logs(
    db: AsyncSession,
    username: str | None,
    action: str | None,
    resource_type: str | None,
    resource_id: int | None,
    offset: int,
    limit: int,
) -> list[AuditLog]:
    stmt = select(AuditLog)
    if username:
        stmt = stmt.where(AuditLog.username.like(f"%{username}%"))
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if resource_type:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
    if resource_id is not None:
        stmt = stmt.where(AuditLog.resource_id == resource_id)
    stmt = stmt.order_by(AuditLog.create_time.desc()).offset(offset).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def count(
    db: AsyncSession,
    username: str | None,
    action: str | None,
    resource_type: str | None,
    resource_id: int | None,
) -> int:
    stmt = select(func.count()).select_from(AuditLog)
    if username:
        stmt = stmt.where(AuditLog.username.like(f"%{username}%"))
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if resource_type:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
    if resource_id is not None:
        stmt = stmt.where(AuditLog.resource_id == resource_id)
    result = await db.execute(stmt)
    return result.scalar_one() or 0
