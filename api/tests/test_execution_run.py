"""Execution run persistence tests."""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ExecType, TestCaseStatus
from app.main import _recover_stuck_tasks
from app.models.execution_run import ExecutionRun
from app.models.report import Report
from app.models.testcase import TestCase
from app.services import jmeter_runner


async def _wait_for_run(db: AsyncSession, report_id: int, status: str | None = None) -> ExecutionRun:
    for _ in range(60):
        await db.rollback()
        run = (
            await db.execute(select(ExecutionRun).where(ExecutionRun.report_id == report_id))
        ).scalar_one_or_none()
        if run is not None and (status is None or run.status == status):
            return run
        await asyncio.sleep(0.05)
    pytest.fail(f"execution run not found: report_id={report_id} status={status}")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_schedule_metric_snapshot_includes_error_sample_snapshot(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []

    monkeypatch.setattr(
        "app.services.report.schedule_metric_snapshot_generation",
        lambda report_id: calls.append(("metric", report_id)),
    )
    monkeypatch.setattr(
        "app.services.report.schedule_transaction_snapshot_generation",
        lambda report_id: calls.append(("transaction", report_id)),
    )
    monkeypatch.setattr(
        "app.services.report.schedule_transaction_metric_snapshot_generation",
        lambda report_id: calls.append(("transaction_metric", report_id)),
    )
    monkeypatch.setattr(
        "app.services.report.schedule_error_sample_snapshot_generation",
        lambda report_id: calls.append(("error", report_id)),
        raising=False,
    )

    jmeter_runner._schedule_metric_snapshot(17)

    assert calls == [
        ("metric", 17),
        ("transaction", 17),
        ("transaction_metric", 17),
        ("error", 17),
    ]


@pytest.mark.asyncio
async def test_launch_jmeter_persists_execution_run_lifecycle(
    tmp_path: Path,
    db: AsyncSession,
    monkeypatch,
) -> None:
    script = tmp_path / "fake_jmeter.sh"
    script.write_text("#!/bin/bash\nsleep 0.4\necho done\n", encoding="utf-8")
    script.chmod(0o755)
    tc = TestCase(name="run_lifecycle", status=TestCaseStatus.RUN_ING.value, test_case_dir=str(tmp_path))
    db.add(tc)
    await db.commit()
    await db.refresh(tc)
    rpt = Report(name="run_lifecycle", test_case_id=tc.id, status=TestCaseStatus.RUN_ING.value)
    db.add(rpt)
    await db.commit()
    await db.refresh(rpt)
    old_report_time = datetime(2026, 1, 1, 10, 0, 0)
    rpt.create_time = old_report_time
    rpt.modify_time = old_report_time
    await db.commit()
    testcase_id = tc.id
    report_id = rpt.id
    metric_tracking_calls: list[tuple[str, int, str | None]] = []

    def fake_start_metric_tracking(tracked_report_id: int, jtl_path: str | None) -> bool:
        metric_tracking_calls.append(("start", tracked_report_id, jtl_path))
        return True

    async def fake_stop_metric_tracking(tracked_report_id: int) -> None:
        metric_tracking_calls.append(("stop", tracked_report_id, None))

    monkeypatch.setattr(
        jmeter_runner,
        "_start_realtime_metric_tracking",
        fake_start_metric_tracking,
        raising=False,
    )
    monkeypatch.setattr(
        jmeter_runner,
        "_stop_realtime_metric_tracking",
        fake_stop_metric_tracking,
        raising=False,
    )

    await jmeter_runner.launch_jmeter(
        [str(script)],
        testcase_id=testcase_id,
        report_id=report_id,
        exec_type=ExecType.EXEC.value,
        jtl_path=str(tmp_path / "result.jtl"),
        log_file_path=str(tmp_path / "jmeter.log"),
    )

    running = await _wait_for_run(db, report_id, "running")
    assert running.pid > 0
    assert running.pgid > 0
    assert running.worker_id
    assert running.cmd == str(script)
    assert running.jtl_path.endswith("result.jtl")
    assert running.log_path.endswith("jmeter.log")

    await jmeter_runner.wait_for_completion(report_id, timeout=5.0)
    await db.refresh(running)
    assert running.status == "success"
    assert running.exit_code == 0
    assert running.finished_at is not None
    await db.refresh(rpt)
    assert rpt.modify_time > old_report_time
    assert metric_tracking_calls == [
        ("start", report_id, str(tmp_path / "result.jtl")),
        ("stop", report_id, None),
    ]


@pytest.mark.asyncio
async def test_launch_stop_uses_execution_run_process_group_when_memory_registry_lost(
    tmp_path: Path,
    db: AsyncSession,
) -> None:
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
    tc = TestCase(name="stop_by_db", status=TestCaseStatus.RUN_ING.value, test_case_dir=str(tmp_path))
    db.add(tc)
    await db.commit()
    await db.refresh(tc)
    rpt = Report(name="stop_by_db", test_case_id=tc.id, status=TestCaseStatus.RUN_ING.value)
    db.add(rpt)
    await db.commit()
    await db.refresh(rpt)
    testcase_id = tc.id
    report_id = rpt.id

    log_path = str(tmp_path / "jmeter.log")
    await jmeter_runner.launch_jmeter(
        [str(script), log_path],
        testcase_id=testcase_id,
        report_id=report_id,
        exec_type=ExecType.EXEC.value,
        jtl_path=None,
        log_file_path=log_path,
    )
    await _wait_for_run(db, report_id, "running")
    for _ in range(60):
        if child_pid_file.exists():
            child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("child process did not start")
    assert _pid_alive(child_pid)

    jmeter_runner._running_processes.pop(report_id, None)

    try:
        assert await jmeter_runner.launch_stop(report_id) is True
        for _ in range(60):
            if not _pid_alive(child_pid):
                break
            await asyncio.sleep(0.05)
        assert not _pid_alive(child_pid)
    finally:
        if _pid_alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)
    await jmeter_runner.wait_for_completion(report_id, timeout=5.0)


