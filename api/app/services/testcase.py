"""TestCase 业务服务，对齐 Java ITestCaseService。

Phase 5 把 debug / run / stop / syncNode / getFull / getJMeterResult 一起补齐，但跳过：
- Redis 排队（用户已确认改为直接抛 TESTCASE_IS_RUNNING）
- JMX 在线编辑相关字段（Phase 4 再补）
"""

from __future__ import annotations

import logging
import math
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiofiles.os
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import stamp_create, stamp_modify
from app.core.codes import Codes
from app.core.context import UserContext
from app.core.enums import ExecType, NodeStatus, TestCaseStatus
from app.core.exceptions import MysteriousException
from app.core.response import PageVO
from app.core.ssh import SSHClient
from app.crud import csv as csv_crud
from app.crud import jar as jar_crud
from app.crud import jmx as jmx_crud
from app.crud import node as node_crud
from app.crud import report as report_crud
from app.crud import testcase as crud
from app.models.csv import Csv
from app.models.jar import Jar
from app.models.jmx import Jmx
from app.models.report import Report as ReportModel
from app.models.testcase import TestCase
from app.schemas.csv import CsvVO
from app.schemas.jar import JarVO
from app.schemas.jmx import JmxVO
from app.schemas.report import ReportParam
from app.schemas.testcase import (
    JMeterResultVO,
    RunParam,
    TestCaseFullVO,
    TestCaseParam,
    TestCaseQuery,
    TestCaseVO,
)
from app.services import config as config_service
from app.services import jmeter_runner
from app.services import report as report_service
from app.core.jmeter_xml import update_run_thread

log = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")

# 用例目录根，Java 对应配置 key 是 MASTER_DATA_HOME
DATA_HOME_KEY = "MASTER_DATA_HOME"
MASTER_JMETER_BIN_HOME_KEY = "MASTER_JMETER_BIN_HOME"

# Java 端 addTestCase 校验：name 不能含空格或 #
_BAD_NAME_CHARS = re.compile(r"[\s#]")

# JMeter 实时日志解析正则（对齐 Java getJMeterResult）
_JMETER_RESULT_RE = re.compile(
    r"\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2}),\d{3} INFO.*summary \+.* (\d+\.\d+)/s Avg: +(\d+)"
)


def _check_name(name: str | None) -> None:
    if not name or _BAD_NAME_CHARS.search(name):
        raise MysteriousException(Codes.TESTCASE_NAME_ERROR)


def _to_vo(obj: TestCase) -> TestCaseVO:
    return TestCaseVO.model_validate(obj)


async def add_testcase(db: AsyncSession, param: TestCaseParam, user: UserContext) -> int:
    _check_name(param.name)

    existing = await crud.get_by_name(db, param.name or "")
    if existing is not None:
        raise MysteriousException(Codes.TESTCASE_IS_EXIST)

    # 计算 test_case_dir = {MASTER_DATA_HOME}/{name}_{YYYY-MM-DD-HH:MM:SS}/
    data_home = await config_service.get_value(db, DATA_HOME_KEY)
    ts = datetime.now(SHANGHAI).strftime("%Y-%m-%d-%H:%M:%S")
    testcase_dir = os.path.join(data_home, f"{param.name}_{ts}") + os.sep

    obj = TestCase(
        name=param.name or "",
        description=param.description or "",
        biz=param.biz or "",
        service=param.service or "",
        version=param.version or "",
        num_threads=param.num_threads or "",
        ramp_time=param.ramp_time or "",
        duration=param.duration or "",
        timeout_seconds=param.timeout_seconds if param.timeout_seconds is not None else 7200,
        status=TestCaseStatus.NOT_RUN.value,
        test_case_dir=testcase_dir,
    )
    stamp_create(obj, user)
    await crud.add(db, obj)

    # 磁盘建目录（Java 端 fileUtils.mkDir）
    try:
        await aiofiles.os.makedirs(testcase_dir, exist_ok=True)
    except OSError as e:
        log.warning("mkdir failed for %s: %s", testcase_dir, e)
        raise MysteriousException(Codes.MKDIR_ERROR, message=str(e)) from e

    return obj.id


