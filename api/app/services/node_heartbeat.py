"""节点心跳检测服务：每 60 秒异步检查所有 enable 的 slave 节点。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import LargeBinary, String, cast, select, update

from app.core.enums import NodeStatus, NodeType
from app.core.shell_safety import build_jmeter_server_start
from app.core.ssh import SSHClient
from app.db import session as session_module
from app.models.node import Node
from app.services import config as config_service

SHANGHAI = ZoneInfo("Asia/Shanghai")
log = logging.getLogger(__name__)

_heartbeat_task: asyncio.Task | None = None

# 每轮创建独立信号量，不跨事件循环共享 asyncio 原语。
_MAX_CONCURRENT_PROBES = 5
_JMETER_SERVER_PS_CMD = "ps -ef | grep 'ApacheJMeter.jar' | grep ' -s ' | grep -v grep"
_JMETER_SERVER_START_WAIT_SECONDS = 2


@dataclass(frozen=True, slots=True)
class NodeSnapshot:
    """Plain immutable data: safe to use after the read session has closed."""

    id: int
    name: str
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    modify_time: str
    last_heartbeat: str | None


@dataclass(frozen=True, slots=True)
class HeartbeatResult:
    node: NodeSnapshot
    health_status: int
    checked_at: datetime
    cpu_usage: float | None = None
    mem_usage: float | None = None
    load_avg: float | None = None


def _now() -> datetime:
    # MySQL DATETIME and the rest of the audit fields use naive Shanghai time.
    return datetime.now(SHANGHAI).replace(tzinfo=None)


def _jmeter_server_running(output: str | None) -> bool:
    return bool(output and output != "null")


def _jmeter_server_started(output: str | None, host: str) -> bool:
    return bool(output and (host in output or "Using local port" in output))


async def _ensure_jmeter_server_running(
    ssh: SSHClient,
    node: Node | NodeSnapshot,
    *,
    slave_bin: str,
    slave_log: str,
) -> bool:
    # Validate even legacy DB values before sending any SSH command.
    start_cmd = (
        build_jmeter_server_start(slave_bin, slave_log, node.host) if slave_bin and slave_log else None
    )
    ps_output = await ssh.exec_command(_JMETER_SERVER_PS_CMD)
    if _jmeter_server_running(ps_output):
        return True
    if start_cmd is None:
        log.warning(
            "Node %s(%s) jmeter-server missing and slave paths are not configured", node.name, node.host
        )
        return False

    result = await ssh.exec_command(start_cmd)
    log.info("Heartbeat restarted jmeter-server host=%s output=%s", node.host, result)
    if _JMETER_SERVER_START_WAIT_SECONDS > 0:
        await asyncio.sleep(_JMETER_SERVER_START_WAIT_SECONDS)
    ps_after_start = await ssh.exec_command(_JMETER_SERVER_PS_CMD)
    return _jmeter_server_running(ps_after_start) or _jmeter_server_started(result, node.host)


async def _check_single_node(node: NodeSnapshot, *, slave_bin: str, slave_log: str) -> HeartbeatResult:
    """Only SSH I/O: no ORM mutation, session access or database commits."""
    try:
        ssh = SSHClient(
            host=node.host,
            port=node.port or 22,
            username=node.username,
            password=node.password,
        )
        # 1. 连通性检查
        await ssh.telnet(timeout_ms=3000)

        # 2. 采集负载
        load_line = await ssh.exec_command("cat /proc/loadavg | awk '{print $1}'")
        load_avg = float(load_line) if load_line != "null" else 0.0

        # 3. 采集内存使用率
        mem_line = await ssh.exec_command("free | awk 'NR==2{printf \"%.1f\", $3*100/$2}'")
        mem_usage = float(mem_line) if mem_line != "null" else 0.0

        # 4. 采集 CPU 使用率（取 idle 后反向计算）
        cpu_line = await ssh.exec_command("top -bn1 | grep 'Cpu(s)' | awk '{print $8}' | cut -d'%' -f1")
        try:
            cpu_idle = float(cpu_line) if cpu_line != "null" else 100.0
            cpu_usage = round(100.0 - cpu_idle, 1)
        except (ValueError, TypeError):
            cpu_usage = 0.0

        # 5. SSH 可连但 jmeter-server 不存在时，保留主动拉起行为。
        healthy = await _ensure_jmeter_server_running(ssh, node, slave_bin=slave_bin, slave_log=slave_log)
        if healthy:
            log.debug(
                "Node %s(%s) healthy: cpu=%s mem=%s load=%s",
                node.name,
                node.host,
                cpu_usage,
                mem_usage,
                load_avg,
            )
        else:
            log.info("Node %s(%s) jmeter-server missing", node.name, node.host)
        return HeartbeatResult(node, int(healthy), _now(), cpu_usage, mem_usage, load_avg)
    except Exception as exc:
        log.info("Node %s(%s) offline: %s", node.name, node.host, exc)
        # On failure preserve previous resource metrics rather than replacing them with zero.
        return HeartbeatResult(node, 0, _now())


async def _heartbeat_round() -> None:
    """Short read transaction → concurrent SSH → sequential conditional writes."""
    async with session_module.AsyncSessionLocal() as db:
        # Preserve DB-rendered timestamp tokens: SQLite CURRENT_TIMESTAMP omits
        # fractional seconds, unlike bound Python datetime values.
        result = await db.execute(
            select(
                Node,
                cast(Node.modify_time, String).label("modified_token"),
                cast(Node.last_heartbeat, String).label("heartbeat_token"),
            )
            .where(
                Node.type == NodeType.SLAVE.value,
                Node.status == NodeStatus.ENABLE.value,
            )
            .order_by(Node.id)
        )
        nodes = [
            NodeSnapshot(
                node.id,
                node.name,
                node.host,
                node.port,
                node.username,
                node.password,
                modified,
                last_heartbeat,
            )
            for node, modified, last_heartbeat in result
        ]
        if nodes:
            slave_bin = await config_service.get_value_or_default(db, "SLAVE_JMETER_BIN_HOME", "")
            slave_log = await config_service.get_value_or_default(db, "SLAVE_JMETER_LOG_HOME", "")

    if not nodes:
        return

    log.info("Heartbeat round started: %d nodes", len(nodes))
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PROBES)

    async def probe(node: NodeSnapshot) -> HeartbeatResult:
        async with semaphore:
            return await _check_single_node(node, slave_bin=slave_bin, slave_log=slave_log)

    outcomes = await asyncio.gather(*(probe(node) for node in nodes), return_exceptions=True)
    results = []
    for node, outcome in zip(nodes, outcomes, strict=True):
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
        if isinstance(outcome, HeartbeatResult):
            results.append(outcome)
        else:
            log.error("Heartbeat probe failed node_id=%s error_type=%s", node.id, type(outcome).__name__)
    if not results:
        return

    updated = 0
    async with session_module.AsyncSessionLocal() as db:
        try:
            for outcome in results:
                node = outcome.node
                values = {"health_status": outcome.health_status, "last_heartbeat": outcome.checked_at}
                if outcome.cpu_usage is not None:
                    values.update(
                        cpu_usage=outcome.cpu_usage, mem_usage=outcome.mem_usage, load_avg=outcome.load_avg
                    )
                # Atomic conditions prevent a stale probe from overwriting a user action
                # or a newer heartbeat. Only health fields are ever updated.
                stmt = (
                    update(Node)
                    .where(
                        Node.id == node.id,
                        Node.type == NodeType.SLAVE.value,
                        Node.status == NodeStatus.ENABLE.value,
                        cast(Node.host, LargeBinary) == node.host.encode("utf-8"),
                        Node.port == node.port,
                        cast(Node.username, LargeBinary) == node.username.encode("utf-8"),
                        # Credentials must match byte-for-byte even with case-insensitive MySQL collations.
                        cast(Node.password, LargeBinary) == node.password.encode("utf-8"),
                        cast(Node.modify_time, String) == node.modify_time,
                        cast(Node.last_heartbeat, String) == node.last_heartbeat,
                    )
                    .values(**values)
                    .execution_options(synchronize_session=False)
                )
                changed = await db.execute(stmt)
                updated += changed.rowcount
            await db.commit()
        except Exception as exc:
            await db.rollback()
            # SQLAlchemy exceptions can include SQL params (encrypted credentials).
            log.error("Heartbeat database write failed, rolled back: error_type=%s", type(exc).__name__)
            raise RuntimeError("心跳数据库写入失败，本轮结果已回滚，下轮重试") from None
    log.info("Heartbeat round finished: updated=%d skipped=%d", updated, len(results) - updated)


async def _heartbeat_loop() -> None:
    """后台循环：每 60 秒执行一次节点心跳检测。"""
    log.info("Node heartbeat scheduler started (interval=60s)")
    while True:
        try:
            await asyncio.sleep(60)
            await _heartbeat_round()
        except asyncio.CancelledError:
            log.info("Node heartbeat scheduler cancelled")
            break
        except Exception:
            log.exception("Heartbeat loop error, will retry")


def start_heartbeat_scheduler() -> asyncio.Task:
    global _heartbeat_task
    if _heartbeat_task is not None and not _heartbeat_task.done():
        return _heartbeat_task
    _heartbeat_task = asyncio.create_task(_heartbeat_loop())
    return _heartbeat_task


async def stop_heartbeat_scheduler() -> None:
    global _heartbeat_task
    if _heartbeat_task:
        _heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await _heartbeat_task
        _heartbeat_task = None
