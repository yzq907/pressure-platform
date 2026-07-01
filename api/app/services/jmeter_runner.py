"""JMeter 子进程异步执行 + 完成回调。

对齐 Java JmxService.runJmx / debugJmx / stopJmx + DebugResultHandler / ExecuteResultHandler /
StopResultHandler 三个回调里的状态机和日志/JTL 解析。

事件流：
1) route → debug/run_testcase()：建报告 + testcase.status=RUN_ING → 调 launch_jmeter()
2) launch_jmeter() 立即返回，asyncio.Task 后台跑子进程
3) 子进程完成 → callback 解析 stdout / jmeter.log / jtl → 写 testcase.status + report.status + report.response_data

测试入口：`await wait_for_completion(report_id)` 同步等待。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import socket
import subprocess
import time
from asyncio.subprocess import PIPE, Process
from datetime import datetime
from pathlib import Path

from lxml import etree
from sqlalchemy import select

from app.core.jmeter_error_samples import (
    ERROR_SAMPLE_FILENAME,
    ERROR_SAMPLE_XML_FILENAME,
    error_sample_dir_from_artifact_dir,
)
from app.core.ssh import SSHClient
from app.core.enums import ExecType, TestCaseStatus
from app.db import session as session_module
from app.models.execution_node import ExecutionNode
from app.models.node import Node
from app.models.execution_run import ExecutionRun
from app.models.report import Report
from app.models.testcase import TestCase

log = logging.getLogger(__name__)

# 内存登记后台子进程，按 report_id 索引，支持按执行独立 stop
_running_processes: dict[int, Process] = {}
_running_tasks: dict[int, asyncio.Task] = {}
_LOG_TAIL_LIMIT = 2000

_SUMMARY_ERROR_RE = re.compile(r"summary\s+=\s+0\s+in.*")
_RESULT_ERROR_RE = re.compile(r".*Err:\s+([1-9][0-9]*)\s+\(.*%\)")
_RUN_ERROR_RE = re.compile(r".*Error.*Exception")
_LOG_BEANSHELL_RE = re.compile(r".*Error invoking bsh method|.*NoClassDefFoundError")
_STOP_GRACE_SECONDS = 3.0
_KILL_GRACE_SECONDS = 2.0
_HEARTBEAT_INTERVAL_SECONDS = 5.0
_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
_ACTIVE_RUN_STATUSES = ("preparing", "running", "stopping")


def _now() -> datetime:
    return datetime.now()


def _check_output_failed(out: str) -> bool:
    """对齐 Java ResultHandler.checkResult 的三类 fail-patterns。"""
    return bool(
        _SUMMARY_ERROR_RE.search(out)
        or _RESULT_ERROR_RE.search(out)
        or _RUN_ERROR_RE.search(out)
    )


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _log_has_error(path: str) -> bool:
    """对齐 Java DebugResultHandler.checkJMeterLog：jmeter.log 里若有 beanshell/NoClassDef 报错 → 失败。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if _LOG_BEANSHELL_RE.search(line):
                    return True
    except OSError:
        pass
    return False


def _parse_debug_response_data(xml_path: str) -> str:
    """对齐 Java DebugResultHandler.getResponseData：
    优先 <assertionResult>/<failureMessage>；否则 <responseData class="java.lang.String">；
    最大 500 字符。"""
    try:
        tree = etree.parse(xml_path)
    except Exception:
        return ""
    for sample in tree.iter():
        if sample.tag in ("httpSample", "sample"):
            for child in sample:
                if child.tag == "assertionResult":
                    for grand in child:
                        if grand.tag == "failureMessage" and grand.text:
                            return grand.text[:500]
            for child in sample:
                if child.tag == "responseData" and child.get("class") == "java.lang.String":
                    return (child.text or "")[:500]
    return ""


