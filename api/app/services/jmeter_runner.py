"""JMeter 子进程异步执行 + 完成回调。

对齐 Java JmxService.runJmx / debugJmx / stopJmx + DebugResultHandler / ExecuteResultHandler /
StopResultHandler 三个回调里的状态机和日志/JTL 解析。

Phase 5 简化：
- 不写 Redis 排队（Java 在 startCaseFromRedis 触发下一个）→ 不再触发
- ExecuteResultHandler 在 Java 里也是基于 stdout summary 判失败的；这里复用同一 checkResult 逻辑

事件流：
1) route → debug/run_testcase()：建报告 + testcase.status=RUN_ING → 调 launch_jmeter()
2) launch_jmeter() 立即返回，asyncio.Task 后台跑子进程
3) 子进程完成 → callback 解析 stdout / jmeter.log / jtl → 写 testcase.status + report.status + report.response_data

测试入口：`await wait_for_completion(testcase_id)` 同步等待。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from asyncio.subprocess import PIPE

from lxml import etree
from sqlalchemy import select

from app.core.enums import ExecType, TestCaseStatus
from app.db import session as session_module
from app.models.report import Report
from app.models.testcase import TestCase

log = logging.getLogger(__name__)

# 内存登记后台 task。stop 不依赖这个；但是测试可以通过 wait_for_completion 同步等待。
_running_tasks: dict[int, asyncio.Task] = {}

_SUMMARY_ERROR_RE = re.compile(r"summary\s+=\s+0\s+in.*")
_RESULT_ERROR_RE = re.compile(r".*Err:\s+([1-9][0-9]*)\s+\(.*%\)")
_RUN_ERROR_RE = re.compile(r".*Error.*Exception")
_LOG_BEANSHELL_RE = re.compile(r".*Error invoking bsh method|.*NoClassDefFoundError")


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
            # 1) 断言失败
            for child in sample:
                if child.tag == "assertionResult":
                    for grand in child:
                        if grand.tag == "failureMessage" and grand.text:
                            return grand.text[:500]
            # 2) responseData
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
    """启动 JMeter 子进程（fire-and-forget）。回调内更新 DB。

    返回 asyncio.Task；调用方通常忽略。测试用 `wait_for_completion(testcase_id)` 同步等待。
    """
    task = asyncio.create_task(
        _run_and_callback(cmd, testcase_id, report_id, exec_type, jtl_path, log_file_path)
    )
    _running_tasks[testcase_id] = task
    return task


async def launch_stop(cmd: list[str], *, testcase_id: int) -> asyncio.Task:
    """启动 shutdown.sh。回调内把 testcase.status RUN_ING → RUN_SUCCESS（对齐 StopResultHandler）。"""
    task = asyncio.create_task(_run_stop_callback(cmd, testcase_id))
    # 不放进 _running_tasks，因为 stop 是触发 shutdown 给 master 进程发信号，跟 run task 是两个独立进程
    return task


async def wait_for_completion(testcase_id: int, timeout: float = 30.0) -> None:
    """测试用：等指定用例的后台 task 跑完。已经跑完或者从未启动则立即返回。"""
    task = _running_tasks.get(testcase_id)
    if task is None:
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        log.warning("wait_for_completion 超时 testcase_id=%s", testcase_id)


async def _run_and_callback(
    cmd: list[str],
    testcase_id: int,
    report_id: int,
    exec_type: int,
    jtl_path: str | None,
    log_file_path: str,
) -> None:
    out_str = ""
    exit_code = -1
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
        stdout_bytes, _ = await proc.communicate()
        exit_code = proc.returncode or 0
        out_str = stdout_bytes.decode("utf-8", errors="replace")
    except Exception:
        log.exception("JMeter 启动失败 cmd=%s", cmd)
        try:
            await _update_testcase_and_report(testcase_id, report_id, TestCaseStatus.RUN_FAILED, None)
        finally:
            _running_tasks.pop(testcase_id, None)
        return

    failed = _check_output_failed(out_str) or exit_code != 0
    final_status = TestCaseStatus.RUN_FAILED if failed else TestCaseStatus.RUN_SUCCESS

    response_data: str | None = None
    if exec_type == ExecType.DEBUG.value:
        # log 过大优先级最高
        if _file_size(log_file_path) >= 1024 * 1024:
            response_data = "调试日志过大, 请确认"
        elif final_status == TestCaseStatus.RUN_SUCCESS and jtl_path:
            response_data = _parse_debug_response_data(jtl_path)
        # jmeter.log 里有 beanshell / NoClassDef 错误 → 强制失败
        if _log_has_error(log_file_path):
            final_status = TestCaseStatus.RUN_FAILED

    try:
        await _update_testcase_and_report(testcase_id, report_id, final_status, response_data)
    finally:
        _running_tasks.pop(testcase_id, None)


async def _run_stop_callback(cmd: list[str], testcase_id: int) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
        await proc.communicate()
    except Exception:
        log.exception("shutdown.sh 启动失败 cmd=%s", cmd)
    # 完成后：若用例状态仍是 RUN_ING，则改为 RUN_SUCCESS
    async with session_module.AsyncSessionLocal() as db:
        tc = (
            await db.execute(select(TestCase).where(TestCase.id == testcase_id))
        ).scalar_one_or_none()
        if tc is not None and tc.status == TestCaseStatus.RUN_ING.value:
            tc.status = TestCaseStatus.RUN_SUCCESS.value
            await db.commit()


async def _update_testcase_and_report(
    testcase_id: int,
    report_id: int,
    status: TestCaseStatus,
    response_data: str | None,
) -> None:
    async with session_module.AsyncSessionLocal() as db:
        tc = (
            await db.execute(select(TestCase).where(TestCase.id == testcase_id))
        ).scalar_one_or_none()
        if tc is not None:
            tc.status = status.value
        rpt = await db.get(Report, report_id)
        if rpt is not None:
            rpt.status = status.value
            if response_data is not None:
                rpt.response_data = response_data
        await db.commit()