async def update_testcase(
    db: AsyncSession, id: int, param: TestCaseParam, user: UserContext
) -> bool:
    if param is None:
        raise MysteriousException(Codes.PARAMS_EMPTY)

    existing = await crud.get_by_id(db, id)
    if existing is None:
        raise MysteriousException(Codes.TESTCASE_NOT_EXIST)

    _check_name(param.name)

    sent = param.model_dump(exclude_unset=True, exclude_none=True, by_alias=False)

    # 改名时级联更新关联的 JMX/CSV/JAR 的 description（对齐 Java 行为）
    new_name = sent.get("name")
    name_changed = new_name is not None and new_name != existing.name
    if name_changed:
        await _cascade_description_on_rename(db, id, new_name, user)

    for field in ("name", "description", "biz", "service", "version", "num_threads", "ramp_time", "duration", "timeout_seconds"):
        if field in sent:
            setattr(existing, field, sent[field])
    stamp_modify(existing, user)
    return await crud.update(db, existing)


async def _cascade_description_on_rename(
    db: AsyncSession, testcase_id: int, new_name: str, user: UserContext
) -> None:
    """用例改名 → 同步 JMX/CSV/JAR 的 description = 新名字"""
    jmx = await jmx_crud.get_by_test_case_id(db, testcase_id)
    if jmx is not None:
        jmx.description = new_name
        stamp_modify(jmx, user)
        await jmx_crud.update(db, jmx)

    for csv_obj in await csv_crud.get_by_test_case_id(db, testcase_id):
        csv_obj.description = new_name
        stamp_modify(csv_obj, user)
        await csv_crud.update(db, csv_obj)

    for jar_obj in await jar_crud.get_by_test_case_id(db, testcase_id):
        jar_obj.description = new_name
        stamp_modify(jar_obj, user)
        await jar_crud.update(db, jar_obj)


async def delete_testcase(db: AsyncSession, id: int) -> bool:
    existing = await crud.get_by_id(db, id)
    if existing is None:
        return False
    # Java 端不删除磁盘目录，仅删除 DB 行
    return await crud.delete(db, id)


async def batch_delete_testcase(db: AsyncSession, ids: list[int]) -> bool:
    """Java 行为：循环调 deleteTestCase；不存在的 id 不抛异常"""
    for id in ids:
        await delete_testcase(db, id)
    return True


async def get_testcase_list(
    db: AsyncSession, query: TestCaseQuery
) -> PageVO[TestCaseVO]:
    page_vo: PageVO[TestCaseVO] = PageVO(page=query.page, size=query.size, total=0, list=[])
    total = await crud.count(
        db, id=query.id, name=query.name, biz=query.biz, service=query.service
    )
    if total <= 0:
        return page_vo
    page_vo.total = total
    offset = PageVO.offset(query.page, query.size)
    items = await crud.list_testcases(
        db,
        id=query.id,
        name=query.name,
        biz=query.biz,
        service=query.service,
        offset=offset,
        limit=query.size,
    )
    page_vo.list = [_to_vo(t) for t in items]
    return page_vo


# ----- Phase 3+ 提供的轻量级关联查询辅助 -----


async def get_associated_jmx(db: AsyncSession, testcase_id: int) -> Jmx | None:
    """供 csv/jar service 等模块判断用例是否已关联 JMX"""
    return await jmx_crud.get_by_test_case_id(db, testcase_id)


async def get_associated_csvs(db: AsyncSession, testcase_id: int) -> list[Csv]:
    return await csv_crud.get_by_test_case_id(db, testcase_id)


async def get_associated_jars(db: AsyncSession, testcase_id: int) -> list[Jar]:
    return await jar_crud.get_by_test_case_id(db, testcase_id)


# ---------------------------------------------------------------------------
# Phase 5：debug / run / stop / syncNode / getFull / getJMeterResult
# ---------------------------------------------------------------------------


async def _ensure_master_bin(db: AsyncSession, action: str) -> str:
    bin_home = await config_service.get_value(db, MASTER_JMETER_BIN_HOME_KEY)
    if not Path(bin_home).is_dir():
        raise MysteriousException(
            Codes.FAIL, message=f"{action}: mysterious-jmeter可执行目录不存在"
        )
    return bin_home


def _ts_now() -> str:
    return datetime.now(SHANGHAI).strftime("%Y-%m-%d-%H:%M:%S")