async def launch_jmeter(
    cmd: list[str],
    *,
    testcase_id: int,
    report_id: int,
    exec_type: int,
    jtl_path: str | None,
    log_file_path: str,
) -> asyncio.Task:
    """启动 JMeter 子进程（fire-and-forget）。按 report_id 注册，支持独立 kill。

    返回 asyncio.Task；调用方通常忽略。测试用 `wait_for_completion(report_id)` 同步等待。
    """
    try:
        await _create_execution_run(
            cmd,
            testcase_id=testcase_id,
            report_id=report_id,
            jtl_path=jtl_path,
            log_file_path=log_file_path,
        )
    except Exception:
        log.exception("JMeter 执行记录创建失败，取消启动: report_id=%s testcase_id=%s", report_id, testcase_id)
        await _safe_update_testcase_and_report(
            testcase_id,
            report_id,
            TestCaseStatus.RUN_FAILED,
            "执行记录创建失败，JMeter 未启动",
        )
        raise
    if report_id in _running_tasks:
        log.warning("JMeter 后台任务已存在，将覆盖登记: report_id=%s", report_id)
    task = asyncio.create_task(
        _run_and_callback(cmd, testcase_id, report_id, exec_type, jtl_path, log_file_path),
        name=f"jmeter-report-{report_id}",
    )
    _running_tasks[report_id] = task
    task.add_done_callback(lambda done_task: _on_jmeter_task_done(report_id, done_task))
    log.info(
        "JMeter 后台任务已创建: report_id=%s testcase_id=%s exec_type=%s log=%s jtl=%s",
        report_id,
        testcase_id,
        exec_type,
        log_file_path,
        jtl_path,
    )
    return task


async def _create_execution_run(
    cmd: list[str],
    *,
    testcase_id: int,
    report_id: int,
    jtl_path: str | None,
    log_file_path: str,
) -> None:
    async with session_module.AsyncSessionLocal() as db:
        rpt = await db.get(Report, report_id)
        existing = (
            await db.execute(select(ExecutionRun).where(ExecutionRun.report_id == report_id))
        ).scalar_one_or_none()
        run = existing or ExecutionRun(report_id=report_id)
        run.test_case_id = testcase_id
        run.region = rpt.region if rpt is not None else ""
        run.status = "preparing"
        run.worker_id = _WORKER_ID
        run.pid = 0
        run.pgid = 0
        run.cmd = " ".join(cmd)
        run.jtl_path = jtl_path or ""
        run.log_path = log_file_path
        run.heartbeat_at = _now()
        run.started_at = None
        run.finished_at = None
        run.stop_requested_at = None
        run.exit_code = 0
        run.message = "JMeter 子进程准备启动"
        if existing is None:
            db.add(run)
        await db.commit()


async def _mark_execution_run_running(report_id: int, proc: Process) -> None:
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = 0
    await _update_execution_run(
        report_id,
        status="running",
        pid=proc.pid,
        pgid=pgid,
        heartbeat_at=_now(),
        started_at=_now(),
        message="JMeter 子进程已启动",
    )


async def _mark_execution_run_finished(
    report_id: int,
    *,
    status: str,
    exit_code: int,
    message: str,
) -> None:
    await _update_execution_run(
        report_id,
        status=status,
        exit_code=exit_code,
        heartbeat_at=_now(),
        finished_at=_now(),
        message=message,
    )


async def _release_execution_node_leases(report_id: int, message: str) -> None:
    try:
        from app.services import execution_node

        async with session_module.AsyncSessionLocal() as db:
            await execution_node.release_by_report(db, report_id, message=message)
    except Exception:
        log.exception("执行节点租约释放失败: report_id=%s", report_id)


async def _mark_execution_run_stopping(report_id: int, message: str) -> None:
    await _update_execution_run(
        report_id,
        status="stopping",
        stop_requested_at=_now(),
        heartbeat_at=_now(),
        message=message,
    )


async def _heartbeat_execution_run(report_id: int) -> None:
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
        await _update_execution_run(report_id, heartbeat_at=_now())


async def _execution_run_status(report_id: int) -> str:
    async with session_module.AsyncSessionLocal() as db:
        run = (
            await db.execute(select(ExecutionRun).where(ExecutionRun.report_id == report_id))
        ).scalar_one_or_none()
        return run.status if run is not None else ""


