"""User session token CRUD."""

from __future__ import annotations

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_session import UserSession


async def get_by_token(db: AsyncSession, token: str) -> UserSession | None:
    stmt = select(UserSession).where(UserSession.token == token)
    return (await db.execute(stmt)).scalar_one_or_none()


async def add(db: AsyncSession, session: UserSession) -> UserSession:
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def delete_by_user_id(db: AsyncSession, user_id: int) -> int:
    result = await db.execute(sql_delete(UserSession).where(UserSession.user_id == user_id))
    await db.commit()
    return result.rowcount or 0
