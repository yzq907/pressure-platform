"""TestCase debug/run/stop/syncNode/getFull/getJMeterResult 集成测试。

策略：
- 用 `jmeter_bin_home` fixture 提供一个 fake jmeter 脚本（写合规 stdout + log + jtl）
- 用 `mock_ssh` autouse fixture mock SSH（slave telnet + scp）
- 真实启动 asyncio.create_subprocess_exec 跑 fake_jmeter.sh，再 wait_for_completion 同步等待
"""

from __future__ import annotations

import asyncio
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