async def _update_execution_run(report_id: int, **values) -> None:
    try:
        async with session_module.AsyncSessionLocal() as db:
            run = (
                await db.execute(select(ExecutionRun).where(ExecutionRun.report_id == report_id))
            ).scalar_one_or_none()
            if run is None:
                return
            for key, value in values.items():
                setattr(run, key, value)
            await db.commit()
    except Exception:
        log.exception("执行记录更新失败: report_id=%s values=%s", report_id, sorted(values))


def _on_jmeter_task_done(report_id: int, task: asyncio.Task) -> None:
    _running_tasks.pop(report_id, None)
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        log.warning("JMeter 后台任务被取消: report_id=%s", report_id)
        return
    if exc is not None:
        log.error(
            "JMeter 后台任务异常退出: report_id=%s",
            report_id,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return
    log.info("JMeter 后台任务已结束: report_id=%s", report_id)


async def launch_stop(report_id: int) -> bool:
    """按 report_id 停止指定执行。

    返回 True 表示找到了进程并 kill；False 表示未找到（已结束或从未启动）。
    """
    await _mark_execution_run_stopping(report_id, "收到停止请求")
    stopped = False
    proc = _running_processes.get(report_id)
    if proc is None:
        log.info("stop: report_id=%s 未找到运行中的进程", report_id)
    else:
        stopped = await _terminate_process_group(proc, report_id)

    recorded_stopped = await _terminate_recorded_execution_process(report_id)
    orphan_stopped = await _terminate_orphan_jmeter_processes(report_id)
    return stopped or recorded_stopped or orphan_stopped


async def _terminate_recorded_execution_process(report_id: int) -> bool:
    """Use persisted pid/pgid to stop a run after in-memory process handles are lost."""
    async with session_module.AsyncSessionLocal() as db:
        run = (
            await db.execute(
                select(ExecutionRun).where(
                    ExecutionRun.report_id == report_id,
                    ExecutionRun.status.in_(_ACTIVE_RUN_STATUSES),
                )
            )
        ).scalar_one_or_none()
        if run is None:
            return False
        pid = run.pid
        pgid = run.pgid
        rpt = await db.get(Report, report_id)
        markers = _execution_run_markers(run, rpt)

    if pid <= 0 and pgid <= 0:
        return False
    if not _recorded_process_matches_markers(pid, markers):
        log.warning(
            "stop: report_id=%s 跳过执行记录进程停止，当前 pid 命令行与报告标识不匹配 pid=%s pgid=%s markers=%s",
            report_id,
            pid,
            pgid,
            markers,
        )
        return False

    current_pgid = os.getpgrp()
    try:
        if pgid > 0 and pgid != current_pgid:
            os.killpg(pgid, signal.SIGTERM)
        elif pid > 0:
            os.kill(pid, signal.SIGTERM)
        else:
            return False
    except ProcessLookupError:
        return True
    except OSError:
        log.exception("stop: report_id=%s 停止执行记录进程失败 pid=%s pgid=%s", report_id, pid, pgid)
        return False

    deadline = time.monotonic() + _STOP_GRACE_SECONDS
    while time.monotonic() < deadline:
        if pid <= 0 or not _pid_exists(pid):
            return True
        await asyncio.sleep(0.1)

    try:
        if pgid > 0 and pgid != current_pgid:
            os.killpg(pgid, signal.SIGKILL)
        elif pid > 0:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        log.exception("stop: report_id=%s 强制停止执行记录进程失败 pid=%s pgid=%s", report_id, pid, pgid)
        return False
    return True


def _execution_run_markers(run: ExecutionRun, rpt: Report | None) -> list[str]:
    candidates = [
        run.log_path,
        run.jtl_path,
        rpt.jmeter_log_file_path if rpt is not None else "",
        rpt.report_dir if rpt is not None else "",
    ]
    markers: list[str] = []
    for marker in candidates:
        if marker and marker not in markers:
            markers.append(marker)
    return markers


def _recorded_process_matches_markers(pid: int, markers: list[str]) -> bool:
    if pid <= 0 or not markers:
        return False
    cmdline = _read_process_cmdline(pid)
    if not cmdline:
        return False
    return any(marker in cmdline for marker in markers)


def _read_process_cmdline(pid: int) -> str:
    proc_cmdline = f"/proc/{pid}/cmdline"
    try:
        with open(proc_cmdline, "rb") as f:
            data = f.read()
        return data.replace(b"\x00", b" ").decode("utf-8", errors="replace")
    except OSError:
        pass

    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    return result.stdout.strip()


async def _terminate_process_group(proc: Process, report_id: int) -> bool:
    """停止 JMeter 进程组，避免只杀 shell 包装脚本而留下 Java 子进程。"""
    try:
        if proc.returncode is not None:
            return True
        pgid = os.getpgid(proc.pid)
        current_pgid = os.getpgrp()
        if pgid == current_pgid:
            log.warning("stop: report_id=%s 子进程组等于当前服务进程组，仅停止子进程", report_id)
            proc.terminate()
        else:
            os.killpg(pgid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_SECONDS)
            return True
        except asyncio.TimeoutError:
            if pgid == current_pgid:
                proc.kill()
            else:
                os.killpg(pgid, signal.SIGKILL)
            try:
                await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_SECONDS)
            except asyncio.TimeoutError:
                log.warning("stop: report_id=%s 强制停止后进程仍未退出", report_id)
            return True
    except ProcessLookupError:
        return True
    except Exception:
        log.exception("kill 子进程失败 report_id=%s", report_id)
        return False


