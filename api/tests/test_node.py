"""/node/* CRUD 路由的集成测试（不含 enable/disable）"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import NodeStatus, NodeType
from app.models.execution_node import ExecutionNode
from app.models.node import Node


@pytest.mark.asyncio
async def test_node_heartbeat_restarts_missing_jmeter_server_for_enabled_slave(
    db: AsyncSession,
    monkeypatch,
    slave_paths: None,
) -> None:
    """启用压力机 SSH 正常但 jmeter-server 进程不存在时，心跳应主动拉起。"""
    from app.core import ssh as ssh_mod
    from app.services import node_heartbeat

    monkeypatch.setattr(node_heartbeat, "_JMETER_SERVER_START_WAIT_SECONDS", 0)

    commands: list[str] = []
    server_started = False

    async def fake_exec(self, command: str) -> str:
        nonlocal server_started
        commands.append(command)
        if "ApacheJMeter.jar" in command and "grep" in command:
            return "root 12345 ... jmeter-server" if server_started else "null"
        if "jmeter-server" in command and "rmi.server.hostname" in command:
            server_started = True
            return "Using local port: 1099"
        if "cat /proc/loadavg" in command:
            return "0.10"
        if command.startswith("free "):
            return "20.0"
        if command.startswith("top "):
            return "90.0"
        return ""

    monkeypatch.setattr(ssh_mod.SSHClient, "exec_command", fake_exec)

    node = Node(
        name="heartbeat-missing-server",
        type=NodeType.SLAVE.value,
        host="10.0.7.1",
        username="root",
        password="x",
        port=22,
        status=NodeStatus.ENABLE.value,
        health_status=1,
    )
    db.add(node)
    await db.commit()
    await db.refresh(node)

    await node_heartbeat._heartbeat_round(db)

    await db.refresh(node)
    assert node.status == NodeStatus.ENABLE.value
    assert node.health_status == 1
    assert node.last_heartbeat is not None
    assert any("jmeter-server -Djava.rmi.server.hostname=10.0.7.1" in cmd for cmd in commands)


@pytest.mark.asyncio
async def test_node_heartbeat_keeps_enabled_slave_unhealthy_when_jmeter_restart_fails(
    db: AsyncSession,
    monkeypatch,
    slave_paths: None,
) -> None:
    """启用压力机自动拉起 jmeter-server 失败时，心跳仍应标记为不健康。"""
    from app.core import ssh as ssh_mod
    from app.services import node_heartbeat

    monkeypatch.setattr(node_heartbeat, "_JMETER_SERVER_START_WAIT_SECONDS", 0)

    commands: list[str] = []

    async def fake_exec(self, command: str) -> str:
        commands.append(command)
        if "ApacheJMeter.jar" in command and "grep" in command:
            return "null"
        if "cat /proc/loadavg" in command:
            return "0.10"
        if command.startswith("free "):
            return "20.0"
        if command.startswith("top "):
            return "90.0"
        return ""

    monkeypatch.setattr(ssh_mod.SSHClient, "exec_command", fake_exec)

    node = Node(
        name="heartbeat-restart-failed",
        type=NodeType.SLAVE.value,
        host="10.0.7.2",
        username="root",
        password="x",
        port=22,
        status=NodeStatus.ENABLE.value,
        health_status=1,
    )
    db.add(node)
    await db.commit()
    await db.refresh(node)

    await node_heartbeat._heartbeat_round(db)

    await db.refresh(node)
    assert node.status == NodeStatus.ENABLE.value
    assert node.health_status == 0
    assert node.last_heartbeat is not None
    assert any("jmeter-server -Djava.rmi.server.hostname=10.0.7.2" in cmd for cmd in commands)


@pytest.mark.asyncio
async def test_node_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/node/list?page=1&size=10")
    assert resp.json()["code"] == 1007


@pytest.mark.asyncio
async def test_add_node_success(auth_client: AsyncClient, db: AsyncSession) -> None:
    resp = await auth_client.post(
        "/node/add",
        json={
            "name": "slave-1",
            "description": "测试节点",
            "type": NodeType.SLAVE.value,
            "host": "192.168.1.10",
            "username": "root",
            "password": "secret",
            "port": 22,
        },
    )
    body = resp.json()
    assert body["code"] == 0
    node_id = body["data"]
    obj = (await db.execute(select(Node).where(Node.id == node_id))).scalar_one()
    assert obj.host == "192.168.1.10"
    # status 默认 DISABLED
    assert obj.status == NodeStatus.DISABLED.value
    assert obj.creator == "测试管理员"


@pytest.mark.asyncio
async def test_add_node_duplicate_host(auth_client: AsyncClient) -> None:
    payload = {"name": "n", "type": 0, "host": "10.0.0.1", "username": "u", "port": 22}
    await auth_client.post("/node/add", json=payload)
    resp = await auth_client.post("/node/add", json={**payload, "name": "n2"})
    assert resp.json()["code"] == 1009  # NODE_EXIST


@pytest.mark.asyncio
async def test_add_node_missing_param(auth_client: AsyncClient) -> None:
    resp = await auth_client.post(
        "/node/add", json={"name": "x", "host": "1.1.1.1"}  # 缺 username/port/type
    )
    assert resp.json()["code"] == 1003


@pytest.mark.asyncio
async def test_update_node_success(auth_client: AsyncClient, db: AsyncSession) -> None:
    add_resp = await auth_client.post(
        "/node/add",
        json={"name": "n", "type": 0, "host": "10.0.0.2", "username": "u", "port": 22},
    )
    node_id = add_resp.json()["data"]
    resp = await auth_client.post(
        f"/node/update/{node_id}",
        json={"description": "updated"},
    )
    assert resp.json()["data"] is True
    obj = (await db.execute(select(Node).where(Node.id == node_id))).scalar_one()
    assert obj.description == "updated"
    assert obj.host == "10.0.0.2"  # 未改


@pytest.mark.asyncio
async def test_update_node_password_only_when_explicit_new_value(
    auth_client: AsyncClient,
    db: AsyncSession,
) -> None:
    add_resp = await auth_client.post(
        "/node/add",
        json={
            "name": "n-password",
            "type": 0,
            "host": "10.0.0.22",
            "username": "u",
            "password": "old-secret",
            "port": 22,
        },
    )
    node_id = add_resp.json()["data"]

    resp = await auth_client.post(f"/node/update/{node_id}", json={"description": "no password"})
    assert resp.json()["data"] is True
    obj = (await db.execute(select(Node).where(Node.id == node_id))).scalar_one()
    assert obj.password == "old-secret"

    resp = await auth_client.post(f"/node/update/{node_id}", json={"password": ""})
    assert resp.json()["data"] is True
    await db.refresh(obj)
    assert obj.password == "old-secret"

    resp = await auth_client.post(f"/node/update/{node_id}", json={"password": "******"})
    assert resp.json()["data"] is True
    await db.refresh(obj)
    assert obj.password == "old-secret"

    resp = await auth_client.post(f"/node/update/{node_id}", json={"password": "new-secret"})
    assert resp.json()["data"] is True
    await db.refresh(obj)
    assert obj.password == "new-secret"


@pytest.mark.asyncio
async def test_get_node_by_id(auth_client: AsyncClient) -> None:
    add_resp = await auth_client.post(
        "/node/add",
        json={"name": "n", "type": 1, "host": "10.0.0.3", "username": "u", "port": 22, "password": "pw"},
    )
    node_id = add_resp.json()["data"]
    resp = await auth_client.get(f"/node/getById/{node_id}")
    body = resp.json()
    data = body["data"]
    assert data["host"] == "10.0.0.3"
    assert data["password"] == "******"
    assert "createTime" in data


@pytest.mark.asyncio
async def test_get_node_by_id_not_found(auth_client: AsyncClient) -> None:
    resp = await auth_client.get("/node/getById/999999")
    assert resp.json()["data"] is None


@pytest.mark.asyncio
async def test_list_nodes_search_by_name(auth_client: AsyncClient) -> None:
    for i, name in enumerate(["search_alpha", "search_beta", "other"]):
        await auth_client.post(
            "/node/add",
            json={"name": name, "type": 0, "host": f"10.0.1.{i}", "username": "u", "port": 22},
        )
    resp = await auth_client.get("/node/list?page=1&size=10&name=search")
    assert resp.json()["data"]["total"] == 2


@pytest.mark.asyncio
async def test_list_nodes_search_by_host_exact(auth_client: AsyncClient) -> None:
    """host 是精确匹配（Java 行为），传半段 IP 不会匹配"""
    await auth_client.post(
        "/node/add",
        json={"name": "n1", "type": 0, "host": "10.0.2.1", "username": "u", "port": 22},
    )
    # 半段 IP，应该 0 条
    resp = await auth_client.get("/node/list?page=1&size=10&host=10.0.2")
    assert resp.json()["data"]["total"] == 0
    # 完整 IP，应该 1 条
    resp2 = await auth_client.get("/node/list?page=1&size=10&host=10.0.2.1")
    assert resp2.json()["data"]["total"] == 1


@pytest.mark.asyncio
async def test_list_nodes_password_masked(auth_client: AsyncClient) -> None:
    await auth_client.post(
        "/node/add",
        json={
            "name": "n",
            "type": 0,
            "host": "10.0.3.1",
            "username": "u",
            "password": "real-secret",
            "port": 22,
        },
    )
    resp = await auth_client.get("/node/list?page=1&size=10")
    items = resp.json()["data"]["list"]
    assert items[0]["password"] == "******"


@pytest.mark.asyncio
async def test_enable_slave_count_region_matches_exact_token(
    auth_client: AsyncClient,
    db: AsyncSession,
) -> None:
    db.add(
        Node(
            name="n1",
            type=NodeType.SLAVE.value,
            host="10.0.5.1",
            username="u",
            port=22,
            status=NodeStatus.ENABLE.value,
            health_status=1,
            region="长沙-备份",
        )
    )
    db.add(
        Node(
            name="n2",
            type=NodeType.SLAVE.value,
            host="10.0.5.2",
            username="u",
            port=22,
            status=NodeStatus.ENABLE.value,
            health_status=1,
            region="深圳,长沙",
        )
    )
    await db.commit()

    resp = await auth_client.get("/node/enableSlaveCount?region=长沙")
    assert resp.json()["data"] == 1


@pytest.mark.asyncio
async def test_enable_slave_count_excludes_leased_slave(
    auth_client: AsyncClient,
    db: AsyncSession,
) -> None:
    node1 = Node(
        name="busy",
        type=NodeType.SLAVE.value,
        host="10.0.6.1",
        username="u",
        port=22,
        status=NodeStatus.ENABLE.value,
        health_status=1,
        region="华东",
    )
    node2 = Node(
        name="idle",
        type=NodeType.SLAVE.value,
        host="10.0.6.2",
        username="u",
        port=22,
        status=NodeStatus.ENABLE.value,
        health_status=1,
        region="华东",
    )
    db.add_all([node1, node2])
    await db.commit()
    await db.refresh(node1)
    db.add(
        ExecutionNode(
            report_id=1001,
            test_case_id=2001,
            node_id=node1.id,
            node_host=node1.host,
            region="华东",
            status="leased",
        )
    )
    await db.commit()

    resp = await auth_client.get("/node/enableSlaveCount?region=华东")
    assert resp.json()["data"] == 1


@pytest.mark.asyncio
async def test_delete_node(auth_client: AsyncClient) -> None:
    add_resp = await auth_client.post(
        "/node/add",
        json={"name": "n", "type": 0, "host": "10.0.4.1", "username": "u", "port": 22},
    )
    node_id = add_resp.json()["data"]
    assert (await auth_client.get(f"/node/delete/{node_id}")).json()["data"] is True
    assert (await auth_client.get(f"/node/delete/{node_id}")).json()["data"] is False