def _cleanup_old_run_jmx(jmx_dir: str, testcase_id: int, keep: int = 5) -> None:
    """清理旧的 run_*.jmx 临时文件，只保留最近 keep 个。"""
    pattern = re.compile(rf"^run_(\d{{4}}-\d{{2}}-\d{{2}}-\d{{2}}:\d{{2}}:\d{{2}})_{testcase_id}\.jmx$")
    run_files: list[tuple[str, str]] = []  # (filename, ts)
    try:
        for name in os.listdir(jmx_dir):
            m = pattern.match(name)
            if m:
                run_files.append((name, m.group(1)))
    except OSError:
        return
    # 按时间戳排序（文件名中时间戳字典序即时间序）
    run_files.sort(key=lambda x: x[1])
    # 删除超出的旧文件
    if len(run_files) > keep:
        for old_name, _ in run_files[:-keep]:
            old_path = os.path.join(jmx_dir, old_name)
            try:
                os.remove(old_path)
                log.info("清理旧 run jmx: %s", old_path)
            except OSError as e:
                log.warning("删除旧 run jmx 失败: %s, %s", old_path, e)


def _prepare_report_dirs(testcase_dir: str, ts: str) -> tuple[str, str, str]:
    report_dir = os.path.join(testcase_dir, "report", ts) + os.sep
    jtl_dir = report_dir + "jtl" + os.sep
    log_dir = report_dir + "log" + os.sep
    data_dir = report_dir + "data" + os.sep
    for d in (report_dir, jtl_dir, log_dir, data_dir):
        Path(d).mkdir(parents=True, exist_ok=True)
    return jtl_dir, log_dir, data_dir


async def debug_testcase(db: AsyncSession, id: int, user: UserContext) -> bool:
    bin_home = await _ensure_master_bin(db, "debug")
    testcase = await crud.get_by_id(db, id)
    if testcase is None:
        raise MysteriousException(Codes.TESTCASE_NOT_EXIST)
    jmx = await jmx_crud.get_by_test_case_id(db, id)
    if jmx is None:
        raise MysteriousException(Codes.JMX_NOT_EXIST)

    # 调试用 debug 副本（线程数已改 1）
    debug_jmx_path = jmx.jmx_dir + "debug_" + jmx.dst_name

    ts = _ts_now()
    jtl_dir, log_dir, _data_dir = _prepare_report_dirs(testcase.test_case_dir, ts)
    jtl_path = jtl_dir + testcase.name + ".xml"
    log_path = log_dir + f"jmeter_{ts}.log"

    cmd = [
        os.path.join(bin_home, "jmeter"),
        "-Jjmeter.save.saveservice.output_format=xml",
        "-Jjmeter.save.saveservice.response_data=true",
        "-n",
        "-t",
        debug_jmx_path,
        "-l",
        jtl_path,
        "-j",
        log_path,
    ]

    # 改用例状态 → RUN_ING
    testcase.status = TestCaseStatus.RUN_ING.value
    stamp_modify(testcase, user)
    await crud.update(db, testcase)

    report_id = await report_service.add_report(
        db,
        ReportParam(
            name=testcase.name,
            description=f"【{ts}】{testcase.description}",
            test_case_id=id,
            report_dir=jtl_dir,
            exec_type=ExecType.DEBUG.value,
            status=TestCaseStatus.RUN_ING.value,
            response_data="",
            jmeter_log_file_path=log_path,
        ),
        user,
    )

    log.info("[debug] cmd=%s", " ".join(cmd))
    await jmeter_runner.launch_jmeter(
        cmd,
        testcase_id=id,
        report_id=report_id,
        exec_type=ExecType.DEBUG.value,
        jtl_path=jtl_path,
        log_file_path=log_path,
    )
    return True