async def _terminate_orphan_jmeter_processes(report_id: int) -> bool:
    """根据报告文件路径兜底查杀孤儿 JMeter Java 进程。

    后端 reload/restart 或 shell 包装脚本退出后，内存中的 Process 句柄可能丢失。
    JMeter 命令行会包含 -j 的日志路径或 -o 的报告目录，可以用它精准匹配本次执行。
    """
    async with session_module.AsyncSessionLocal() as db:
        rpt = await db.get(Report, report_id)
        if rpt is None:
            return False
        markers = [m for m in (rpt.jmeter_log_file_path, rpt.report_dir) if m]

    if not markers:
        return False

    killed = False
    current_pid = os.getpid()
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        log.exception("stop: report_id=%s 查询进程列表失败", report_id)
        return False

    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, args = stripped.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == current_pid:
            continue
        if "ApacheJMeter.jar" not in args and "/jmeter" not in args:
            continue
        if not any(marker in args for marker in markers):
            continue
        if _terminate_pid(pid, report_id):
            killed = True
    return killed


def _terminate_pid(pid: int, report_id: int) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        log.exception("stop: report_id=%s 停止孤儿进程失败 pid=%s", report_id, pid)
        return False
    deadline = time.monotonic() + _STOP_GRACE_SECONDS
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        log.exception("stop: report_id=%s 强制停止孤儿进程失败 pid=%s", report_id, pid)
        return False
    return True


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


async def wait_for_completion(report_id: int, timeout: float = 30.0) -> None:
    """测试用：等指定报告的后台 task 跑完。"""
    task = _running_tasks.get(report_id)
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            log.warning("wait_for_completion 等待后台任务超时 report_id=%s", report_id)
        return

    proc = _running_processes.get(report_id)
    if proc is None:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        log.warning("wait_for_completion 超时 report_id=%s", report_id)