@pytest.mark.asyncio
async def test_launch_stop_does_not_kill_recorded_pid_when_marker_mismatches(
    tmp_path: Path,
    db: AsyncSession,
    monkeypatch,
) -> None:
    tc = TestCase(name="stop_marker_mismatch", status=TestCaseStatus.RUN_ING.value, test_case_dir=str(tmp_path))
    db.add(tc)
    await db.commit()
    await db.refresh(tc)
    rpt = Report(
        name="stop_marker_mismatch",
        test_case_id=tc.id,
        status=TestCaseStatus.RUN_ING.value,
        report_dir=str(tmp_path / "report" / "data"),
        jmeter_log_file_path=str(tmp_path / "report" / "jmeter.log"),
    )
    db.add(rpt)
    await db.commit()
    await db.refresh(rpt)
    run = ExecutionRun(
        report_id=rpt.id,
        test_case_id=tc.id,
        status="running",
        pid=12345,
        pgid=0,
        cmd="/usr/bin/java -jar unrelated.jar",
        jtl_path=str(tmp_path / "report" / "jtl" / "result.jtl"),
        log_path=str(tmp_path / "report" / "jmeter.log"),
    )
    db.add(run)
    await db.commit()

    def fail_kill(pid: int, sig: int) -> None:
        raise AssertionError(f"unexpected kill pid={pid} sig={sig}")

    async def fake_terminate_orphans(report_id: int) -> bool:
        return False

    monkeypatch.setattr(jmeter_runner.os, "kill", fail_kill)
    monkeypatch.setattr(jmeter_runner, "_terminate_orphan_jmeter_processes", fake_terminate_orphans)

    stopped = await jmeter_runner.launch_stop(rpt.id)

    assert stopped is False


@pytest.mark.asyncio
async def test_startup_recovery_marks_stale_execution_run_lost(
    tmp_path: Path,
    db: AsyncSession,
) -> None:
    tc = TestCase(name="stale_run", status=TestCaseStatus.RUN_ING.value, test_case_dir=str(tmp_path))
    db.add(tc)
    await db.commit()
    await db.refresh(tc)
    rpt = Report(name="stale_run", test_case_id=tc.id, status=TestCaseStatus.RUN_ING.value)
    db.add(rpt)
    await db.commit()
    await db.refresh(rpt)
    run = ExecutionRun(
        report_id=rpt.id,
        test_case_id=tc.id,
        status="running",
        worker_id="old-worker",
        pid=99999999,
        pgid=99999999,
        cmd="/tmp/missing-jmeter",
    )
    db.add(run)
    await db.commit()

    await _recover_stuck_tasks()

    await db.refresh(tc)
    await db.refresh(rpt)
    await db.refresh(run)
    assert tc.status == TestCaseStatus.RUN_FAILED.value
    assert rpt.status == TestCaseStatus.RUN_FAILED.value
    assert run.status == "lost"
    assert run.finished_at is not None


@pytest.mark.asyncio
async def test_launch_jmeter_marks_report_failed_when_execution_run_create_fails(
    tmp_path: Path,
    db: AsyncSession,
    monkeypatch,
) -> None:
    script = tmp_path / "fake_jmeter.sh"
    script.write_text("#!/bin/bash\necho should-not-run\n", encoding="utf-8")
    script.chmod(0o755)
    tc = TestCase(name="create_run_failed", status=TestCaseStatus.RUN_ING.value, test_case_dir=str(tmp_path))
    db.add(tc)
    await db.commit()
    await db.refresh(tc)
    rpt = Report(name="create_run_failed", test_case_id=tc.id, status=TestCaseStatus.RUN_ING.value)
    db.add(rpt)
    await db.commit()
    await db.refresh(rpt)
    testcase_id = tc.id
    report_id = rpt.id

    async def broken_create(*args, **kwargs) -> None:
        raise RuntimeError("execution run insert failed")

    monkeypatch.setattr(jmeter_runner, "_create_execution_run", broken_create)

    with pytest.raises(RuntimeError, match="execution run insert failed"):
        await jmeter_runner.launch_jmeter(
            [str(script)],
            testcase_id=testcase_id,
            report_id=report_id,
            exec_type=ExecType.EXEC.value,
            jtl_path=None,
            log_file_path=str(tmp_path / "jmeter.log"),
        )

    await db.refresh(tc)
    await db.refresh(rpt)
    assert tc.status == TestCaseStatus.RUN_FAILED.value
    assert rpt.status == TestCaseStatus.RUN_FAILED.value
    assert "执行记录创建失败" in rpt.response_data
    assert report_id not in jmeter_runner._running_tasks
    assert report_id not in jmeter_runner._running_processes