async def run_testcase(db: AsyncSession, id: int, param: RunParam, user: UserContext) -> bool:
    bin_home = await _ensure_master_bin(db, "run")
    testcase = await crud.get_by_id(db, id)
    if testcase is None:
        raise MysteriousException(Codes.TESTCASE_NOT_EXIST)
    jmx = await jmx_crud.get_by_test_case_id(db, id)
    if jmx is None:
        raise MysteriousException(Codes.JMX_NOT_EXIST)

    if testcase.status == TestCaseStatus.RUN_ING.value:
        raise MysteriousException(Codes.TESTCASE_IS_RUNNING)

    # 获取启用 slave，可选按区域过滤
    region = param.region.strip() if param.region else ""

    # 同用例+同区域互斥：该区域已在跑则拒绝
    if region:
        region_running = await report_crud.get_running_by_testcase_region(db, id, region)
        if region_running:
            raise MysteriousException(
                Codes.TESTCASE_IS_RUNNING,
                message=f"用例「{testcase.name}」在区域「{region}」已有正在执行的任务",
            )
    enable_slaves = await node_crud.list_enable_slaves(db, region=region or None)

    # 过滤掉离线的 slave
    healthy_slaves = [s for s in enable_slaves if s.health_status == 1]

    # 用户可选择使用多少台 slave
    total_available = len(healthy_slaves)
    if param.slave_count > total_available:
        raise MysteriousException(
            Codes.FAIL,
            message=f"压测机数量不足: 需要{param.slave_count}台, 可用{total_available}台",
        )
    if param.slave_count > 0 and param.slave_count <= total_available:
        healthy_slaves = healthy_slaves[:param.slave_count]
        region_info = f"区域={region}, " if region else ""
        log.info("%s用户指定使用 %d/%d 台 slave", region_info, param.slave_count, total_available)

    for s in healthy_slaves:
        try:
            ssh = SSHClient(s.host, s.port, s.username, s.password)
            await ssh.telnet(200)
        except MysteriousException as e:
            log.info("run 前置检查 slave %s 失败: %s", s.host, e)
            raise MysteriousException(Codes.NODE_CANNOT_CONNECT) from e

    # 压测参数优先使用传入值，否则回退到用例自身保存的值
    num_threads = param.num_threads if param.num_threads not in (None, "") else (testcase.num_threads or "10")
    ramp_time = param.ramp_time if param.ramp_time not in (None, "") else (testcase.ramp_time or "0")
    duration = param.duration if param.duration not in (None, "") else (testcase.duration or "60")

    # 分布式执行：总并发数按 slave 数均分，每台只执行自己的份额
    slave_count = len(healthy_slaves)
    if slave_count > 1:
        per_slave = math.ceil(int(num_threads) / slave_count)
        log.info(
            "分布式执行: 总并发=%s, slave数=%d, 每台=%d",
            num_threads, slave_count, per_slave,
        )
        num_threads = str(per_slave)

    # 根据用户传入的参数动态修改 JMX，生成临时执行文件
    src_jmx_path = jmx.jmx_dir + jmx.dst_name
    ts = _ts_now()
    run_jmx_name = f"run_{ts}_{id}.jmx"
    run_jmx_path = jmx.jmx_dir + run_jmx_name
    update_run_thread(
        src_jmx_path,
        run_jmx_path,
        num_threads,
        ramp_time,
        duration,
    )

    # 清理旧 run_*.jmx，只保留最近 5 个
    _cleanup_old_run_jmx(jmx.jmx_dir, id, keep=5)

    jtl_dir, log_dir, data_dir = _prepare_report_dirs(testcase.test_case_dir, ts)
    jtl_path = jtl_dir + testcase.name + ".jtl"
    log_path = log_dir + f"jmeter_{ts}.log"

    cmd = [
        os.path.join(bin_home, "jmeter"),
        "-n",
        "-t",
        run_jmx_path,
    ]
    if healthy_slaves:
        cmd += ["-R", ",".join(f"{s.host}:1099" for s in healthy_slaves)]
    cmd += [
        "-l",
        jtl_path,
        "-j",
        log_path,
        "-e",
        "-o",
        data_dir,
    ]

    testcase.status = TestCaseStatus.RUN_ING.value
    stamp_modify(testcase, user)
    await crud.update(db, testcase)

    report_id = await report_service.add_report(
        db,
        ReportParam(
            name=testcase.name,
            description=testcase.description,
            test_case_id=id,
            report_dir=data_dir,
            exec_type=ExecType.EXEC.value,
            status=TestCaseStatus.RUN_ING.value,
            response_data=Codes.STRESS_RESULT.message,
            jmeter_log_file_path=log_path,
            region=region,
        ),
        user,
    )

    log.info("[run] cmd=%s region=%s", " ".join(cmd), region or "全部")
    await jmeter_runner.launch_jmeter(
        cmd,
        testcase_id=id,
        report_id=report_id,
        exec_type=ExecType.EXEC.value,
        jtl_path=jtl_path,
        log_file_path=log_path,
    )
    return True