async def _run_and_callback(
    cmd: list[str],
    testcase_id: int,
    report_id: int,
    exec_type: int,
    jtl_path: str | None,
    log_file_path: str,
) -> None:
    out_str = ""
    err_str = ""
    exit_code = -1
    proc = None
    heartbeat_task: asyncio.Task | None = None
    started_at = time.monotonic()
    try:
        log.info(
            "JMeter 启动开始: report_id=%s testcase_id=%s exec_type=%s cmd=%s",
            report_id,
            testcase_id,
            exec_type,
            " ".join(cmd),
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=PIPE,
            stderr=PIPE,
            start_new_session=True,
        )
        _running_processes[report_id] = proc
        await _mark_execution_run_running(report_id, proc)
        heartbeat_task = asyncio.create_task(
            _heartbeat_execution_run(report_id),
            name=f"jmeter-heartbeat-{report_id}",
        )
        log.info(
            "JMeter 进程已启动: report_id=%s testcase_id=%s pid=%s log=%s jtl=%s",
            report_id,
            testcase_id,
            proc.pid,
            log_file_path,
            jtl_path,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        exit_code = proc.returncode if proc.returncode is not None else -1
        out_str = stdout_bytes.decode("utf-8", errors="replace")
        err_str = stderr_bytes.decode("utf-8", errors="replace")
        log.info(
            "JMeter 进程已退出: report_id=%s testcase_id=%s pid=%s exit_code=%s duration=%.2fs stdout_tail=%s stderr_tail=%s",
            report_id,
            testcase_id,
            proc.pid,
            exit_code,
            time.monotonic() - started_at,
            _tail_for_log(out_str),
            _tail_for_log(err_str),
        )
    except Exception:
        log.exception(
            "JMeter 启动或执行异常: report_id=%s testcase_id=%s cmd=%s",
            report_id,
            testcase_id,
            cmd,
        )
        await _mark_execution_run_finished(
            report_id,
            status="failed",
            exit_code=exit_code,
            message="JMeter 启动或执行异常",
        )
        await _safe_update_testcase_and_report(testcase_id, report_id, TestCaseStatus.RUN_FAILED, None)
        await _release_execution_node_leases(report_id, "JMeter 启动或执行异常")
        _running_processes.pop(report_id, None)
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
        return
    finally:
        if heartbeat_task is not None and not heartbeat_task.done():
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

    # proc.kill() 会导致进程被杀→ returncode < 0，视为失败
    if exit_code < 0:
        final_status = TestCaseStatus.RUN_FAILED
        response_data = None
        output_failed = False
    else:
        output_failed = _check_output_failed(out_str)
        failed = output_failed or exit_code != 0
        final_status = TestCaseStatus.RUN_FAILED if failed else TestCaseStatus.RUN_SUCCESS

        response_data: str | None = None
        if exec_type == ExecType.DEBUG.value:
            if _file_size(log_file_path) >= 1024 * 1024:
                response_data = "调试日志过大, 请确认"
            elif final_status == TestCaseStatus.RUN_SUCCESS and jtl_path:
                response_data = _parse_debug_response_data(jtl_path)
            if _log_has_error(log_file_path):
                final_status = TestCaseStatus.RUN_FAILED

    log.info(
        "JMeter 状态判定完成: report_id=%s testcase_id=%s exit_code=%s output_failed=%s final_status=%s",
        report_id,
        testcase_id,
        exit_code,
        output_failed,
        final_status.value,
    )
    if exec_type == ExecType.EXEC.value:
        await _collect_remote_error_samples(report_id)
    updated = await _safe_update_testcase_and_report(testcase_id, report_id, final_status, response_data)
    run_status = "success" if final_status == TestCaseStatus.RUN_SUCCESS else "failed"
    run_message = "JMeter 执行完成" if run_status == "success" else "JMeter 执行失败"
    if await _execution_run_status(report_id) == "stopping":
        run_status = "failed"
        run_message = "JMeter 已按停止请求退出"
    await _mark_execution_run_finished(
        report_id,
        status=run_status,
        exit_code=exit_code,
        message=run_message,
    )
    await _release_execution_node_leases(report_id, run_message)
    if updated and exec_type == ExecType.EXEC.value:
        _schedule_metric_snapshot(report_id)
    _running_processes.pop(report_id, None)


async def _collect_remote_error_samples(report_id: int) -> None:
    try:
        async with session_module.AsyncSessionLocal() as db:
            rpt = await db.get(Report, report_id)
            if rpt is None or not rpt.artifact_dir:
                return
            rows = list(
                (
                    await db.execute(
                        select(Node)
                        .join(ExecutionNode, ExecutionNode.node_id == Node.id)
                        .where(ExecutionNode.report_id == report_id)
                        .order_by(ExecutionNode.id.asc())
                    )
                ).scalars().all()
            )
            if not rows:
                return
            error_sample_dir = error_sample_dir_from_artifact_dir(rpt.artifact_dir)
            error_sample_dir.mkdir(parents=True, exist_ok=True)
            merged_path = error_sample_dir / ERROR_SAMPLE_FILENAME
            remote_path = str(error_sample_dir / ERROR_SAMPLE_FILENAME)
            for node in rows:
                safe_host = re.sub(r"[^A-Za-z0-9_.-]", "_", node.host or str(node.id))
                tmp_path = error_sample_dir / f"error_samples.{safe_host}.jsonl"
                ssh = SSHClient(node.host, node.port, node.username, node.password)
                await ssh.fetch_file(remote_path, str(tmp_path), raise_on_error=False)
                if not tmp_path.exists() or tmp_path.stat().st_size <= 0:
                    continue
                with tmp_path.open("r", encoding="utf-8", errors="replace") as src:
                    with merged_path.open("a", encoding="utf-8") as dst:
                        for line in src:
                            if line.strip():
                                dst.write(line if line.endswith("\n") else line + "\n")
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
                remote_xml_path = str(error_sample_dir / ERROR_SAMPLE_XML_FILENAME)
                local_xml_path = error_sample_dir / f"error_samples.{safe_host}.xml"
                await ssh.fetch_file(remote_xml_path, str(local_xml_path), raise_on_error=False)
                if local_xml_path.exists() and local_xml_path.stat().st_size <= 0:
                    try:
                        local_xml_path.unlink()
                    except OSError:
                        pass
    except Exception:
        log.exception("分布式错误样本拉取失败: report_id=%s", report_id)


def _schedule_metric_snapshot(report_id: int) -> None:
    from app.services import report as report_service

    report_service.schedule_metric_snapshot_generation(report_id)
    report_service.schedule_transaction_snapshot_generation(report_id)
    report_service.schedule_transaction_metric_snapshot_generation(report_id)


def _tail_for_log(value: str, limit: int = _LOG_TAIL_LIMIT) -> str:
    if not value:
        return ""
    text = value[-limit:]
    return text.replace("\n", "\\n")


async def _safe_update_testcase_and_report(
    testcase_id: int,
    report_id: int,
    status: TestCaseStatus,
    response_data: str | None,
) -> bool:
    try:
        await _update_testcase_and_report(testcase_id, report_id, status, response_data)
    except Exception:
        log.exception(
            "JMeter 状态回写失败: report_id=%s testcase_id=%s status=%s response_data_present=%s",
            report_id,
            testcase_id,
            status.value,
            response_data is not None,
        )
        return False
    log.info(
        "JMeter 状态回写成功: report_id=%s testcase_id=%s status=%s response_data_present=%s",
        report_id,
        testcase_id,
        status.value,
        response_data is not None,
    )
    return True


async def _update_testcase_and_report(
    testcase_id: int,
    report_id: int,
    status: TestCaseStatus,
    response_data: str | None,
) -> None:
    async with session_module.AsyncSessionLocal() as db:
        rpt = await db.get(Report, report_id)
        if rpt is not None and rpt.status != TestCaseStatus.RUN_ING.value:
            log.info(
                "JMeter 回调跳过非运行中报告: report_id=%s current_status=%s callback_status=%s",
                report_id,
                rpt.status,
                status.value,
            )
            return
        tc = (
            await db.execute(select(TestCase).where(TestCase.id == testcase_id))
        ).scalar_one_or_none()
        if tc is not None:
            tc.status = status.value
        if rpt is not None:
            rpt.status = status.value
            rpt.modify_time = _now()
            if response_data is not None:
                rpt.response_data = response_data
            from app.services import execution_queue

            await execution_queue.sync_report_status(db, report_id, status)
        await db.commit()
