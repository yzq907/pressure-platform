"""Node 的 CRUD 操作，对齐 Java NodeMapper + node.xml。"""

from __future__ import annotations

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.node import Node


async def get_by_id(db: AsyncSession, id: int) -> Node | None:
    return await db.get(Node, id)


async def get_by_host(db: AsyncSession, host: str) -> Node | None:
    """Java 端返回 List，但实际只在判存使用，所以这里返回单条即可"""
    stmt = select(Node).where(Node.host == host).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()


async def add(db: AsyncSession, obj: Node) -> Node:
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def update(db: AsyncSession, obj: Node) -> bool:
    await db.commit()
    return True


async def delete(db: AsyncSession, id: int) -> bool:
    result = await db.execute(sql_delete(Node).where(Node.id == id))
    await db.commit()
    return result.rowcount > 0


async def count(
    db: AsyncSession,
    name: str | None = None,
    host: str | None = None,
) -> int:
    """对齐 Java：name LIKE %?%；host 精确匹配"""
    stmt = select(func.count()).select_from(Node)
    if name is not None:
        stmt = stmt.where(Node.name.like(f"%{name}%"))
    if host is not None:
        stmt = stmt.where(Node.host == host)
    return (await db.execute(stmt)).scalar_one() or 0


async def list_nodes(
    db: AsyncSession,
    name: str | None,
    host: str | None,
    offset: int,
    limit: int,
) -> list[Node]:
    stmt = select(Node)
    if name is not None:
        stmt = stmt.where(Node.name.like(f"%{name}%"))
    if host is not None:
        stmt = stmt.where(Node.host == host)
    stmt = stmt.order_by(Node.modify_time.desc()).offset(offset).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def list_enable_slaves(db: AsyncSession) -> list[Node]:
    """获取所有启用中的 slave 节点，给 Phase 5 的 JMeter 分布式调度用"""
    from app.core.enums import NodeStatus, NodeType

    stmt = select(Node).where(
        Node.type == NodeType.SLAVE.value,
        Node.status == NodeStatus.ENABLE.value,
    )
    return list((await db.execute(stmt)).scalars().all())
