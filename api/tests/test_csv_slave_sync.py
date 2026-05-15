"""CSV slave 同步集成测试。验证：
- 没 enabled slave 时不调 scp
- 有 enabled slave 时按数量调 scp
- scp 失败不影响主流程（CSV 仍能上传成功）
"""

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
async def test_upload_csv_no_slaves_no_scp(
    auth_client: AsyncClient,
    data_home: Path,
    sample_jmx_bytes: bytes,
    monkeypatch,
) -> None:
    """无 enabled slave → 不会触发 scp_file"""
    from app.core import ssh as ssh_mod

    scp_calls: list = []

    async def tracking(self, local: str, remote: str) -> None:
        scp_calls.append((local, remote))

    monkeypatch.setattr(ssh_mod.SSHClient, "scp_file", tracking)

    case_id = await _create_case_with_jmx(auth_client, "no_slv", sample_jmx_bytes)
    resp = await auth_client.post(
        f"/csv/upload/{case_id}",
        files={"csvFile": ("data.csv", b"a,b\n1,2\n", "text/csv")},
    )
    assert resp.json()["code"] == 0
    assert scp_calls == []


@pytest.mark.asyncio
async def test_upload_csv_with_2_slaves_calls_scp_twice(
    auth_client: AsyncClient,
    data_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
    monkeypatch,
) -> None:
    from app.core import ssh as ssh_mod

    scp_calls: list = []

    async def tracking(self, local: str, remote: str) -> None:
        scp_calls.append((self.host, local, remote))

    monkeypatch.setattr(ssh_mod.SSHClient, "scp_file", tracking)

    # 插入 2 个 enabled slave
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

    case_id = await _create_case_with_jmx(auth_client, "two_slv", sample_jmx_bytes)
    resp = await auth_client.post(
        f"/csv/upload/{case_id}",
        files={"csvFile": ("data.csv", b"a,b\n1,2\n", "text/csv")},
    )
    assert resp.json()["code"] == 0
    hosts = sorted(c[0] for c in scp_calls)
    assert hosts == ["s1", "s2"]


@pytest.mark.asyncio
async def test_upload_csv_scp_failure_does_not_block_upload(
    auth_client: AsyncClient,
    data_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
    monkeypatch,
) -> None:
    """scp 抛异常时不应影响 CSV 上传：响应仍然 code=0，DB 有记录"""
    from app.core import ssh as ssh_mod

    async def failing(self, local: str, remote: str) -> None:
        raise RuntimeError("ssh broke")

    monkeypatch.setattr(ssh_mod.SSHClient, "scp_file", failing)

    db.add(
        Node(
            name="s_bad",
            type=NodeType.SLAVE.value,
            host="s_bad",
            username="root",
            password="x",
            port=22,
            status=NodeStatus.ENABLE.value,
        )
    )
    await db.commit()

    case_id = await _create_case_with_jmx(auth_client, "fail_slv", sample_jmx_bytes)
    resp = await auth_client.post(
        f"/csv/upload/{case_id}",
        files={"csvFile": ("data.csv", b"a,b\n1,2\n", "text/csv")},
    )
    assert resp.json()["code"] == 0


@pytest.mark.asyncio
async def test_delete_csv_rm_on_enabled_slaves(
    auth_client: AsyncClient,
    data_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
    monkeypatch,
) -> None:
    """删除 CSV 时应给每个 enabled slave 发 rm -rf 命令"""
    from app.core import ssh as ssh_mod
    from sqlalchemy import select
    from app.models.csv import Csv

    # 先上传一个 CSV（此时还没有 slave，所以不会 scp）
    case_id = await _create_case_with_jmx(auth_client, "del_slv", sample_jmx_bytes)
    await auth_client.post(
        f"/csv/upload/{case_id}",
        files={"csvFile": ("data.csv", b"a,b\n1,2\n", "text/csv")},
    )

    # 再加 enabled slave
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

    csv_obj = (
        await db.execute(select(Csv).where(Csv.test_case_id == case_id))
    ).scalar_one()
    resp = await auth_client.get(f"/csv/delete/{csv_obj.id}")
    assert resp.json()["data"] is True
    assert len(rm_calls) == 1
    assert "rm -rf" in rm_calls[0]
