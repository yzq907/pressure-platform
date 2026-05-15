"""JAR slave 同步集成测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import NodeStatus, NodeType
from app.models.node import Node


async def _create_case_with_jmx(
    auth_client: AsyncClient,
    name: str,
    jmx_bytes: bytes,
) -> int:
    resp = await auth_client.post("/testcase/add", json={"name": name})
    case_id = resp.json()["data"]
    await auth_client.post(
        f"/jmx/upload/{case_id}",
        files={"jmxFile": ("test.jmx", jmx_bytes, "application/octet-stream")},
    )
    return case_id


@pytest.mark.asyncio
async def test_upload_jar_with_2_slaves_calls_scp_twice(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
    monkeypatch,
) -> None:
    from app.core import ssh as ssh_mod

    scp_calls: list = []

    async def tracking(self, local: str, remote: str) -> None:
        scp_calls.append((self.host, local, remote))

    monkeypatch.setattr(ssh_mod.SSHClient, "scp_file", tracking)

    for h in ("s1", "s2"):
        db.add(
            Node(
                name=h,
                type=NodeType.SLAVE.value,
                host=h,
                username="root",
                password="x",
                port=22,
                status=NodeStatus.ENABLE.value,
            )
        )
    await db.commit()

    case_id = await _create_case_with_jmx(auth_client, "jslv", sample_jmx_bytes)
    resp = await auth_client.post(
        f"/jar/upload/{case_id}",
        files={"jarFile": ("dep.jar", b"x", "application/java-archive")},
    )
    assert resp.json()["code"] == 0
    hosts = sorted(c[0] for c in scp_calls)
    assert hosts == ["s1", "s2"]


@pytest.mark.asyncio
async def test_upload_plugin_jar_also_synced_to_slaves(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
    monkeypatch,
) -> None:
    """插件 JAR 也要同步到 slave（Java 不区分）"""
    from app.core import ssh as ssh_mod

    scp_calls: list = []

    async def tracking(self, local: str, remote: str) -> None:
        scp_calls.append((self.host, local, remote))

    monkeypatch.setattr(ssh_mod.SSHClient, "scp_file", tracking)

    db.add(
        Node(
            name="s1",
            type=NodeType.SLAVE.value,
            host="s1",
            username="root",
            password="x",
            port=22,
            status=NodeStatus.ENABLE.value,
        )
    )
    await db.commit()

    case_id = await _create_case_with_jmx(auth_client, "pjslv", sample_jmx_bytes)
    resp = await auth_client.post(
        f"/jar/upload/{case_id}",
        files={"jarFile": ("jmeter-plugins-foo.jar", b"x", "application/java-archive")},
    )
    assert resp.json()["code"] == 0
    assert len(scp_calls) == 1
    # 目录应该是 master_jmeter_home/lib/ext/
    assert "lib/ext" in scp_calls[0][2]


@pytest.mark.asyncio
async def test_delete_normal_jar_rm_on_slaves(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
    monkeypatch,
) -> None:
    from sqlalchemy import select

    from app.core import ssh as ssh_mod
    from app.models.jar import Jar

    case_id = await _create_case_with_jmx(auth_client, "del_njar", sample_jmx_bytes)
    await auth_client.post(
        f"/jar/upload/{case_id}",
        files={"jarFile": ("dep.jar", b"x", "application/java-archive")},
    )

    db.add(
        Node(
            name="s1",
            type=NodeType.SLAVE.value,
            host="s1",
            username="root",
            password="x",
            port=22,
            status=NodeStatus.ENABLE.value,
        )
    )
    await db.commit()

    rm_calls: list[str] = []

    async def tracking_exec(self, command: str) -> str:
        if "rm -rf" in command:
            rm_calls.append(command)
        return "null" if "ps aux" in command else ""

    monkeypatch.setattr(ssh_mod.SSHClient, "exec_command", tracking_exec)

    obj = (await db.execute(select(Jar).where(Jar.test_case_id == case_id))).scalar_one()
    resp = await auth_client.get(f"/jar/delete/{obj.id}")
    assert resp.json()["data"] is True
    assert len(rm_calls) == 1


@pytest.mark.asyncio
async def test_delete_plugin_jar_does_not_rm_anywhere(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
    monkeypatch,
) -> None:
    """插件 JAR 删除不调 rm -rf（Java 行为）"""
    from sqlalchemy import select

    from app.core import ssh as ssh_mod
    from app.models.jar import Jar

    case_id = await _create_case_with_jmx(auth_client, "del_pjar", sample_jmx_bytes)
    await auth_client.post(
        f"/jar/upload/{case_id}",
        files={"jarFile": ("jmeter-plugins-bar.jar", b"x", "application/java-archive")},
    )

    db.add(
        Node(
            name="s1",
            type=NodeType.SLAVE.value,
            host="s1",
            username="root",
            password="x",
            port=22,
            status=NodeStatus.ENABLE.value,
        )
    )
    await db.commit()

    rm_calls: list[str] = []

    async def tracking_exec(self, command: str) -> str:
        if "rm -rf" in command:
            rm_calls.append(command)
        return "null" if "ps aux" in command else ""

    monkeypatch.setattr(ssh_mod.SSHClient, "exec_command", tracking_exec)

    obj = (await db.execute(select(Jar).where(Jar.test_case_id == case_id))).scalar_one()
    resp = await auth_client.get(f"/jar/delete/{obj.id}")
    assert resp.json()["data"] is True
    assert len(rm_calls) == 0  # 插件 JAR 不应触发 rm
