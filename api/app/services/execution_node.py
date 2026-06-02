"""Execution node lease service."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import node as node_crud
from app.models.execution_node import ExecutionNode
from app.models.node import Node

log = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")

STATUS_LEASED = "leased"
STATUS_RELEASED = "released"
STATUS_RELEASE_FAILED = "release_failed"
STATUS_LOST = "lost"
ACTIVE_STATUSES = (STATUS_LEASED,)


def _now() -> datetime:
    return datetime.now(SHANGHAI).replace(tzinfo=None)


async def active_node_ids(db: AsyncSession) -> set[int]:
    rows = (
        await db.execute(
            select(ExecutionNode.node_id).where(ExecutionNode.status.in_(ACTIVE_STATUSES))
        )
    ).scalars().all()
    return {int(node_id) for node_id in rows if node_id}


async def active_hosts_by_report_ids(db: AsyncSession, report_ids: list[int]) -> dict[int, list[str]]:
    if not report_ids:
        return {}
    rows = (
        await db.execute(
            select(ExecutionNode.report_id, ExecutionNode.node_host)
            .where(
                ExecutionNode.report_id.in_(report_ids),
                ExecutionNode.status.in_(ACTIVE_STATUSES),
            )
            .order_by(ExecutionNode.id.asc())
        )
    ).all()
    result: dict[int, list[str]] = {}
    for report_id, host in rows:
        if not host:
            continue
        result.setdefault(int(report_id), []).append(host)
    return result


async def list_available_slaves(
    db: AsyncSession,
    *,
    region: str | None = None,
    ignore_health: bool = False,
) -> list[Node]:
    slaves = await node_crud.list_enable_slaves(db, region=region)
    leased_ids = await active_node_ids(db)
    available: list[Node] = []
    for node in slaves:
        if node.id in leased_ids:
            continue
        if not ignore_health and node.health_status != 1:
            continue
        available.append(node)
    return available


async def count_available_slaves(
    db: AsyncSession,
    *,
    region: str | None = None,
    ignore_health: bool = False,
) -> int:
    return len(await list_available_slaves(db, region=region, ignore_health=ignore_health))


async def lease_nodes(
    db: AsyncSession,
    *,
    report_id: int,
    test_case_id: int,
    nodes: list[Node],
    region: str,
    execution_run_id: int = 0,
) -> list[ExecutionNode]:
    if not nodes:
        return []
    node_ids = [node.id for node in nodes if node.id is not None]
    await db.execute(select(Node.id).where(Node.id.in_(node_ids)).with_for_update())
    leased_ids = set(
        (
            await db.execute(
                select(ExecutionNode.node_id).where(
                    ExecutionNode.node_id.in_(node_ids),
                    ExecutionNode.status.in_(ACTIVE_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    conflicts = [node for node in nodes if node.id in leased_ids]
    if conflicts:
        hosts = ",".join(node.host for node in conflicts)
        raise RuntimeError(f"压力机已被占用: {hosts}")

    now = _now()
    leases: list[ExecutionNode] = []
    for node in nodes:
        lease = ExecutionNode(
            report_id=report_id,
            test_case_id=test_case_id,
            execution_run_id=execution_run_id,
            node_id=node.id,
            node_host=node.host,
            region=region,
            status=STATUS_LEASED,
            leased_at=now,
        )
        db.add(lease)
        leases.append(lease)
    await db.commit()
    log.info(
        "执行节点租约已创建: report_id=%s testcase_id=%s hosts=%s",
        report_id,
        test_case_id,
        ",".join(node.host for node in nodes),
    )
    return leases


async def release_by_report(
    db: AsyncSession,
    report_id: int,
    *,
    message: str = "",
    status: str = STATUS_RELEASED,
) -> int:
    rows = list(
        (
            await db.execute(
                select(ExecutionNode).where(
                    ExecutionNode.report_id == report_id,
                    ExecutionNode.status.in_(ACTIVE_STATUSES),
                )
            )
        ).scalars().all()
    )
    if not rows:
        return 0
    now = _now()
    for lease in rows:
        lease.status = status
        lease.released_at = now
        lease.release_message = message
    await db.commit()
    log.info("执行节点租约已释放: report_id=%s count=%s status=%s", report_id, len(rows), status)
    return len(rows)


async def mark_active_lost(db: AsyncSession, message: str) -> int:
    rows = list(
        (
            await db.execute(
                select(ExecutionNode).where(ExecutionNode.status.in_(ACTIVE_STATUSES))
            )
        ).scalars().all()
    )
    if not rows:
        return 0
    now = _now()
    for lease in rows:
        lease.status = STATUS_LOST
        lease.released_at = now
        lease.release_message = message
    await db.commit()
    log.info("执行节点租约启动自愈标记 lost: count=%s", len(rows))
    return len(rows)
