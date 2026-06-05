"""TestCase debug/run/stop/syncNode/getFull/getJMeterResult 集成测试。

策略：
- 用 `jmeter_bin_home` fixture 提供一个 fake jmeter 脚本（写合规 stdout + log + jtl）
- 用 `mock_ssh` autouse fixture mock SSH（slave telnet + scp）
- 真实启动 asyncio.create_subprocess_exec 跑 fake_jmeter.sh，再 wait_for_completion 同步等待
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ExecType, NodeStatus, NodeType, TestCaseStatus
from app.models.config import Config
from app.models.execution_node import ExecutionNode
from app.models.node import Node
from app.models.report import Report
from app.models.testcase import TestCase
from app.services import jmeter_runner


def _jmeter_property_value(parent, name: str) -> str | None:
    for prop in parent.iter():
        if prop.get("name") == name:
            return prop.text
        prop_children = list(prop)
        if prop_children and prop_children[0].tag == "name" and prop_children[0].text == name:
            for child in prop_children[1:]:
                if child.tag == "value":
                    return child.text
    return None


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


UPLOAD_FILE_JMX = b"""<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan>
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true"/>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="Thread Group" enabled="true">
        <stringProp name="ThreadGroup.num_threads">1</stringProp>
        <stringProp name="ThreadGroup.ramp_time">1</stringProp>
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController">
          <stringProp name="LoopController.loops">1</stringProp>
        </elementProp>
      </ThreadGroup>
      <hashTree>
        <HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="upload" enabled="true">
          <elementProp name="HTTPsampler.Files" elementType="HTTPFileArgs">
            <collectionProp name="HTTPFileArgs.files">
              <elementProp name="avatar.jpg" elementType="HTTPFileArg">
                <stringProp name="File.path">avatar.jpg</stringProp>
                <stringProp name="File.paramname">file</stringProp>
                <stringProp name="File.mimetype">image/jpeg</stringProp>
              </elementProp>
            </collectionProp>
          </elementProp>
        </HTTPSamplerProxy>
        <hashTree/>
      </hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
"""

TRANSACTION_JMX = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan>
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true"/>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="业务线程组" enabled="true">
        <stringProp name="ThreadGroup.num_threads">5</stringProp>
        <stringProp name="ThreadGroup.ramp_time">1</stringProp>
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController">
          <stringProp name="LoopController.loops">-1</stringProp>
        </elementProp>
      </ThreadGroup>
      <hashTree>
        <TransactionController guiclass="TransactionControllerGui" testclass="TransactionController" testname="策略获取" enabled="true">
          <boolProp name="TransactionController.parent">true</boolProp>
        </TransactionController>
        <hashTree>
          <HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="策略接口" enabled="true"/>
          <hashTree/>
        </hashTree>
        <TransactionController guiclass="TransactionControllerGui" testclass="TransactionController" testname="获取设备信息" enabled="true"/>
        <hashTree/>
      </hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
""".encode()

MULTI_THREAD_GROUP_JMX = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan>
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true"/>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="2_策略获取" enabled="true">
        <stringProp name="ThreadGroup.num_threads">1</stringProp>
        <stringProp name="ThreadGroup.ramp_time">1</stringProp>
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController">
          <stringProp name="LoopController.loops">-1</stringProp>
        </elementProp>
      </ThreadGroup>
      <hashTree><HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="2_策略获取" enabled="true"/><hashTree/></hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="7_获取应用版本" enabled="true">
        <stringProp name="ThreadGroup.num_threads">1</stringProp>
        <stringProp name="ThreadGroup.ramp_time">1</stringProp>
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController">
          <stringProp name="LoopController.loops">-1</stringProp>
        </elementProp>
      </ThreadGroup>
      <hashTree><HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="7_获取应用版本" enabled="true"/><hashTree/></hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="9_下载证书" enabled="true">
        <stringProp name="ThreadGroup.num_threads">1</stringProp>
        <stringProp name="ThreadGroup.ramp_time">1</stringProp>
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController">
          <stringProp name="LoopController.loops">-1</stringProp>
        </elementProp>
      </ThreadGroup>
      <hashTree><HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="9_下载证书" enabled="true"/><hashTree/></hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
