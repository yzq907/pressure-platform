"""Node enable/disable 集成测试。"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import NodeStatus, NodeType
from app.core.exceptions import MysteriousException
from app.models.node import Node


async def _create_node(
    db: AsyncSession,
    host: str = "10.0.0.1",
    type: int = NodeType.SLAVE.value,
    status: int = NodeStatus.DISABLED.value,
) -> int:
    n = Node(
        name=f"node-{host}",
        description="",
        type=type,
        host=host,
        username="root",
        password="x",
        port=22,
        status=status,
    )
    db.add(n)
    await db.commit()
    await db.refresh(n)
    return n.id


@pytest.mark.asyncio
async def test_enable_requires_auth(client: AsyncClient, db: AsyncSession) -> None:
    nid = await _create_node(db)
    resp = await client.get(f"/node/enable/{nid}")
    assert resp.json()["code"] == 1007


@pytest.mark.asyncio
async def test_enable_master_node_rejected(
    auth_client: AsyncClient, db: AsyncSession, slave_paths: None
) -> None:
    nid = await _create_node(db, type=NodeType.MASTER.value)
    resp = await auth_client.get(f"/node/enable/{nid}")
    assert resp.json()["code"] == 1049  # NODE_TYPE_ERROR


@pytest.mark.asyncio
async def test_enable_already_enabled_rejected(
    auth_client: AsyncClient, db: AsyncSession, slave_paths: None
) -> None:
    nid = await _create_node(db, status=NodeStatus.ENABLE.value)
    resp = await auth_client.get(f"/node/enable/{nid}")
    assert resp.json()["code"] == 1042  # NODE_IS_ENABLE


@pytest.mark.asyncio
async def test_enable_success_marks_db_enable(
    auth_client: AsyncClient, db: AsyncSession, slave_paths: None
) -> None:
    """默认 mock_ssh 让所有 SSH 调用成功"""
    nid = await _create_node(db)
    resp = await auth_client.get(f"/node/enable/{nid}")
    assert resp.json()["code"] == 0
    n = (await db.execute(select(Node).where(Node.id == nid))).scalar_one()
    assert n.status == NodeStatus.ENABLE.value


@pytest.mark.asyncio
async def test_enable_telnet_failure_marks_db_failed(
    auth_client: AsyncClient,
    db: AsyncSession,
    slave_paths: None,
    monkeypatch,
) -> None:
    """模拟 telnet 失败"""
    from app.core import ssh as ssh_mod
    from app.core.codes import Codes

    async def failing_telnet(self, timeout_ms: int = 200) -> bool:
        raise MysteriousException(Codes.CANNOT_CONNECT)

    monkeypatch.setattr(ssh_mod.SSHClient, "telnet", failing_telnet)

    nid = await _create_node(db)
    resp = await auth_client.get(f"/node/enable/{nid}")
    assert resp.json()["code"] == 1050  # NODE_CANNOT_CONNECT
    n = (await db.execute(select(Node).where(Node.id == nid))).scalar_one()
    assert n.status == NodeStatus.FAILED.value


@pytest.mark.asyncio
async def test_enable_md5_invalid_returns_jmeter_server_not_found(
    auth_client: AsyncClient,
    db: AsyncSession,
    slave_paths: None,
    monkeypatch,
) -> None:
    """md5sum 返回非 32 位十六进制 → JMETER_SERVER_NOT_FOUND"""
    from app.core import ssh as ssh_mod

    async def custom_exec(self, command: str) -> str:
        if "md5sum" in command:
            return "not-a-valid-md5"
        if "ps aux" in command:
            return "null"
        return ""

    monkeypatch.setattr(ssh_mod.SSHClient, "exec_command", custom_exec)

    nid = await _create_node(db)
    resp = await auth_client.get(f"/node/enable/{nid}")
    assert resp.json()["code"] == 1052  # JMETER_SERVER_NOT_FOUND


@pytest.mark.asyncio
async def test_disable_master_node_rejected(
    auth_client: AsyncClient, db: AsyncSession
) -> None:
    nid = await _create_node(db, type=NodeType.MASTER.value)
    resp = await auth_client.get(f"/node/disable/{nid}")
    assert resp.json()["code"] == 1053  # ONLY_SLAVE_CAN_DISABLE


@pytest.mark.asyncio
async def test_disable_node_not_exist(auth_client: AsyncClient) -> None:
    resp = await auth_client.get("/node/disable/999999")
    assert resp.json()["code"] == 1013  # NODE_NOT_EXIST


@pytest.mark.asyncio
async def test_disable_no_process_running(
    auth_client: AsyncClient, db: AsyncSession, monkeypatch
) -> None:
    """默认 mock_ssh 的 ps 返回 'null'，disable 时应抛 JMETER_SERVER_IS_NOT_ENABLE"""
    nid = await _create_node(db, status=NodeStatus.ENABLE.value)
    resp = await auth_client.get(f"/node/disable/{nid}")
    # 默认 ps 返回 "null"，所以会触发 JMETER_SERVER_IS_NOT_ENABLE (1052)
    assert resp.json()["code"] == 1052


@pytest.mark.asyncio
async def test_disable_with_running_process(
    auth_client: AsyncClient, db: AsyncSession, monkeypatch
) -> None:
    """模拟 jmeter-server 进程存在 → 应正常 kill 并设置 DISABLED"""
    from app.core import ssh as ssh_mod

    call_count = {"ps": 0}

    async def custom_exec(self, command: str) -> str:
        if "ps aux" in command and "grep jmeter-server" in command and "kill" not in command:
            call_count["ps"] += 1
            # 第一次 ps：有进程；第二次 ps（kill 之后）：没进程
            return "root  12345  ..." if call_count["ps"] == 1 else "null"
        if "kill -9" in command:
            return ""
        return ""

    monkeypatch.setattr(ssh_mod.SSHClient, "exec_command", custom_exec)

    nid = await _create_node(db, status=NodeStatus.ENABLE.value)
    resp = await auth_client.get(f"/node/disable/{nid}")
    assert resp.json()["code"] == 0
    n = (await db.execute(select(Node).where(Node.id == nid))).scalar_one()
    assert n.status == NodeStatus.DISABLED.value
