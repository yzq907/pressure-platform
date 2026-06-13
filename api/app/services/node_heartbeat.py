"""节点心跳检测服务：每 60 秒异步检查所有 enable 的 slave 节点。"""

from __future__ import annotations

import asyncio
import logging
import shlex
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ssh import SSHClient
from app.db.session import AsyncSessionLocal
from app.models.node import Node
from app.services import config as config_service

SHANGHAI = ZoneInfo("Asia/Shanghai")
log = logging.getLogger(__name__)

_heartbeat_task: asyncio.Task | None = None

# 并发控制信号量，最多同时 5 个 SSH 连接
_SEMAPHORE = asyncio.Semaphore(5)
_JMETER_SERVER_PS_CMD = "ps -ef | grep 'ApacheJMeter.jar' | grep ' -s ' | grep -v grep"
_JMETER_SERVER_START_WAIT_SECONDS = 2


def _now() -> datetime:
    return datetime.now(SHANGHAI)


def _jmeter_server_running(output: str | None) -> bool:
    return bool(output and output != "null")


def _jmeter_server_started(output: str | None, host: str) -> bool:
    return bool(output and (host in output or "Using local port" in output))


async def _ensure_jmeter_server_running(
    ssh: SSHClient,
    node: Node,
    *,
    slave_bin: str,
    slave_log: str,
) -> bool:
    ps_output = await ssh.exec_command(_JMETER_SERVER_PS_CMD)
    if _jmeter_server_running(ps_output):
        return True
    if not slave_bin or not slave_log:
        log.warning("Node %s(%s) jmeter-server missing and slave paths are not configured", node.name, node.host)
        return False

    slave_bin = slave_bin.rstrip("/")
    slave_log = slave_log.rstrip("/")
    start_cmd = (
        f"mkdir -p {shlex.quote(slave_log)} && cd {shlex.quote(slave_log)} && "
        f"nohup {shlex.quote(f'{slave_bin}/jmeter-server')} "
        f"-Djava.rmi.server.hostname={shlex.quote(node.host)} "
        f"> jmeter-server.log 2>&1 & echo $!"
    )
    result = await ssh.exec_command(start_cmd)
    log.info("Heartbeat restarted jmeter-server host=%s output=%s", node.host, result)
    if _JMETER_SERVER_START_WAIT_SECONDS > 0:
        await asyncio.sleep(_JMETER_SERVER_START_WAIT_SECONDS)
    ps_after_start = await ssh.exec_command(_JMETER_SERVER_PS_CMD)
    return _jmeter_server_running(ps_after_start) or _jmeter_server_started(result, node.host)


async def _check_single_node(db: AsyncSession, node: Node, *, slave_bin: str, slave_log: str) -> None:
    """检查单个节点健康状态并更新 DB。"""
    async with _SEMAPHORE:
        ssh = SSHClient(
            host=node.host,
            port=node.port or 22,
            username=node.username,
            password=node.password,
        )
        try:
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

            # 5. 对压力机来说，SSH 可连但 jmeter-server 进程不存在时主动拉起；拉起失败才判为不健康。
            node.health_status = 1 if await _ensure_jmeter_server_running(
                ssh,
                node,
                slave_bin=slave_bin,
                slave_log=slave_log,
            ) else 0
            node.last_heartbeat = _now()
            node.load_avg = load_avg
            node.mem_usage = mem_usage
            node.cpu_usage = cpu_usage
            if node.health_status == 1:
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
        except Exception as e:
            node.health_status = 0
            node.last_heartbeat = _now()
            log.info("Node %s(%s) offline: %s", node.name, node.host, e)
        finally:
            await db.commit()


async def _heartbeat_round(db: AsyncSession) -> None:
    """执行一轮心跳检测。"""
    from app.core.enums import NodeType, NodeStatus

    stmt = select(Node).where(
        Node.type == NodeType.SLAVE.value,
        Node.status == NodeStatus.ENABLE.value,
    )
    result = await db.execute(stmt)
    nodes = list(result.scalars().all())

    if not nodes:
        return

    slave_bin = await config_service.get_value_or_default(db, "SLAVE_JMETER_BIN_HOME", "")
    slave_log = await config_service.get_value_or_default(db, "SLAVE_JMETER_LOG_HOME", "")
    log.info("Heartbeat round started: %d nodes", len(nodes))
    tasks = [_check_single_node(db, node, slave_bin=slave_bin, slave_log=slave_log) for node in nodes]
    await asyncio.gather(*tasks, return_exceptions=True)
    log.info("Heartbeat round finished")


async def _heartbeat_loop() -> None:
    """后台循环：每 60 秒执行一次节点心跳检测。"""
    log.info("Node heartbeat scheduler started (interval=60s)")
    while True:
        try:
            await asyncio.sleep(60)
            async with AsyncSessionLocal() as db:
                await _heartbeat_round(db)
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
        try:
            await _heartbeat_task
        except asyncio.CancelledError:
            pass
        _heartbeat_task = None