async def stop_testcase(db: AsyncSession, id: int, user: UserContext) -> bool:
    """停止该用例所有正在运行的执行（停所有区域）。"""
    testcase = await crud.get_by_id(db, id)
    if testcase is None or testcase.status != TestCaseStatus.RUN_ING.value:
        raise MysteriousException(Codes.TESTCASE_IS_NOT_RUNNING)

    stmt = select(ReportModel).where(
        ReportModel.test_case_id == id,
        ReportModel.status == TestCaseStatus.RUN_ING.value,
    )
    running_reports = list((await db.execute(stmt)).scalars().all())

    for rpt in running_reports:
        ok = await jmeter_runner.launch_stop(rpt.id)
        log.info("[stop] report_id=%s region=%s ok=%s", rpt.id, rpt.region, ok)
    return True


async def stop_execution(db: AsyncSession, report_id: int, user: UserContext) -> bool:
    """停止指定报告（执行）对应的 JMeter 进程"""
    rpt = await report_crud.get_by_id(db, report_id)
    if rpt is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)
    if rpt.status != TestCaseStatus.RUN_ING.value:
        raise MysteriousException(Codes.TESTCASE_IS_NOT_RUNNING)

    log.info("[stop execution] report_id=%s region=%s", report_id, rpt.region)
    await jmeter_runner.launch_stop(report_id)
    return True


async def get_full_vo(db: AsyncSession, id: int) -> TestCaseFullVO | None:
    testcase = await crud.get_by_id(db, id)
    if testcase is None:
        return None
    jmx = await jmx_crud.get_by_test_case_id(db, id)
    csvs = await csv_crud.get_by_test_case_id(db, id)
    jars = await jar_crud.get_by_test_case_id(db, id)
    return TestCaseFullVO(
        **TestCaseVO.model_validate(testcase).model_dump(by_alias=False),
        jmx_vo=JmxVO.model_validate(jmx) if jmx else None,
        csv_vo_list=[CsvVO.model_validate(o) for o in csvs],
        jar_vo_list=[JarVO.model_validate(o) for o in jars],
    )


async def sync_node(db: AsyncSession, node_id: int, user: UserContext) -> bool:
    """对齐 Java testcase/syncNode：把所有用例的 CSV/JAR scp 到新增 slave。"""
    node = await node_crud.get_by_id(db, node_id)
    if node is None:
        raise MysteriousException(Codes.NODE_NOT_EXIST)
    if node.status == NodeStatus.ENABLE.value:
        raise MysteriousException(Codes.NODE_IS_ENABLE)

    ssh = SSHClient(node.host, node.port, node.username, node.password)
    # Java 端 testCaseList = getTestCaseListByStatus(null)，即所有用例
    stmt = select(TestCase)
    all_cases = list((await db.execute(stmt)).scalars().all())
    for tc in all_cases:
        for csv_obj in await csv_crud.get_by_test_case_id(db, tc.id):
            await ssh.scp_file(csv_obj.csv_dir + csv_obj.src_name, csv_obj.csv_dir)
        for jar_obj in await jar_crud.get_by_test_case_id(db, tc.id):
            await ssh.scp_file(jar_obj.jar_dir + jar_obj.src_name, jar_obj.jar_dir)
    return True


async def get_jmeter_result(db: AsyncSession, id: int) -> list[JMeterResultVO]:
    """对齐 Java getJMeterResult：取最近 10 条该用例的报告，从第 0 条的 jmeter.log 解析 summary 行。"""
    reports = await report_service.get_debug_reports_by_test_case_id(db, id, None, 10)
    if not reports:
        return []
    log_path = reports[0].jmeter_log_file_path
    if not log_path or not Path(log_path).exists():
        return []
    results: list[JMeterResultVO] = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _JMETER_RESULT_RE.search(line)
                if m:
                    results.append(
                        JMeterResultVO(
                            current_time=m.group(1),
                            throughput=float(m.group(2)),
                            avg_response_time=float(m.group(3)),
                        )
                    )
    except OSError as e:
        log.warning("读取 jmeter.log 失败: %s", e)
        raise MysteriousException(Codes.FAIL, message="实时数据读取失败") from e
    return results