""".encode()


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
    assert report.total_threads == 60
    assert report.slave_count == 1
    assert report.region == "华南"
    assert report.grafana_instance == "10.10.27.42:9200"
    assert report.artifact_dir.endswith("/artifacts")


@pytest.mark.asyncio
async def test_run_applies_thread_group_overrides(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_bin_home: Path,
    sample_jmx_bytes: bytes,
    monkeypatch,
) -> None:
    captured: dict = {}
    real_launch = jmeter_runner.launch_jmeter

    async def spy(cmd, **kw):
        captured["cmd"] = cmd
        return await real_launch(cmd, **kw)

    monkeypatch.setattr(jmeter_runner, "launch_jmeter", spy)

    case_id = await _create_case_with_jmx(auth_client, "r_tg_override", sample_jmx_bytes)
    groups = await auth_client.get(f"/testcase/runThreadGroups/{case_id}")
    assert groups.json()["data"] == [
        {"key": "thread_group:0", "name": "Default ThreadGroup", "type": "thread_group", "enabled": True},
        {"key": "stepping_thread_group:1", "name": "Disabled Stepping", "type": "stepping_thread_group", "enabled": False},
        {"key": "concurrency_thread_group:2", "name": "Concurrency Group", "type": "concurrency_thread_group", "enabled": True},
    ]

    resp = await auth_client.post(
        f"/testcase/run/{case_id}",
        json={
            "numThreads": "30",
            "rampTime": "10",
            "duration": "600",
            "slaveCount": 0,
            "threadGroupOverrides": [
                {
                    "name": "Default ThreadGroup",
                    "mode": "custom",
                    "numThreads": "1",
                    "rampTime": "1",
                },
                {"name": "Concurrency Group", "mode": "fixed"},
            ],
        },
    )
    assert resp.json()["code"] == 0
    await jmeter_runner.wait_for_completion(case_id, timeout=10.0)

    run_jmx = Path(captured["cmd"][captured["cmd"].index("-t") + 1])
    from lxml import etree

    tree = etree.parse(str(run_jmx))
    values: dict[tuple[str, str], str] = {}
    for tag in ("ThreadGroup", "com.blazemeter.jmeter.threads.concurrency.ConcurrencyThreadGroup"):
        for parent in tree.iter(tag):
            testname = parent.get("testname") or ""
            for prop in parent.iter():
                name = prop.get("name")
                if name:
                    values[(testname, name)] = prop.text or ""
    assert values[("Default ThreadGroup", "ThreadGroup.num_threads")] == "1"
    assert values[("Default ThreadGroup", "ThreadGroup.ramp_time")] == "1"
    assert values[("Default ThreadGroup", "ThreadGroup.duration")] == "600"
    assert values[("Concurrency Group", "TargetLevel")] == "100"
    assert values[("Concurrency Group", "Hold")] == "300"


@pytest.mark.asyncio
async def test_run_transactions_lists_transaction_controllers(
    auth_client: AsyncClient,
    data_home: Path,
) -> None:
    case_id = await _create_case_with_jmx(auth_client, "r_transactions", TRANSACTION_JMX)

    resp = await auth_client.get(f"/testcase/runTransactions/{case_id}")
    body = resp.json()

    assert body["code"] == 0
    assert body["data"] == [
        {
            "key": "transaction:0",
            "name": "策略获取",
            "threadGroup": "业务线程组",
            "enabled": True,
        },
        {
            "key": "transaction:1",
            "name": "获取设备信息",
            "threadGroup": "业务线程组",
            "enabled": True,
        },
    ]


@pytest.mark.asyncio
async def test_run_applies_thread_group_pacing_cycle_timer(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_bin_home: Path,
    monkeypatch,
) -> None:
    captured: dict = {}
    real_launch = jmeter_runner.launch_jmeter

    async def spy(cmd, **kw):
        captured["cmd"] = cmd
        return await real_launch(cmd, **kw)

    monkeypatch.setattr(jmeter_runner, "launch_jmeter", spy)

    case_id = await _create_case_with_jmx(auth_client, "r_thread_group_pacing", TRANSACTION_JMX)
    resp = await auth_client.post(
        f"/testcase/run/{case_id}",
        json={
            "numThreads": "10",
            "rampTime": "1",
            "duration": "60",
            "slaveCount": 0,
            "threadGroupOverrides": [
                {"key": "thread_group:0", "name": "业务线程组", "enabled": True, "mode": "global", "pacingMs": 800}
            ],
        },
    )

    assert resp.json()["code"] == 0
    await jmeter_runner.wait_for_completion(case_id, timeout=10.0)

    from lxml import etree

    run_jmx = Path(captured["cmd"][captured["cmd"].index("-t") + 1])
    tree = etree.parse(str(run_jmx))
    timer = next(tree.iter("JSR223Timer"))
    assert timer.get("testname") == "平台Pacing_业务线程组"
    assert _jmeter_property_value(timer, "parameters") == "800"
    assert _jmeter_property_value(timer, "scriptLanguage") == "groovy"
    assert "platform_pacing_next_start" in (_jmeter_property_value(timer, "script") or "")
    assert list(tree.iter("ConstantTimer")) == []


@pytest.mark.asyncio
async def test_run_meta_total_threads_uses_sum_of_custom_thread_groups(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_bin_home: Path,
    db: AsyncSession,
    monkeypatch,
) -> None:
    captured: dict = {}
    real_launch = jmeter_runner.launch_jmeter

    async def spy(cmd, **kw):
        captured["cmd"] = cmd
        return await real_launch(cmd, **kw)

    monkeypatch.setattr(jmeter_runner, "launch_jmeter", spy)

    case_id = await _create_case_with_jmx(auth_client, "r_thread_group_total", MULTI_THREAD_GROUP_JMX)
    resp = await auth_client.post(
        f"/testcase/run/{case_id}",
        json={
            "numThreads": "10",
            "rampTime": "1",
            "duration": "60",
            "slaveCount": 0,
            "threadGroupOverrides": [
                {"key": "thread_group:0", "name": "2_策略获取", "enabled": True, "mode": "custom", "numThreads": "7", "rampTime": "1"},
                {"key": "thread_group:1", "name": "7_获取应用版本", "enabled": True, "mode": "custom", "numThreads": "7", "rampTime": "1"},
                {"key": "thread_group:2", "name": "9_下载证书", "enabled": True, "mode": "custom", "numThreads": "1", "rampTime": "1"},
            ],
        },
    )

    assert resp.json()["code"] == 0
    await jmeter_runner.wait_for_completion(case_id, timeout=10.0)

    report = (await db.execute(select(Report).where(Report.test_case_id == case_id))).scalars().one()
    assert report.total_threads == 15

    assert captured["cmd"]
    report_root = Path(report.report_dir).resolve().parent
    meta = json.loads((report_root / "run_meta.json").read_text(encoding="utf-8"))
    assert meta["total_threads"] == 15
    assert meta["per_slave_threads"] == 15


@pytest.mark.asyncio
async def test_run_splits_custom_thread_group_threads_per_slave(
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

    case_id = await _create_case_with_jmx(auth_client, "r_tg_split", sample_jmx_bytes)
    for h in ("10.0.10.1", "10.0.10.2"):
        db.add(
            Node(
                name=h,
                type=NodeType.SLAVE.value,
                host=h,
                username="root",
                password="x",
                port=22,
                status=NodeStatus.ENABLE.value,
                health_status=1,
            )
        )
    await db.commit()

    resp = await auth_client.post(
        f"/testcase/run/{case_id}",
        json={
            "numThreads": "30",
            "rampTime": "10",
            "duration": "600",
            "slaveCount": 2,
            "threadGroupOverrides": [
                {
                    "name": "Default ThreadGroup",
                    "mode": "custom",
                    "numThreads": "100",
                    "rampTime": "1",
                    "duration": "60",
                }
            ],
        },
    )
    assert resp.json()["code"] == 0
    await jmeter_runner.wait_for_completion(case_id, timeout=10.0)

    from lxml import etree

    run_jmx = Path(captured["cmd"][captured["cmd"].index("-t") + 1])
    tree = etree.parse(str(run_jmx))
    for parent in tree.iter("ThreadGroup"):
        if parent.get("testname") == "Default ThreadGroup":
            for prop in parent.iter():
                if prop.get("name") == "ThreadGroup.num_threads":
                    assert prop.text == "50"
                    return
    pytest.fail("未找到 Default ThreadGroup 线程数配置")


@pytest.mark.asyncio
async def test_run_rejects_stale_thread_group_override_key(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_bin_home: Path,
    sample_jmx_bytes: bytes,
) -> None:
    case_id = await _create_case_with_jmx(auth_client, "r_tg_stale", sample_jmx_bytes)

    resp = await auth_client.post(
        f"/testcase/run/{case_id}",
        json={
            "numThreads": "30",
            "rampTime": "10",
            "duration": "600",
            "slaveCount": 0,
            "threadGroupOverrides": [
                {
                    "key": "thread_group:0",
                    "name": "Old ThreadGroup Name",
                    "mode": "custom",
                    "numThreads": "1",
                    "rampTime": "1",
                    "duration": "60",
                }
            ],
        },
    )

    body = resp.json()
    assert body["code"] == -1
    assert "线程组配置已过期" in body["message"]


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


@pytest.mark.asyncio
async def test_run_skips_slave_with_active_node_lease(
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

    busy = Node(
        name="busy-slave",
        type=NodeType.SLAVE.value,
        host="10.0.11.1",
        username="root",
        password="x",
        port=22,
        status=NodeStatus.ENABLE.value,
        health_status=1,
        region="长沙",
    )
    free = Node(
        name="free-slave",
        type=NodeType.SLAVE.value,
        host="10.0.11.2",
        username="root",
        password="x",
        port=22,
        status=NodeStatus.ENABLE.value,
        health_status=1,
        region="长沙",
    )
    db.add_all([busy, free])
    await db.commit()
    await db.refresh(busy)
    db.add(
        ExecutionNode(
            report_id=999,
            test_case_id=999,
            node_id=busy.id,
            node_host=busy.host,
            region="长沙",
            status="leased",
        )
    )
    await db.commit()

    case_id = await _create_case_with_jmx(auth_client, "r_lease_skip", sample_jmx_bytes)
    resp = await auth_client.post(
        f"/testcase/run/{case_id}",
        json={"numThreads": "10", "rampTime": "0", "duration": "60", "slaveCount": 1, "region": "长沙"},
    )

    assert resp.json()["code"] == 0
    assert "-R" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("-R") + 1] == "10.0.11.2:1099"
    await jmeter_runner.wait_for_completion(case_id, timeout=10.0)


@pytest.mark.asyncio
async def test_run_releases_node_lease_when_jmeter_finishes(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_bin_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
) -> None:
    db.add(
        Node(
            name="lease-release-slave",
            type=NodeType.SLAVE.value,
            host="10.0.11.3",
            username="root",
            password="x",
            port=22,
            status=NodeStatus.ENABLE.value,
            health_status=1,
            region="长沙",
        )
    )
    await db.commit()

    case_id = await _create_case_with_jmx(auth_client, "r_lease_release", sample_jmx_bytes)
    resp = await auth_client.post(
        f"/testcase/run/{case_id}",
        json={"numThreads": "10", "rampTime": "0", "duration": "60", "slaveCount": 1, "region": "长沙"},
    )

    assert resp.json()["code"] == 0
    report = (await db.execute(select(Report).where(Report.test_case_id == case_id))).scalar_one()
    await jmeter_runner.wait_for_completion(report.id, timeout=10.0)
    leases = (
        await db.execute(select(ExecutionNode).where(ExecutionNode.report_id == report.id))
    ).scalars().all()
    assert len(leases) == 1
    assert leases[0].status == "released"
    assert leases[0].released_at is not None


@pytest.mark.asyncio
async def test_run_rolls_back_status_when_node_lease_conflicts(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_bin_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.add(
        Node(
            name="lease-conflict-slave",
            type=NodeType.SLAVE.value,
            host="10.0.11.4",
            username="root",
            password="x",
            port=22,
            status=NodeStatus.ENABLE.value,
            health_status=1,
            region="长沙",
        )
    )
    await db.commit()
    case_id = await _create_case_with_jmx(auth_client, "r_lease_conflict", sample_jmx_bytes)

    from app.services import execution_node as execution_node_service

    async def conflict(*args, **kwargs):
        raise RuntimeError("压力机已被占用: 10.0.11.4")

    monkeypatch.setattr(execution_node_service, "lease_nodes", conflict)

    resp = await auth_client.post(
        f"/testcase/run/{case_id}",
        json={"numThreads": "10", "rampTime": "0", "duration": "60", "slaveCount": 1, "region": "长沙"},
    )
    body = resp.json()
    assert body["success"] is False
    assert "压力机已被占用" in body["message"]

    tc = (await db.execute(select(TestCase).where(TestCase.id == case_id))).scalar_one()
    report = (
        await db.execute(select(Report).where(Report.test_case_id == case_id))
    ).scalar_one()
    assert tc.status == TestCaseStatus.RUN_FAILED.value
    assert report.status == TestCaseStatus.RUN_FAILED.value
    assert "压力机已被占用" in report.response_data


@pytest.mark.asyncio
async def test_run_syncs_current_case_dependencies_to_selected_slaves(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_bin_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
    monkeypatch,
) -> None:
    """执行前应把当前用例 CSV/JAR 补同步到本次选中的压力机。"""
    from app.core import ssh as ssh_mod

    case_id = await _create_case_with_jmx(auth_client, "r_sync_deps", sample_jmx_bytes)
    await auth_client.post(
        f"/csv/upload/{case_id}",
        files={"csvFile": ("data.csv", b"a,b\n1,2\n", "text/csv")},
    )
    await auth_client.post(
        f"/jar/upload/{case_id}",
        files={"jarFile": ("dep.jar", b"x", "application/java-archive")},
    )

    scp_calls: list[tuple[str, str, bool]] = []

    async def tracking_scp(self, local_path: str, remote_dir: str, *, raise_on_error: bool = False) -> None:
        scp_calls.append((local_path, remote_dir, raise_on_error))

    monkeypatch.setattr(ssh_mod.SSHClient, "scp_file", tracking_scp)

    db.add(
        Node(
            name="sync-slave",
            type=NodeType.SLAVE.value,
            host="10.0.9.1",
            username="root",
            password="x",
            port=22,
            status=NodeStatus.ENABLE.value,
            health_status=1,
        )
    )
    await db.commit()

    resp = await auth_client.post(
        f"/testcase/run/{case_id}",
        json={"numThreads": "10", "rampTime": "0", "duration": "60", "slaveCount": 1},
    )
    assert resp.json()["code"] == 0
    await jmeter_runner.wait_for_completion(case_id, timeout=10.0)

    assert len(scp_calls) == 2
    assert all(call[2] is True for call in scp_calls)
    assert any(local_path.endswith("/csv/data.csv") for local_path, _, _ in scp_calls)
    assert any(local_path.endswith("/jar/dep.jar") for local_path, _, _ in scp_calls)


@pytest.mark.asyncio
async def test_run_syncs_upload_file_and_rewrites_run_jmx(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_bin_home: Path,
    db: AsyncSession,
    monkeypatch,
) -> None:
    """执行前应同步上传接口文件，并把 run JMX 中 File.path 改为平台路径。"""
    from app.core import ssh as ssh_mod

    case_id = await _create_case_with_jmx(auth_client, "r_sync_upload", UPLOAD_FILE_JMX)
    await auth_client.post(
        f"/uploadFile/upload/{case_id}",
        files={"uploadFile": ("avatar.jpg", b"img", "image/jpeg")},
    )

    scp_calls: list[tuple[str, str, bool]] = []

    async def tracking_scp(self, local_path: str, remote_dir: str, *, raise_on_error: bool = False) -> None:
        scp_calls.append((local_path, remote_dir, raise_on_error))

    monkeypatch.setattr(ssh_mod.SSHClient, "scp_file", tracking_scp)

    db.add(
        Node(
            name="upload-sync-slave",
            type=NodeType.SLAVE.value,
            host="10.0.9.2",
            username="root",
            password="x",
            port=22,
            status=NodeStatus.ENABLE.value,
            health_status=1,
        )
    )
    await db.commit()

    resp = await auth_client.post(
        f"/testcase/run/{case_id}",
        json={"numThreads": "10", "rampTime": "0", "duration": "60", "slaveCount": 1},
    )
    assert resp.json()["code"] == 0
    await jmeter_runner.wait_for_completion(case_id, timeout=10.0)

    upload_path = str(next(data_home.glob("r_sync_upload_*/upload/avatar.jpg")))
    assert any(local_path == upload_path and raise_on_error is True for local_path, _, raise_on_error in scp_calls)

    run_jmx = next(data_home.glob("r_sync_upload_*/jmx/run_*.jmx"))
    assert upload_path in run_jmx.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_run_splits_marked_csv_per_slave_and_rewrites_run_jmx(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_bin_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
    monkeypatch,
) -> None:
    """仅标记为 split_by_slave 的 CSV 会按本次 slave 切片，并改写 run JMX 到执行专属路径。"""
    from app.core import ssh as ssh_mod
    from app.models.csv import Csv

    captured: dict = {}
    real_launch = jmeter_runner.launch_jmeter

    async def spy(cmd, **kw):
        captured["cmd"] = cmd
        return await real_launch(cmd, **kw)

    monkeypatch.setattr(jmeter_runner, "launch_jmeter", spy)

    case_id = await _create_case_with_jmx(auth_client, "r_split_csv", sample_jmx_bytes)
    await auth_client.post(
        f"/csv/upload/{case_id}",
        files={"csvFile": ("data.csv", b"h1,h2\n1,a\n2,b\n3,c\n4,d\n", "text/csv")},
    )
    csv_obj = (await db.execute(select(Csv).where(Csv.test_case_id == case_id))).scalar_one()
    csv_obj.distribution_strategy = "split_by_slave"

    for h in ("10.0.8.1", "10.0.8.2"):
        db.add(
            Node(
                name=h,
                type=NodeType.SLAVE.value,
                host=h,
                username="root",
                password="x",
                port=22,
                status=NodeStatus.ENABLE.value,
                health_status=1,
            )
        )
    await db.commit()

    scp_payloads: dict[str, bytes] = {}

    async def capture_scp(self, local_path: str, remote_dir: str, *, raise_on_error: bool = False) -> None:
        if "runtime_csv" in local_path:
            scp_payloads[self.host] = Path(local_path).read_bytes()

    monkeypatch.setattr(ssh_mod.SSHClient, "scp_file", capture_scp)

    resp = await auth_client.post(
        f"/testcase/run/{case_id}",
        json={"numThreads": "20", "rampTime": "0", "duration": "60", "slaveCount": 2},
    )
    assert resp.json()["code"] == 0
    await jmeter_runner.wait_for_completion(case_id, timeout=10.0)

    assert set(scp_payloads) == {"10.0.8.1", "10.0.8.2"}
    assert scp_payloads["10.0.8.1"] == b"h1,h2\n1,a\n3,c\n"
    assert scp_payloads["10.0.8.2"] == b"h1,h2\n2,b\n4,d\n"

    run_jmx = Path(captured["cmd"][captured["cmd"].index("-t") + 1])
    assert "runtime_csv" in run_jmx.read_text(encoding="utf-8")
    assert "report/" in run_jmx.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_run_splits_marked_csv_without_ignore_first_line(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_bin_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
    monkeypatch,
) -> None:
    """JMX 未配置 ignoreFirstLine 时，第一行也必须作为数据切片，避免 .dat/无表头文件丢数据。"""
    from app.core import ssh as ssh_mod
    from app.models.csv import Csv

    jmx_bytes = sample_jmx_bytes.replace(
        b'<boolProp name="ignoreFirstLine">true</boolProp>',
        b'<boolProp name="ignoreFirstLine">false</boolProp>',
    )
    case_id = await _create_case_with_jmx(auth_client, "r_split_dat", jmx_bytes)
    await auth_client.post(
        f"/csv/upload/{case_id}",
        files={"csvFile": ("data.csv", b"1,a\n2,b\n3,c\n4,d\n", "text/csv")},
    )
    csv_obj = (await db.execute(select(Csv).where(Csv.test_case_id == case_id))).scalar_one()
    csv_obj.distribution_strategy = "split_by_slave"

    for h in ("10.0.9.1", "10.0.9.2"):
        db.add(
            Node(
                name=h,
                type=NodeType.SLAVE.value,
                host=h,
                username="root",
                password="x",
                port=22,
                status=NodeStatus.ENABLE.value,
                health_status=1,
            )
        )
    await db.commit()

    scp_payloads: dict[str, bytes] = {}

    async def capture_scp(self, local_path: str, remote_dir: str, *, raise_on_error: bool = False) -> None:
        if "runtime_csv" in local_path:
            scp_payloads[self.host] = Path(local_path).read_bytes()

    monkeypatch.setattr(ssh_mod.SSHClient, "scp_file", capture_scp)

    resp = await auth_client.post(
        f"/testcase/run/{case_id}",
        json={"numThreads": "20", "rampTime": "0", "duration": "60", "slaveCount": 2},
    )
    assert resp.json()["code"] == 0
    await jmeter_runner.wait_for_completion(case_id, timeout=10.0)

    assert scp_payloads["10.0.9.1"] == b"1,a\n3,c\n"
    assert scp_payloads["10.0.9.2"] == b"2,b\n4,d\n"


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.asyncio
async def test_launch_stop_kills_jmeter_process_group_children(
    tmp_path: Path,
    db: AsyncSession,
) -> None:
    """停止 JMeter 时必须杀掉包装脚本派生的真实子进程。"""
    script = tmp_path / "fake_jmeter_wrapper.sh"
    child_pid_file = tmp_path / "child.pid"
    script.write_text(
        "\n".join(
            [
                "#!/bin/bash",
                f"sleep 60 & echo $! > {child_pid_file}",
                "wait",
            ]
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)

    tc = TestCase(name="stop_pg", status=TestCaseStatus.RUN_ING.value, test_case_dir=str(tmp_path))
    db.add(tc)
    await db.commit()
    await db.refresh(tc)
    rpt = Report(name="stop_pg", test_case_id=tc.id, status=TestCaseStatus.RUN_ING.value)
    db.add(rpt)
    await db.commit()
    await db.refresh(rpt)

    await jmeter_runner.launch_jmeter(
        [str(script)],
        testcase_id=tc.id,
        report_id=rpt.id,
        exec_type=2,
        jtl_path=None,
        log_file_path=str(tmp_path / "jmeter.log"),
    )

    child_pid = None
    try:
        for _ in range(40):
            if child_pid_file.exists():
                child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
                break
            await asyncio.sleep(0.05)
        assert child_pid is not None
        assert _pid_alive(child_pid)

        assert await jmeter_runner.launch_stop(rpt.id) is True

        for _ in range(40):
            if not _pid_alive(child_pid):
                break
            await asyncio.sleep(0.05)
        assert not _pid_alive(child_pid)
    finally:
        if child_pid and _pid_alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)


@pytest.mark.asyncio
async def test_jmeter_callback_update_failure_is_logged_and_registry_cleaned(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """JMeter 已退出但状态回写异常时，后台 task 不应静默失败或留下内存登记。"""
    script = tmp_path / "fake_jmeter_done.sh"
    script.write_text("#!/bin/bash\necho done\n", encoding="utf-8")
    script.chmod(0o755)
    report_id = 98765

    async def broken_update(*args, **kwargs) -> None:
        raise RuntimeError("db commit failed")

    exception_logs: list[str] = []

    def capture_exception(message: str, *args, **kwargs) -> None:
        exception_logs.append(message % args if args else message)

    monkeypatch.setattr(jmeter_runner, "_update_testcase_and_report", broken_update)
    monkeypatch.setattr(jmeter_runner.log, "exception", capture_exception)

    task = await jmeter_runner.launch_jmeter(
        [str(script)],
        testcase_id=12345,
        report_id=report_id,
        exec_type=ExecType.EXEC.value,
        jtl_path=None,
        log_file_path=str(tmp_path / "jmeter.log"),
    )
    await task

    assert any("JMeter 状态回写失败" in message for message in exception_logs)
    assert report_id not in jmeter_runner._running_processes
    assert report_id not in jmeter_runner._running_tasks


@pytest.mark.asyncio
async def test_stop_execution_marks_report_and_case_failed_immediately(
    auth_client: AsyncClient,
    db: AsyncSession,
    monkeypatch,
) -> None:
    """点击执行队列停止后应立即落库，避免页面持续显示运行中。"""
    async def fake_stop(report_id: int) -> bool:
        return True

    monkeypatch.setattr(jmeter_runner, "launch_stop", fake_stop)

    tc = TestCase(name="stop_exec", status=TestCaseStatus.RUN_ING.value, test_case_dir="/tmp/stop_exec")
    db.add(tc)
    await db.commit()
    await db.refresh(tc)
    rpt = Report(name="stop_exec", test_case_id=tc.id, status=TestCaseStatus.RUN_ING.value)
    db.add(rpt)
    await db.commit()
    await db.refresh(rpt)

    resp = await auth_client.get(f"/testcase/stopExecution/{rpt.id}")

    assert resp.json()["code"] == 0
    await db.refresh(tc)
    await db.refresh(rpt)
    assert tc.status == TestCaseStatus.RUN_FAILED.value
    assert rpt.status == TestCaseStatus.RUN_FAILED.value
    assert "用户手动停止" in rpt.response_data


@pytest.mark.asyncio
async def test_stop_execution_restarts_busy_remote_slave_engine(
    auth_client: AsyncClient,
    tmp_path: Path,
    db: AsyncSession,
    monkeypatch,
) -> None:
    """停止分布式执行时，slave 仍 busy 应自动重启 jmeter-server。"""
    from app.core import ssh as ssh_mod
    from app.services import testcase as testcase_service

    monkeypatch.setattr(jmeter_runner, "launch_stop", lambda report_id: asyncio.sleep(0, result=True))
    monkeypatch.setattr(testcase_service, "_REMOTE_STOP_WAIT_SECONDS", 0)
    monkeypatch.setattr(testcase_service, "_REMOTE_RESTART_WAIT_SECONDS", 0)

    commands: list[str] = []

    async def tracking_exec(self, command: str) -> str:
        commands.append(command)
        if "ps aux" in command and "grep jmeter-server" in command and "kill" not in command:
            return "root 12345 ... jmeter-server"
        if "jmeter-server" in command and "rmi.server.hostname" in command:
            return "Using local port: 1099"
        return "null"

    monkeypatch.setattr(ssh_mod.SSHClient, "exec_command", tracking_exec)

    db.add(Config(config_key="SLAVE_JMETER_BIN_HOME", config_value="/opt/jmeter/bin"))
    db.add(Config(config_key="SLAVE_JMETER_LOG_HOME", config_value="/opt/jmeter/log"))
    node = Node(
        name="slave-97",
        type=NodeType.SLAVE.value,
        host="10.10.27.97",
        username="root",
        password="x",
        port=22,
        status=NodeStatus.ENABLE.value,
        health_status=1,
    )
    db.add(node)
    tc = TestCase(name="remote_stop", status=TestCaseStatus.RUN_ING.value, test_case_dir=str(tmp_path))
    db.add(tc)
    await db.commit()
    await db.refresh(tc)

    report_root = tmp_path / "report" / "2026-05-28-09:11:22"
    data_dir = report_root / "data"
    data_dir.mkdir(parents=True)
    (report_root / "run_meta.json").write_text(
        '{"slave_hosts":["10.10.27.97:1099"]}',
        encoding="utf-8",
    )
    rpt = Report(
        name="remote_stop",
        test_case_id=tc.id,
        report_dir=str(data_dir) + os.sep,
        status=TestCaseStatus.RUN_ING.value,
    )
    db.add(rpt)
    await db.commit()
    await db.refresh(rpt)

    resp = await auth_client.get(f"/testcase/stopExecution/{rpt.id}")

    assert resp.json()["code"] == 0
    assert any("/opt/jmeter/bin/shutdown.sh" in cmd for cmd in commands)
    assert any("xargs kill -9" in cmd for cmd in commands)
    assert any("jmeter-server -Djava.rmi.server.hostname=10.10.27.97" in cmd for cmd in commands)

    await jmeter_runner._update_testcase_and_report(
        tc.id,
        rpt.id,
        TestCaseStatus.RUN_SUCCESS,
        "late success callback",
    )
    await db.refresh(tc)
    await db.refresh(rpt)
    assert tc.status == TestCaseStatus.RUN_FAILED.value
    assert rpt.status == TestCaseStatus.RUN_FAILED.value
    assert "用户手动停止" in rpt.response_data


@pytest.mark.asyncio
async def test_stop_execution_marks_remote_slave_unhealthy_when_restart_fails(
    auth_client: AsyncClient,
    tmp_path: Path,
    db: AsyncSession,
    monkeypatch,
) -> None:
    """远程 jmeter-server 重启后仍不可用时，应标记压力机不健康，避免后续继续分配。"""
    from app.core import ssh as ssh_mod
    from app.services import testcase as testcase_service

    monkeypatch.setattr(jmeter_runner, "launch_stop", lambda report_id: asyncio.sleep(0, result=True))
    monkeypatch.setattr(testcase_service, "_REMOTE_STOP_WAIT_SECONDS", 0)
    monkeypatch.setattr(testcase_service, "_REMOTE_RESTART_WAIT_SECONDS", 0)

    ps_checks = 0

    async def failed_restart_exec(self, command: str) -> str:
        nonlocal ps_checks
        if "ps aux" in command and "grep jmeter-server" in command and "kill" not in command:
            ps_checks += 1
            if ps_checks == 1:
                return "root 12345 ... jmeter-server"
            return "null"
        if "jmeter-server" in command and "rmi.server.hostname" in command:
            return "null"
        return "null"

    monkeypatch.setattr(ssh_mod.SSHClient, "exec_command", failed_restart_exec)

    db.add(Config(config_key="SLAVE_JMETER_BIN_HOME", config_value="/opt/jmeter/bin"))
    db.add(Config(config_key="SLAVE_JMETER_LOG_HOME", config_value="/opt/jmeter/log"))
    node = Node(
        name="slave-111",
        type=NodeType.SLAVE.value,
        host="10.10.27.111",
        username="root",
        password="x",
        port=22,
        status=NodeStatus.ENABLE.value,
        health_status=1,
    )
    db.add(node)
    tc = TestCase(name="remote_stop_failed", status=TestCaseStatus.RUN_ING.value, test_case_dir=str(tmp_path))
    db.add(tc)
    await db.commit()
    await db.refresh(tc)

    report_root = tmp_path / "report" / "2026-05-28-13:50:50"
    data_dir = report_root / "data"
    data_dir.mkdir(parents=True)
    (report_root / "run_meta.json").write_text(
        '{"slave_hosts":["10.10.27.111:1099"]}',
        encoding="utf-8",
    )
    rpt = Report(
        name="remote_stop_failed",
        test_case_id=tc.id,
        report_dir=str(data_dir) + os.sep,
        status=TestCaseStatus.RUN_ING.value,
    )
    db.add(rpt)
    await db.commit()
    await db.refresh(rpt)

    resp = await auth_client.get(f"/testcase/stopExecution/{rpt.id}")

    assert resp.json()["code"] == 0
    await db.refresh(node)
    await db.refresh(rpt)
    assert node.health_status == 0
    assert "压力机清理失败: 10.10.27.111" in rpt.response_data


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

    async def tracking_scp(self, local_path: str, remote_dir: str, *, raise_on_error: bool = False) -> None:
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


@pytest.mark.asyncio
async def test_sync_node_reports_scp_failure(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
    monkeypatch,
) -> None:
    """点击同步时如果文件同步失败，应返回明确错误而不是吞掉失败。"""
    from app.core import ssh as ssh_mod
    from app.core.exceptions import MysteriousException

    case_id = await _create_case_with_jmx(auth_client, "sn_fail", sample_jmx_bytes)
    await auth_client.post(
        f"/csv/upload/{case_id}",
        files={"csvFile": ("data.csv", b"a,b\n1,2\n", "text/csv")},
    )

    async def failing_scp(self, local_path: str, remote_dir: str, *, raise_on_error: bool = False) -> None:
        if raise_on_error:
            raise MysteriousException(message="scp failed")

    monkeypatch.setattr(ssh_mod.SSHClient, "scp_file", failing_scp)

    n = Node(
        name="newslv-fail",
        type=NodeType.SLAVE.value,
        host="10.0.0.100",
        username="root",
        password="x",
        port=22,
        status=NodeStatus.DISABLED.value,
    )
    db.add(n)
    await db.commit()
    await db.refresh(n)

    resp = await auth_client.get(f"/testcase/syncNode/{n.id}")
    body = resp.json()
    assert body["code"] == -1
    assert "同步失败" in body["message"]
    assert "data.csv" in body["message"]


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
