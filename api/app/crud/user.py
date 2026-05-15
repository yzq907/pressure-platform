"""User 的 CRUD 操作。纯 DB 访问，无业务逻辑。对齐 Java UserMapper + user.xml。"""

from __future__ import annotations

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


async def get_by_id(db: AsyncSession, id: int) -> User | None:
    return await db.get(User, id)


async def get_by_username(db: AsyncSession, username: str) -> User | None:
    stmt = select(User).where(User.username == username)
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_by_token(db: AsyncSession, token: str) -> User | None:
    stmt = select(User).where(User.token == token)
    return (await db.execute(stmt)).scalar_one_or_none()


async def add(db: AsyncSession, user: User) -> User:
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def update(db: AsyncSession, user: User) -> bool:
    """user 必须是托管/已合并到 session 的对象。返回是否成功提交。"""
    await db.commit()
    return True


async def delete(db: AsyncSession, id: int) -> bool:
    result = await db.execute(sql_delete(User).where(User.id == id))
    await db.commit()
    return result.rowcount > 0


async def count(
    db: AsyncSession,
    username: str | None = None,
    real_name: str | None = None,
) -> int:
    """对齐 Java SQL：username/real_name 不为 None 就拼 LIKE %xxx%"""
    stmt = select(func.count()).select_from(User)
    if username is not None:
        stmt = stmt.where(User.username.like(f"%{username}%"))
    if real_name is not None:
        stmt = stmt.where(User.real_name.like(f"%{real_name}%"))
    return (await db.execute(stmt)).scalar_one() or 0


async def list_users(
    db: AsyncSession,
    username: str | None,
    real_name: str | None,
    offset: int,
    limit: int,
) -> list[User]:
    stmt = select(User)
    if username is not None:
        stmt = stmt.where(User.username.like(f"%{username}%"))
    if real_name is not None:
        stmt = stmt.where(User.real_name.like(f"%{real_name}%"))
    stmt = stmt.order_by(User.effect_time.desc()).offset(offset).limit(limit)
    return list((await db.execute(stmt)).scalars().all())
