"""Jmx 业务服务。Phase 3 做了上传路径；Phase 4 补齐在线编辑（addOnline/getOnline/updateOnline/forceDelete）。"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

import aiofiles
import aiofiles.os
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import stamp_create, stamp_modify
from app.core.codes import Codes
from app.core.context import UserContext
from app.core.enums import JMeterSample, JMeterScript, JMeterThreads
from app.core.exceptions import MysteriousException
from app.core.jmeter_xml import JMeterXMLBuilder, update_debug_thread
from app.core.response import PageVO
from app.crud import csv as csv_crud
from app.crud import jar as jar_crud
from app.crud import jmx as jmx_crud
from app.crud import testcase as testcase_crud
from app.crud import jmx_assertion as jmx_assertion_crud
from app.crud import jmx_concurrency_thread_group as jmx_concurrency_thread_group_crud
from app.crud import jmx_csv as jmx_csv_crud
from app.crud import jmx_http as jmx_http_crud
from app.crud import jmx_http_header as jmx_http_header_crud
from app.crud import jmx_http_param as jmx_http_param_crud
from app.crud import jmx_java as jmx_java_crud
from app.crud import jmx_stepping_thread_group as jmx_stepping_thread_group_crud
from app.crud import jmx_thread_group as jmx_thread_group_crud
from app.models.jmx import Jmx
from app.models.jmx_assertion import JmxAssertion
from app.models.jmx_concurrency_thread_group import JmxConcurrencyThreadGroup
from app.models.jmx_csv import JmxCsv
from app.models.jmx_http import JmxHttp
from app.models.jmx_http_header import JmxHttpHeader
from app.models.jmx_http_param import JmxHttpParam
from app.models.jmx_java import JmxJava
from app.models.jmx_stepping_thread_group import JmxSteppingThreadGroup
from app.models.jmx_thread_group import JmxThreadGroup
from app.schemas.jmx import JmxQuery, JmxVO
from app.schemas.jmx_assertion import AssertionVO
from app.schemas.jmx_csv import CsvDataVO, CsvFileVO
from app.schemas.jmx_http import HttpHeaderVO, HttpParamVO, HttpVO
from app.schemas.jmx_java import JavaParamVO, JavaVO
from app.schemas.jmx_thread import (
    ConcurrencyThreadGroupVO,
    SteppingThreadGroupVO,
    ThreadGroupVO,
)
from app.services import config as config_service

log = logging.getLogger(__name__)


def _check_jmx_name(name: str | None) -> None:
    if not name or " " in name or not name.endswith(".jmx"):
        raise MysteriousException(Codes.JMX_NAME_ERROR)


def _to_vo(obj: Jmx) -> JmxVO:
    return JmxVO.model_validate(obj)


async def upload_jmx(
    db: AsyncSession,
    testcase_id: int,
    jmx_file: UploadFile,
    user: UserContext,
) -> bool:
    """上传 JMX 脚本：落盘到 {testCaseDir}/jmx/，并复制一份 debug 副本压低线程数"""
    # 1. 用例必须存在
    testcase = await testcase_crud.get_by_id(db, testcase_id)
    if testcase is None:
        raise MysteriousException(Codes.TESTCASE_NOT_EXIST)

    # 2. 用例必须没关联 JMX（一对一）
    existing = await jmx_crud.get_by_test_case_id(db, testcase_id)
    if existing is not None:
        raise MysteriousException(Codes.TESTCASE_HAS_JMX)

    # 3. 文件名校验
    src_name = jmx_file.filename or ""
    _check_jmx_name(src_name)
    dst_name = src_name

    jmx_dir = os.path.join(testcase.test_case_dir, "jmx") + os.sep
    jmx_filepath = jmx_dir + dst_name
    debug_jmx_filepath = jmx_dir + "debug_" + dst_name

    # 4. 入库
    obj = Jmx(
        src_name=src_name,
        dst_name=dst_name,
        description=testcase.name,
        jmx_dir=jmx_dir,
        test_case_id=testcase_id,
        jmeter_script_type=JMeterScript.UPLOAD_JMX.value,
    )
    stamp_create(obj, user)
    await jmx_crud.add(db, obj)

    # 5. 落盘 + 复制 debug 副本
    await aiofiles.os.makedirs(jmx_dir, exist_ok=True)
    content = await jmx_file.read()
    async with aiofiles.open(jmx_filepath, "wb") as f:
        await f.write(content)
    async with aiofiles.open(debug_jmx_filepath, "wb") as f:
        await f.write(content)

    # 6. 改 debug 副本的线程数为 1
    update_debug_thread(debug_jmx_filepath)

    return True


async def delete_jmx(db: AsyncSession, id: int) -> bool:
    """删 JMX 前必须确认用例下没有关联的 JAR/CSV"""
    obj = await jmx_crud.get_by_id(db, id)
    if obj is None:
        raise MysteriousException(Codes.FILE_NOT_EXIST)

    jars = await jar_crud.get_by_test_case_id(db, obj.test_case_id)
    if jars:
        raise MysteriousException(Codes.JMX_HAS_JAR)
    csvs = await csv_crud.get_by_test_case_id(db, obj.test_case_id)
    if csvs:
        raise MysteriousException(Codes.JMX_HAS_CSV)

    # ONLINE_JMX 模式级联删除 9 张子表
    if obj.jmeter_script_type == JMeterScript.ONLINE_JMX.value:
        await jmx_thread_group_crud.delete_by_jmx_id(db, id)
        await jmx_stepping_thread_group_crud.delete_by_jmx_id(db, id)
        await jmx_concurrency_thread_group_crud.delete_by_jmx_id(db, id)
        await jmx_http_header_crud.delete_by_jmx_id(db, id)
        await jmx_http_param_crud.delete_by_jmx_id(db, id)
        await jmx_http_crud.delete_by_jmx_id(db, id)
        await jmx_java_crud.delete_by_jmx_id(db, id)
        await jmx_assertion_crud.delete_by_jmx_id(db, id)
        await jmx_csv_crud.delete_by_jmx_id(db, id)

    log.info("删除 JMX id=%s, jmx_dir=%s", id, obj.jmx_dir)
    await jmx_crud.delete(db, id)

    # 删整个 jmx 子目录（Java 端 fileUtils.rmFile(jmxDir)，实际 forceDelete 既能删文件也能删目录）
    if obj.jmx_dir and os.path.isdir(obj.jmx_dir):
        shutil.rmtree(obj.jmx_dir, ignore_errors=True)
    return True


async def get_jmx_list(db: AsyncSession, query: JmxQuery) -> PageVO[JmxVO]:
    page_vo: PageVO[JmxVO] = PageVO(page=query.page, size=query.size, total=0, list=[])
    total = await jmx_crud.count(db, src_name=query.src_name, test_case_id=query.test_case_id)
    if total <= 0:
        return page_vo
    page_vo.total = total
    offset = PageVO.offset(query.page, query.size)
    items = await jmx_crud.list_jmxs(
        db, src_name=query.src_name, test_case_id=query.test_case_id, offset=offset, limit=query.size
    )
    page_vo.list = [_to_vo(o) for o in items]
    return page_vo


async def get_jmx_vo(db: AsyncSession, id: int) -> JmxVO:
    """给 view/download 端点用：取不到抛 FILE_NOT_EXIST"""
    obj = await jmx_crud.get_by_id(db, id)
    if obj is None:
        raise MysteriousException(Codes.FILE_NOT_EXIST)
    return _to_vo(obj)


async def get_by_test_case_id(db: AsyncSession, test_case_id: int) -> JmxVO | None:
    obj = await jmx_crud.get_by_test_case_id(db, test_case_id)
    return _to_vo(obj) if obj else None


# ---------------------------------------------------------------------------
# Phase 4 — 在线编辑（addOnline / getOnline / updateOnline / forceDelete）
# ---------------------------------------------------------------------------


async def _get_base_jmx_dir(db: AsyncSession) -> str:
    """取 base JMX 模板目录，配置不存在时 fallback 到 api/jmx_base/"""
    try:
        configured = await config_service.get_value(db, "MASTER_BASE_JMX_FILES_PATH")
        if configured and Path(configured).is_dir():
            return configured
    except MysteriousException:
        pass
    return str(Path(__file__).resolve().parent.parent.parent / "jmx_base")


async def _get_base_jmx_path(db: AsyncSession, threads_type: int, sample_type: int) -> str:
    """拼 base 模板完整路径：{dir}/{thread}_{sample}.jmx"""
    base_dir = await _get_base_jmx_dir(db)
    thread_map = {
        JMeterThreads.THREAD_GROUP.value: "thread_group",
        JMeterThreads.STEPPING_THREAD_GROUP.value: "stepping_thread_group",
        JMeterThreads.CONCURRENCY_THREAD_GROUP.value: "concurrency_thread_group",
    }
    sample_map = {
        JMeterSample.HTTP_REQUEST.value: "http",
        JMeterSample.JAVA_REQUEST.value: "java",
    }
    return os.path.join(
        base_dir,
        f"{thread_map.get(threads_type, 'thread_group')}_{sample_map.get(sample_type, 'http')}.jmx",
    )


async def _cascade_delete_subtables(db: AsyncSession, jmx_id: int) -> None:
    """级联删除 9 张子表（forceDelete / update 清理用）"""
    await jmx_thread_group_crud.delete_by_jmx_id(db, jmx_id)
    await jmx_stepping_thread_group_crud.delete_by_jmx_id(db, jmx_id)
    await jmx_concurrency_thread_group_crud.delete_by_jmx_id(db, jmx_id)
    await jmx_http_header_crud.delete_by_jmx_id(db, jmx_id)
    await jmx_http_param_crud.delete_by_jmx_id(db, jmx_id)
    await jmx_http_crud.delete_by_jmx_id(db, jmx_id)
    await jmx_java_crud.delete_by_jmx_id(db, jmx_id)
    await jmx_assertion_crud.delete_by_jmx_id(db, jmx_id)
    await jmx_csv_crud.delete_by_jmx_id(db, jmx_id)


async def add_online_jmx(db: AsyncSession, jmx_vo: JmxVO, user: UserContext) -> bool:
    """在线新增 JMX：校验 → 落库 → XML 生成 → 落盘 → debug 副本"""
    # 1. 名称校验
    _check_jmx_name(jmx_vo.src_name)

    # 2. 用例必须存在
    testcase = await testcase_crud.get_by_id(db, jmx_vo.test_case_id)
    if testcase is None:
        raise MysteriousException(Codes.TESTCASE_NOT_EXIST)

    # 3. 用例必须没关联 JMX
    existing = await jmx_crud.get_by_test_case_id(db, jmx_vo.test_case_id)
    if existing is not None:
        raise MysteriousException(Codes.TESTCASE_HAS_JMX)

    # 4. HTTP body 和 param 不能同时非空
    if (
        jmx_vo.jmeter_sample_type == JMeterSample.HTTP_REQUEST.value
        and jmx_vo.http_vo
    ):
        has_body = bool(jmx_vo.http_vo.body and jmx_vo.http_vo.body.strip())
        has_param = bool(
            jmx_vo.http_vo.http_param_vo_list
            and any(p.param_key for p in jmx_vo.http_vo.http_param_vo_list)
        )
        if has_body and has_param:
            raise MysteriousException(Codes.PARAM_WRONG)

    # 5. CSV 文件必须 .csv 后缀
    if jmx_vo.csv_data_vo and jmx_vo.csv_data_vo.csv_file_vo_list:
        for csv_file in jmx_vo.csv_data_vo.csv_file_vo_list:
            if csv_file.filename and not csv_file.filename.endswith(".csv"):
                raise MysteriousException(Codes.CSV_NAME_ERROR)

    # 6. 必填字段校验
    if jmx_vo.jmeter_threads_type is None or jmx_vo.jmeter_sample_type is None:
        raise MysteriousException(Codes.PARAM_MISSING)

    # 7. 拼 base 模板路径
    base_jmx_path = await _get_base_jmx_path(
        db, jmx_vo.jmeter_threads_type, jmx_vo.jmeter_sample_type
    )

    # 8. 入库 JmxDO
    jmx_dir = os.path.join(testcase.test_case_dir, "jmx") + os.sep
    dst_name = jmx_vo.src_name

    obj = Jmx(
        src_name=jmx_vo.src_name,
        dst_name=dst_name,
        description=testcase.name,
        jmx_dir=jmx_dir,
        test_case_id=jmx_vo.test_case_id,
        jmeter_script_type=JMeterScript.ONLINE_JMX.value,
        jmeter_threads_type=jmx_vo.jmeter_threads_type,
        jmeter_sample_type=jmx_vo.jmeter_sample_type,
    )
    stamp_create(obj, user)
    await jmx_crud.add(db, obj)

    jmx_filepath = jmx_dir + dst_name
    debug_jmx_filepath = jmx_dir + "debug_" + dst_name

    # 9. jmxDir 已存在 → rmtree + mkdir
    if os.path.isdir(jmx_dir):
        shutil.rmtree(jmx_dir, ignore_errors=True)
    await aiofiles.os.makedirs(jmx_dir, exist_ok=True)

    # 10. builder
    builder = JMeterXMLBuilder()
    builder.init(base_jmx_path)

    # 11. 线程组
    if (
        jmx_vo.jmeter_threads_type == JMeterThreads.THREAD_GROUP.value
        and jmx_vo.thread_group_vo
    ):
        tg_vo = jmx_vo.thread_group_vo
        tg_obj = JmxThreadGroup(
            test_case_id=jmx_vo.test_case_id,
            jmx_id=obj.id,
            num_threads=tg_vo.num_threads or "",
            ramp_time=tg_vo.ramp_time or "",
            loops=tg_vo.loops or "",
            same_user_on_next_iteration=tg_vo.same_user_on_next_iteration or 0,
            delayed_start=tg_vo.delayed_start or 0,
            scheduler=tg_vo.scheduler or 0,
            duration=tg_vo.duration or "",
            delay=tg_vo.delay or "",
        )
        stamp_create(tg_obj, user)
        await jmx_thread_group_crud.add(db, tg_obj)
        builder.update_thread_group(tg_vo)
    elif (
        jmx_vo.jmeter_threads_type == JMeterThreads.STEPPING_THREAD_GROUP.value
        and jmx_vo.stepping_thread_group_vo
    ):
        tg_vo = jmx_vo.stepping_thread_group_vo
        tg_obj = JmxSteppingThreadGroup(
            test_case_id=jmx_vo.test_case_id,
            jmx_id=obj.id,
            num_threads=tg_vo.num_threads or "",
            first_wait_for_seconds=tg_vo.first_wait_for_seconds or "",
            then_start_threads=tg_vo.then_start_threads or "",
            next_add_threads=tg_vo.next_add_threads or "",
            next_add_threads_every_seconds=tg_vo.next_add_threads_every_seconds or "",
            using_ramp_up_seconds=tg_vo.using_ramp_up_seconds or "",
            then_hold_load_for_seconds=tg_vo.then_hold_load_for_seconds or "",
            finally_stop_threads=tg_vo.finally_stop_threads or "",
            finally_stop_threads_every_seconds=tg_vo.finally_stop_threads_every_seconds or "",
        )
        stamp_create(tg_obj, user)
        await jmx_stepping_thread_group_crud.add(db, tg_obj)
        builder.update_stepping_thread_group(tg_vo)
    elif (
        jmx_vo.jmeter_threads_type == JMeterThreads.CONCURRENCY_THREAD_GROUP.value
        and jmx_vo.concurrency_thread_group_vo
    ):
        tg_vo = jmx_vo.concurrency_thread_group_vo
        tg_obj = JmxConcurrencyThreadGroup(
            test_case_id=jmx_vo.test_case_id,
            jmx_id=obj.id,
            target_concurrency=tg_vo.target_concurrency or "",
            ramp_up_time=tg_vo.ramp_up_time or "",
            ramp_up_steps_count=tg_vo.ramp_up_steps_count or "",
            hold_target_rate_time=tg_vo.hold_target_rate_time or "",
        )
        stamp_create(tg_obj, user)
        await jmx_concurrency_thread_group_crud.add(db, tg_obj)
        builder.update_concurrency_thread_group(tg_vo)

    # 12. HTTP / Java
    if (
        jmx_vo.jmeter_sample_type == JMeterSample.HTTP_REQUEST.value
        and jmx_vo.http_vo
    ):
        http_vo = jmx_vo.http_vo
        http_obj = JmxHttp(
            test_case_id=jmx_vo.test_case_id,
            jmx_id=obj.id,
            method=http_vo.method or "",
            protocol=http_vo.protocol or "",
            domain=http_vo.domain or "",
            port=http_vo.port or "",
            path=http_vo.path or "",
            content_encoding=http_vo.content_encoding or "",
            body=http_vo.body or "",
        )
        stamp_create(http_obj, user)
        http_obj = await jmx_http_crud.add(db, http_obj)
        builder.update_http_sample(http_vo)

        # headers
        if http_vo.http_header_vo_list:
            header_objs = []
            for h in http_vo.http_header_vo_list:
                if h.header_key:
                    ho = JmxHttpHeader(
                        test_case_id=jmx_vo.test_case_id,
                        jmx_id=obj.id,
                        http_id=http_obj.id,
                        header_key=h.header_key or "",
                        header_value=h.header_value or "",
                    )
                    stamp_create(ho, user)
                    header_objs.append(ho)
                    builder.add_http_header(h.header_key, h.header_value or "")
            if header_objs:
                await jmx_http_header_crud.batch_add(db, header_objs)

        # params
        if http_vo.http_param_vo_list:
            param_objs = []
            for p in http_vo.http_param_vo_list:
                if p.param_key:
                    po = JmxHttpParam(
                        test_case_id=jmx_vo.test_case_id,
                        jmx_id=obj.id,
                        http_id=http_obj.id,
                        param_key=p.param_key or "",
                        param_value=p.param_value or "",
                    )
                    stamp_create(po, user)
                    param_objs.append(po)
                    builder.add_http_param(p.param_key, p.param_value or "")
            if param_objs:
                await jmx_http_param_crud.batch_add(db, param_objs)

        # body
        if http_vo.body and http_vo.body.strip():
            builder.add_http_body(http_vo.body)

    elif (
        jmx_vo.jmeter_sample_type == JMeterSample.JAVA_REQUEST.value
        and jmx_vo.java_vo
    ):
        java_vo = jmx_vo.java_vo
        if java_vo.java_param_vo_list:
            java_objs = []
            for p in java_vo.java_param_vo_list:
                jo = JmxJava(
                    test_case_id=jmx_vo.test_case_id,
                    jmx_id=obj.id,
                    java_request_class_path=java_vo.java_request_class_path or "",
                    param_key=p.param_key or "",
                    param_value=p.param_value or "",
                )
                stamp_create(jo, user)
                java_objs.append(jo)
            if java_objs:
                await jmx_java_crud.batch_add(db, java_objs)

        builder.update_java_request(java_vo.java_request_class_path or "")
        if java_vo.java_param_vo_list:
            for p in java_vo.java_param_vo_list:
                if p.param_key:
                    builder.add_java_param(p.param_key, p.param_value or "")

    # 13. 断言
    if jmx_vo.assertion_vo:
        avo = jmx_vo.assertion_vo
        assertion_obj = JmxAssertion(
            test_case_id=jmx_vo.test_case_id,
            jmx_id=obj.id,
            response_code=avo.response_code or "",
            response_message=avo.response_message or "",
            json_path=avo.json_path or "",
            expected_value=avo.expected_value or "",
        )
        stamp_create(assertion_obj, user)
        await jmx_assertion_crud.add(db, assertion_obj)
        builder.add_assertion(
            jmx_vo.jmeter_sample_type,
            avo.response_code,
            avo.response_message,
            avo.json_path,
            avo.expected_value,
        )

    # 14. CSV
    if jmx_vo.csv_data_vo:
        csv_vo = jmx_vo.csv_data_vo
        if csv_vo.csv_file_vo_list:
            for cf in csv_vo.csv_file_vo_list:
                co = JmxCsv(
                    test_case_id=jmx_vo.test_case_id,
                    jmx_id=obj.id,
                    filename=cf.filename or "",
                    variable_names=cf.variable_names or "",
                    delimiter=csv_vo.delimiter or ",",
                    file_encoding=csv_vo.file_encoding or "UTF-8",
                    ignore_first_line=csv_vo.ignore_first_line or 1,
                    allow_quoted_data=csv_vo.allow_quoted_data or 0,
                    recycle_on_eof=csv_vo.recycle_on_eof or 1,
                    stop_thread_on_eof=csv_vo.stop_thread_on_eof or 0,
                    sharing_mode=csv_vo.sharing_mode or "Current thread group",
                )
                stamp_create(co, user)
                await jmx_csv_crud.add(db, co)
        builder.add_csv(csv_vo, jmx_vo.jmeter_sample_type)

    # 15. 写盘
    builder.write_jmx_file(jmx_filepath)

    # 16. debug 副本
    shutil.copy(jmx_filepath, debug_jmx_filepath)
    update_debug_thread(debug_jmx_filepath)

    return True


async def get_online_jmx(db: AsyncSession, id: int) -> JmxVO:
    """取在线编辑 JMX 的完整嵌套 VO"""
    obj = await jmx_crud.get_by_id(db, id)
    if obj is None:
        raise MysteriousException(Codes.JMX_NOT_EXIST)
    if obj.jmeter_script_type != JMeterScript.ONLINE_JMX.value:
        raise MysteriousException(Codes.JMX_ERROR)

    vo = _to_vo(obj)

    # 线程组（3 选 1）
    if obj.jmeter_threads_type == JMeterThreads.THREAD_GROUP.value:
        tg = await jmx_thread_group_crud.get_by_jmx_id(db, id)
        if tg:
            vo.thread_group_vo = ThreadGroupVO.model_validate(tg)
    elif obj.jmeter_threads_type == JMeterThreads.STEPPING_THREAD_GROUP.value:
        tg = await jmx_stepping_thread_group_crud.get_by_jmx_id(db, id)
        if tg:
            vo.stepping_thread_group_vo = SteppingThreadGroupVO.model_validate(tg)
    elif obj.jmeter_threads_type == JMeterThreads.CONCURRENCY_THREAD_GROUP.value:
        tg = await jmx_concurrency_thread_group_crud.get_by_jmx_id(db, id)
        if tg:
            vo.concurrency_thread_group_vo = ConcurrencyThreadGroupVO.model_validate(tg)

    # Sample
    if obj.jmeter_sample_type == JMeterSample.HTTP_REQUEST.value:
        http = await jmx_http_crud.get_by_jmx_id(db, id)
        if http:
            vo.http_vo = HttpVO.model_validate(http)
            headers = await jmx_http_header_crud.get_by_jmx_id(db, id)
            vo.http_vo.http_header_vo_list = [
                HttpHeaderVO.model_validate(h) for h in headers
            ]
            params = await jmx_http_param_crud.get_by_jmx_id(db, id)
            vo.http_vo.http_param_vo_list = [
                HttpParamVO.model_validate(p) for p in params
            ]
    elif obj.jmeter_sample_type == JMeterSample.JAVA_REQUEST.value:
        java_rows = await jmx_java_crud.get_by_jmx_id(db, id)
        if java_rows:
            first = java_rows[0]
            vo.java_vo = JavaVO(
                test_case_id=first.test_case_id,
                jmx_id=first.jmx_id,
                java_request_class_path=first.java_request_class_path,
                java_param_vo_list=[JavaParamVO.model_validate(r) for r in java_rows],
            )

    # 断言
    assertion = await jmx_assertion_crud.get_by_jmx_id(db, id)
    if assertion:
        vo.assertion_vo = AssertionVO.model_validate(assertion)

    # CSV
    csv_rows = await jmx_csv_crud.get_by_jmx_id(db, id)
    if csv_rows:
        vo.csv_data_vo = CsvDataVO(
            test_case_id=obj.test_case_id,
            jmx_id=id,
            file_encoding=csv_rows[0].file_encoding,
            delimiter=csv_rows[0].delimiter,
            ignore_first_line=csv_rows[0].ignore_first_line,
            allow_quoted_data=csv_rows[0].allow_quoted_data,
            recycle_on_eof=csv_rows[0].recycle_on_eof,
            stop_thread_on_eof=csv_rows[0].stop_thread_on_eof,
            sharing_mode=csv_rows[0].sharing_mode,
            csv_file_vo_list=[
                CsvFileVO(filename=r.filename, variable_names=r.variable_names)
                for r in csv_rows
            ],
        )

    return vo


async def update_online_jmx(
    db: AsyncSession, id: int, jmx_vo: JmxVO, user: UserContext
) -> bool:
    """更新在线 JMX：校验 → 清理旧子表 → 重新入库 → 重写 JMX"""
    obj = await jmx_crud.get_by_id(db, id)
    if obj is None:
        raise MysteriousException(Codes.JMX_NOT_EXIST)
    if obj.jmeter_script_type != JMeterScript.ONLINE_JMX.value:
        raise MysteriousException(Codes.JMX_ERROR)

    # 1. 校验用例没 JAR/CSV
    jars = await jar_crud.get_by_test_case_id(db, obj.test_case_id)
    if jars:
        raise MysteriousException(Codes.JMX_HAS_JAR)
    csvs = await csv_crud.get_by_test_case_id(db, obj.test_case_id)
    if csvs:
        raise MysteriousException(Codes.JMX_HAS_CSV)

    # 2. Sample 类型不能改
    if jmx_vo.jmeter_sample_type != obj.jmeter_sample_type:
        raise MysteriousException(Codes.PARAM_WRONG)

    # 3. 名称校验
    _check_jmx_name(jmx_vo.src_name)

    # 4. HTTP body 和 param 互斥
    if (
        jmx_vo.jmeter_sample_type == JMeterSample.HTTP_REQUEST.value
        and jmx_vo.http_vo
    ):
        has_body = bool(jmx_vo.http_vo.body and jmx_vo.http_vo.body.strip())
        has_param = bool(
            jmx_vo.http_vo.http_param_vo_list
            and any(p.param_key for p in jmx_vo.http_vo.http_param_vo_list)
        )
        if has_body and has_param:
            raise MysteriousException(Codes.PARAM_WRONG)

    # 5. CSV 后缀校验
    if jmx_vo.csv_data_vo and jmx_vo.csv_data_vo.csv_file_vo_list:
        for csv_file in jmx_vo.csv_data_vo.csv_file_vo_list:
            if csv_file.filename and not csv_file.filename.endswith(".csv"):
                raise MysteriousException(Codes.CSV_NAME_ERROR)

    # 6. 线程组类型切换：删旧
    if jmx_vo.jmeter_threads_type != obj.jmeter_threads_type:
        if obj.jmeter_threads_type == JMeterThreads.THREAD_GROUP.value:
            await jmx_thread_group_crud.delete_by_jmx_id(db, id)
        elif obj.jmeter_threads_type == JMeterThreads.STEPPING_THREAD_GROUP.value:
            await jmx_stepping_thread_group_crud.delete_by_jmx_id(db, id)
        elif obj.jmeter_threads_type == JMeterThreads.CONCURRENCY_THREAD_GROUP.value:
            await jmx_concurrency_thread_group_crud.delete_by_jmx_id(db, id)

    # 7. 其他子表全删（header/param/java/assertion/csv 不做 diff，全删重建）
    await jmx_http_header_crud.delete_by_jmx_id(db, id)
    await jmx_http_param_crud.delete_by_jmx_id(db, id)
    await jmx_http_crud.delete_by_jmx_id(db, id)
    await jmx_java_crud.delete_by_jmx_id(db, id)
    await jmx_assertion_crud.delete_by_jmx_id(db, id)
    await jmx_csv_crud.delete_by_jmx_id(db, id)

    # 8. 更新 JmxDO 主表
    obj.src_name = jmx_vo.src_name
    obj.dst_name = jmx_vo.src_name
    obj.jmeter_threads_type = jmx_vo.jmeter_threads_type
    obj.jmeter_sample_type = jmx_vo.jmeter_sample_type
    stamp_modify(obj, user)
    await jmx_crud.update(db, obj)

    # 9. 重新生成 JMX
    jmx_dir = obj.jmx_dir
    jmx_filepath = jmx_dir + obj.dst_name
    debug_jmx_filepath = jmx_dir + "debug_" + obj.dst_name
    base_jmx_path = await _get_base_jmx_path(
        db, jmx_vo.jmeter_threads_type, jmx_vo.jmeter_sample_type
    )

    builder = JMeterXMLBuilder()
    builder.init(base_jmx_path)

    # 线程组
    if (
        jmx_vo.jmeter_threads_type == JMeterThreads.THREAD_GROUP.value
        and jmx_vo.thread_group_vo
    ):
        tg_vo = jmx_vo.thread_group_vo
        tg_obj = JmxThreadGroup(
            test_case_id=obj.test_case_id,
            jmx_id=id,
            num_threads=tg_vo.num_threads or "",
            ramp_time=tg_vo.ramp_time or "",
            loops=tg_vo.loops or "",
            same_user_on_next_iteration=tg_vo.same_user_on_next_iteration or 0,
            delayed_start=tg_vo.delayed_start or 0,
            scheduler=tg_vo.scheduler or 0,
            duration=tg_vo.duration or "",
            delay=tg_vo.delay or "",
        )
        stamp_create(tg_obj, user)
        await jmx_thread_group_crud.add(db, tg_obj)
        builder.update_thread_group(tg_vo)
    elif (
        jmx_vo.jmeter_threads_type == JMeterThreads.STEPPING_THREAD_GROUP.value
        and jmx_vo.stepping_thread_group_vo
    ):
        tg_vo = jmx_vo.stepping_thread_group_vo
        tg_obj = JmxSteppingThreadGroup(
            test_case_id=obj.test_case_id,
            jmx_id=id,
            num_threads=tg_vo.num_threads or "",
            first_wait_for_seconds=tg_vo.first_wait_for_seconds or "",
            then_start_threads=tg_vo.then_start_threads or "",
            next_add_threads=tg_vo.next_add_threads or "",
            next_add_threads_every_seconds=tg_vo.next_add_threads_every_seconds or "",
            using_ramp_up_seconds=tg_vo.using_ramp_up_seconds or "",
            then_hold_load_for_seconds=tg_vo.then_hold_load_for_seconds or "",
            finally_stop_threads=tg_vo.finally_stop_threads or "",
            finally_stop_threads_every_seconds=tg_vo.finally_stop_threads_every_seconds or "",
        )
        stamp_create(tg_obj, user)
        await jmx_stepping_thread_group_crud.add(db, tg_obj)
        builder.update_stepping_thread_group(tg_vo)
    elif (
        jmx_vo.jmeter_threads_type == JMeterThreads.CONCURRENCY_THREAD_GROUP.value
        and jmx_vo.concurrency_thread_group_vo
    ):
        tg_vo = jmx_vo.concurrency_thread_group_vo
        tg_obj = JmxConcurrencyThreadGroup(
            test_case_id=obj.test_case_id,
            jmx_id=id,
            target_concurrency=tg_vo.target_concurrency or "",
            ramp_up_time=tg_vo.ramp_up_time or "",
            ramp_up_steps_count=tg_vo.ramp_up_steps_count or "",
            hold_target_rate_time=tg_vo.hold_target_rate_time or "",
        )
        stamp_create(tg_obj, user)
        await jmx_concurrency_thread_group_crud.add(db, tg_obj)
        builder.update_concurrency_thread_group(tg_vo)

    # HTTP / Java
    if (
        jmx_vo.jmeter_sample_type == JMeterSample.HTTP_REQUEST.value
        and jmx_vo.http_vo
    ):
        http_vo = jmx_vo.http_vo
        http_obj = JmxHttp(
            test_case_id=obj.test_case_id,
            jmx_id=id,
            method=http_vo.method or "",
            protocol=http_vo.protocol or "",
            domain=http_vo.domain or "",
            port=http_vo.port or "",
            path=http_vo.path or "",
            content_encoding=http_vo.content_encoding or "",
            body=http_vo.body or "",
        )
        stamp_create(http_obj, user)
        http_obj = await jmx_http_crud.add(db, http_obj)
        builder.update_http_sample(http_vo)

        if http_vo.http_header_vo_list:
            header_objs = []
            for h in http_vo.http_header_vo_list:
                if h.header_key:
                    ho = JmxHttpHeader(
                        test_case_id=obj.test_case_id,
                        jmx_id=id,
                        http_id=http_obj.id,
                        header_key=h.header_key or "",
                        header_value=h.header_value or "",
                    )
                    stamp_create(ho, user)
                    header_objs.append(ho)
                    builder.add_http_header(h.header_key, h.header_value or "")
            if header_objs:
                await jmx_http_header_crud.batch_add(db, header_objs)

        if http_vo.http_param_vo_list:
            param_objs = []
            for p in http_vo.http_param_vo_list:
                if p.param_key:
                    po = JmxHttpParam(
                        test_case_id=obj.test_case_id,
                        jmx_id=id,
                        http_id=http_obj.id,
                        param_key=p.param_key or "",
                        param_value=p.param_value or "",
                    )
                    stamp_create(po, user)
                    param_objs.append(po)
                    builder.add_http_param(p.param_key, p.param_value or "")
            if param_objs:
                await jmx_http_param_crud.batch_add(db, param_objs)

        if http_vo.body and http_vo.body.strip():
            builder.add_http_body(http_vo.body)

    elif (
        jmx_vo.jmeter_sample_type == JMeterSample.JAVA_REQUEST.value
        and jmx_vo.java_vo
    ):
        java_vo = jmx_vo.java_vo
        if java_vo.java_param_vo_list:
            java_objs = []
            for p in java_vo.java_param_vo_list:
                jo = JmxJava(
                    test_case_id=obj.test_case_id,
                    jmx_id=id,
                    java_request_class_path=java_vo.java_request_class_path or "",
                    param_key=p.param_key or "",
                    param_value=p.param_value or "",
                )
                stamp_create(jo, user)
                java_objs.append(jo)
            if java_objs:
                await jmx_java_crud.batch_add(db, java_objs)

        builder.update_java_request(java_vo.java_request_class_path or "")
        if java_vo.java_param_vo_list:
            for p in java_vo.java_param_vo_list:
                if p.param_key:
                    builder.add_java_param(p.param_key, p.param_value or "")

    # 断言
    if jmx_vo.assertion_vo:
        avo = jmx_vo.assertion_vo
        assertion_obj = JmxAssertion(
            test_case_id=obj.test_case_id,
            jmx_id=id,
            response_code=avo.response_code or "",
            response_message=avo.response_message or "",
            json_path=avo.json_path or "",
            expected_value=avo.expected_value or "",
        )
        stamp_create(assertion_obj, user)
        await jmx_assertion_crud.add(db, assertion_obj)
        builder.add_assertion(
            jmx_vo.jmeter_sample_type,
            avo.response_code,
            avo.response_message,
            avo.json_path,
            avo.expected_value,
        )

    # CSV
    if jmx_vo.csv_data_vo:
        csv_vo = jmx_vo.csv_data_vo
        if csv_vo.csv_file_vo_list:
            for cf in csv_vo.csv_file_vo_list:
                co = JmxCsv(
                    test_case_id=obj.test_case_id,
                    jmx_id=id,
                    filename=cf.filename or "",
                    variable_names=cf.variable_names or "",
                    delimiter=csv_vo.delimiter or ",",
                    file_encoding=csv_vo.file_encoding or "UTF-8",
                    ignore_first_line=csv_vo.ignore_first_line or 1,
                    allow_quoted_data=csv_vo.allow_quoted_data or 0,
                    recycle_on_eof=csv_vo.recycle_on_eof or 1,
                    stop_thread_on_eof=csv_vo.stop_thread_on_eof or 0,
                    sharing_mode=csv_vo.sharing_mode or "Current thread group",
                )
                stamp_create(co, user)
                await jmx_csv_crud.add(db, co)
        builder.add_csv(csv_vo, jmx_vo.jmeter_sample_type)

    # 写盘
    builder.write_jmx_file(jmx_filepath)

    # debug 副本
    shutil.copy(jmx_filepath, debug_jmx_filepath)
    update_debug_thread(debug_jmx_filepath)

    return True


async def force_delete_jmx(db: AsyncSession, id: int) -> bool:
    """强制删除 JMX（跳过 JAR/CSV 关联检查），级联删 9 张子表"""
    obj = await jmx_crud.get_by_id(db, id)
    if obj is None:
        raise MysteriousException(Codes.FILE_NOT_EXIST)

    await _cascade_delete_subtables(db, id)
    await jmx_crud.delete(db, id)

    if obj.jmx_dir and os.path.isdir(obj.jmx_dir):
        shutil.rmtree(obj.jmx_dir, ignore_errors=True)
    return True
