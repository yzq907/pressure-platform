"""TestCase debug/run/stop/syncNode/getFull/getJMeterResult 集成测试。

策略：
- 用 `jmeter_bin_home` fixture 提供一个 fake jmeter 脚本（写合规 stdout + log + jtl）
- 用 `mock_ssh` autouse fixture mock SSH（slave telnet + scp）
- 真实启动 asyncio.create_subprocess_exec 跑 fake_jmeter.sh，再 wait_for_completion 同步等待
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import NodeStatus, NodeType, TestCaseStatus
from app.models.config import Config
from app.models.node import Node
from app.models.report import Report
from app.models.testcase import TestCase
from app.services import jmeter_runner


async def _create_case(auth_client: AsyncClient, name: str = "t1") -> int:
    """建一个空用例，返回 id"""
    resp = await auth_client.post("/testcase/add", json={"name": name})
    return resp.json()["data"]


async def _create_case_with_jmx(
    auth_client: AsyncClient,
    name: str,
    jmx_bytes: bytes,
) -> int:
    """建用例 + 上传 sample JMX，返回 id"""
    case_id = await _create_case(auth_client, name)
    files = {"jmxFile": ("test.jmx", jmx_bytes, "application/octet-stream")}
    await auth_client.post(f"/jmx/upload/{case_id}", files=files)
    return case_id


# ---------------------------------------------------------------------------
# debug
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debug_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/testcase/debug/1")
    assert resp.json()["code"] == 1007


@pytest.mark.asyncio
async def test_debug_no_jmx_returns_jmx_not_exist(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_bin_home: Path,
) -> None:
    case_id = await _create_case(auth_client, "no_jmx")
    resp = await auth_client.get(f"/testcase/debug/{case_id}")
    assert resp.json()["code"] == 1014  # JMX_NOT_EXIST


@pytest.mark.asyncio
async def test_debug_no_bin_home_returns_fail(
    auth_client: AsyncClient,
    data_home: Path,
    sample_jmx_bytes: bytes,
) -> None:
    """没注册 MASTER_JMETER_BIN_HOME → 配置不存在抛 CONFIG_NOT_EXIST(1012)"""
    case_id = await _create_case_with_jmx(auth_client, "no_bin", sample_jmx_bytes)
    resp = await auth_client.get(f"/testcase/debug/{case_id}")
    assert resp.json()["code"] == 1012


@pytest.mark.asyncio
async def test_debug_success_flow(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_bin_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
) -> None:
    """端到端：状态 NOT_RUN → RUN_ING → RUN_SUCCESS，report.response_data 含 fake JMeter 输出"""
    case_id = await _create_case_with_jmx(auth_client, "dbg_ok", sample_jmx_bytes)
    resp = await auth_client.get(f"/testcase/debug/{case_id}")
    assert resp.json()["code"] == 0

    # 同步等待后台 jmeter task 完成
    await jmeter_runner.wait_for_completion(case_id, timeout=10.0)

    tc = (await db.execute(select(TestCase).where(TestCase.id == case_id))).scalar_one()
    assert tc.status == TestCaseStatus.RUN_SUCCESS.value

    reports = (
        await db.execute(select(Report).where(Report.test_case_id == case_id))
    ).scalars().all()
    assert len(reports) == 1
    assert reports[0].status == TestCaseStatus.RUN_SUCCESS.value
    assert "Hello from fake JMeter" in reports[0].response_data


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_with_another_running_rejected(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_bin_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
) -> None:
    case_id = await _create_case_with_jmx(auth_client, "r_a", sample_jmx_bytes)
    # 手动把别的用例标 RUN_ING
    blocker = TestCase(name="x", status=TestCaseStatus.RUN_ING.value, test_case_dir="/tmp")
    db.add(blocker)
    await db.commit()

    resp = await auth_client.get(f"/testcase/run/{case_id}")
    assert resp.json()["code"] == 1058  # TESTCASE_IS_RUNNING


@pytest.mark.asyncio
async def test_run_no_slaves_no_R_flag(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_bin_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
    monkeypatch,
) -> None:
    """无 enabled slave → cmd 不带 -R"""
    captured: dict = {}

    real_launch = jmeter_runner.launch_jmeter

    async def spy(cmd, **kw):
        captured["cmd"] = cmd
        return await real_launch(cmd, **kw)

    monkeypatch.setattr(jmeter_runner, "launch_jmeter", spy)

    case_id = await _create_case_with_jmx(auth_client, "r_no_slave", sample_jmx_bytes)
    resp = await auth_client.get(f"/testcase/run/{case_id}")
    assert resp.json()["code"] == 0
    assert "-R" not in captured["cmd"]
    await jmeter_runner.wait_for_completion(case_id, timeout=10.0)


@pytest.mark.asyncio
async def test_run_creates_report_snapshot(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_bin_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
    monkeypatch,
) -> None:
    captured: dict = {}
    real_launch = jmeter_runner.launch_jmeter

    async def spy(cmd, **kw):
        captured["cmd"] = cmd
        return await real_launch(cmd, **kw)

    monkeypatch.setattr(jmeter_runner, "launch_jmeter", spy)

    case_id = await _create_case_with_jmx(auth_client, "r_snapshot", sample_jmx_bytes)
    tc = (await db.execute(select(TestCase).where(TestCase.id == case_id))).scalar_one()
    tc.service = "EMM-API"
    db.add(Config(config_key="INIT_ARTIFACT_TESTCASE_IDS", config_value=str(case_id), description="init case"))
    db.add(Config(config_key="GRAFANA_INSTANCE_MAP", config_value='{"EMM-API":"10.10.27.42:9200"}', description="grafana"))
    await db.commit()

    resp = await auth_client.post(
        f"/testcase/run/{case_id}",
        json={"numThreads": "30", "rampTime": "0", "duration": "60", "slaveCount": 1, "region": "华南"},
    )
    assert resp.json()["code"] == 0
    await jmeter_runner.wait_for_completion(case_id, timeout=10.0)

    assert "-R" not in captured["cmd"]
    assert any(arg.startswith("-JartifactDir=") for arg in captured["cmd"])
    report = (
        await db.execute(select(Report).where(Report.test_case_id == case_id))
    ).scalars().one()
    assert report.service_name == "EMM-API"
    assert report.total_threads == 30
    assert report.slave_count == 1
    assert report.region == "华南"
    assert report.grafana_instance == "10.10.27.42:9200"
    assert report.artifact_dir.endswith("/artifacts")


@pytest.mark.asyncio
async def test_run_with_region_without_healthy_slave_does_not_create_report(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_bin_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
) -> None:
    case_id = await _create_case_with_jmx(auth_client, "r_region_no_slave", sample_jmx_bytes)
    resp = await auth_client.post(
        f"/testcase/run/{case_id}",
        json={
            "numThreads": "10",
            "rampTime": "0",
            "duration": "60",
            "slaveCount": 1,
            "region": "长沙",
        },
    )

    body = resp.json()
    assert body["code"] == -1
    assert "暂无可用压力机" in body["message"]

    reports = (
        await db.execute(select(Report).where(Report.test_case_id == case_id))
    ).scalars().all()
    assert reports == []


@pytest.mark.asyncio
async def test_run_with_slaves_adds_R_flag(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_bin_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
    monkeypatch,
) -> None:
    """有 2 个 enabled slave → cmd 带 -R host1,host2"""
    captured: dict = {}

    real_launch = jmeter_runner.launch_jmeter

    async def spy(cmd, **kw):
        captured["cmd"] = cmd
        return await real_launch(cmd, **kw)

    monkeypatch.setattr(jmeter_runner, "launch_jmeter", spy)

    # 插入 2 个 enabled slave
    for h in ("10.0.0.1", "10.0.0.2"):
        n = Node(
            name=h,
            type=NodeType.SLAVE.value,
            host=h,
            username="root",
            password="x",
            port=22,
            status=NodeStatus.ENABLE.value,
        )
        db.add(n)
    await db.commit()

    case_id = await _create_case_with_jmx(auth_client, "r_slaves", sample_jmx_bytes)
    resp = await auth_client.get(f"/testcase/run/{case_id}")
    assert resp.json()["code"] == 0
    assert "-R" in captured["cmd"]
    r_idx = captured["cmd"].index("-R")
    assert captured["cmd"][r_idx + 1] == "10.0.0.1,10.0.0.2"
    await jmeter_runner.wait_for_completion(case_id, timeout=10.0)


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_not_running_rejected(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_bin_home: Path,
    sample_jmx_bytes: bytes,
) -> None:
    case_id = await _create_case_with_jmx(auth_client, "s_not_run", sample_jmx_bytes)
    resp = await auth_client.get(f"/testcase/stop/{case_id}")
    assert resp.json()["code"] == 1059  # TESTCASE_IS_NOT_RUNNING


@pytest.mark.asyncio
async def test_stop_running_transitions_to_success(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_bin_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
) -> None:
    """stop 时 testcase 必须是 RUN_ING；shutdown.sh callback 应把状态改为 RUN_SUCCESS"""
    case_id = await _create_case_with_jmx(auth_client, "s_run", sample_jmx_bytes)
    # 手动把状态改为 RUN_ING
    tc = (await db.execute(select(TestCase).where(TestCase.id == case_id))).scalar_one()
    tc.status = TestCaseStatus.RUN_ING.value
    await db.commit()

    resp = await auth_client.get(f"/testcase/stop/{case_id}")
    assert resp.json()["code"] == 0

    # 等 shutdown.sh + callback 跑完。stop 不挂在 _running_tasks，所以直接 sleep 一会。
    import asyncio as _a

    for _ in range(20):
        await _a.sleep(0.05)
        await db.refresh(tc)
        if tc.status == TestCaseStatus.RUN_SUCCESS.value:
            break
    assert tc.status == TestCaseStatus.RUN_SUCCESS.value


# ---------------------------------------------------------------------------
# getFull
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_full(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_home: Path,
    sample_jmx_bytes: bytes,
) -> None:
    case_id = await _create_case_with_jmx(auth_client, "gf", sample_jmx_bytes)
    # 加 CSV + JAR
    await auth_client.post(
        f"/csv/upload/{case_id}",
        files={"csvFile": ("data.csv", b"a,b\n1,2\n", "text/csv")},
    )
    await auth_client.post(
        f"/jar/upload/{case_id}",
        files={"jarFile": ("dep.jar", b"x", "application/java-archive")},
    )

    resp = await auth_client.get(f"/testcase/getFull/{case_id}")
    data = resp.json()["data"]
    assert data["jmxVO"]["srcName"] == "test.jmx"
    assert len(data["csvVOList"]) == 1
    assert len(data["jarVOList"]) == 1


# ---------------------------------------------------------------------------
# syncNode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_node_enable_rejected(auth_client: AsyncClient, db: AsyncSession) -> None:
    n = Node(
        name="slv",
        type=NodeType.SLAVE.value,
        host="10.0.0.5",
        username="root",
        password="x",
        port=22,
        status=NodeStatus.ENABLE.value,
    )
    db.add(n)
    await db.commit()
    await db.refresh(n)

    resp = await auth_client.get(f"/testcase/syncNode/{n.id}")
    assert resp.json()["code"] == 1042  # NODE_IS_ENABLE


@pytest.mark.asyncio
async def test_sync_node_scp_called_for_csv_and_jar(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
    monkeypatch,
) -> None:
    """新增 slave 时，应给每个用例的 csv/jar 调 scp_file"""
    from app.core import ssh as ssh_mod

    scp_calls: list[tuple[str, str]] = []

    async def tracking_scp(self, local_path: str, remote_dir: str) -> None:
        scp_calls.append((local_path, remote_dir))

    monkeypatch.setattr(ssh_mod.SSHClient, "scp_file", tracking_scp)

    case_id = await _create_case_with_jmx(auth_client, "sn", sample_jmx_bytes)
    await auth_client.post(
        f"/csv/upload/{case_id}",
        files={"csvFile": ("data.csv", b"a,b\n1,2\n", "text/csv")},
    )
    await auth_client.post(
        f"/jar/upload/{case_id}",
        files={"jarFile": ("dep.jar", b"x", "application/java-archive")},
    )

    # 上传过程中也会触发 scp（slave 同步），先清空
    scp_calls.clear()

    n = Node(
        name="newslv",
        type=NodeType.SLAVE.value,
        host="10.0.0.99",
        username="root",
        password="x",
        port=22,
        status=NodeStatus.DISABLED.value,
    )
    db.add(n)
    await db.commit()
    await db.refresh(n)

    resp = await auth_client.get(f"/testcase/syncNode/{n.id}")
    assert resp.json()["code"] == 0
    # 1 个 csv + 1 个 jar = 2 次 scp
    assert len(scp_calls) == 2


# ---------------------------------------------------------------------------
# getJMeterResult
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_jmeter_result_no_reports_returns_empty(
    auth_client: AsyncClient, data_home: Path
) -> None:
    case_id = await _create_case(auth_client, "no_rpt")
    resp = await auth_client.get(f"/testcase/getJMeterResult/{case_id}")
    assert resp.json()["data"] == []


@pytest.mark.asyncio
async def test_get_jmeter_result_parses_log(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_bin_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
) -> None:
    """先跑 debug，再调 getJMeterResult，应该能解析 fake_jmeter 写的 summary 行"""
    case_id = await _create_case_with_jmx(auth_client, "gjr", sample_jmx_bytes)
    await auth_client.get(f"/testcase/debug/{case_id}")
    await jmeter_runner.wait_for_completion(case_id, timeout=10.0)

    resp = await auth_client.get(f"/testcase/getJMeterResult/{case_id}")
    items = resp.json()["data"]
    assert len(items) == 1
    assert items[0]["timestamp"] == "10:00:00"
    assert items[0]["throughput"] == 100.0
    assert items[0]["avgResponseTime"] == 5.0
