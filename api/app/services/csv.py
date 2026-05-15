"""Csv 业务服务。"""

from __future__ import annotations

import logging
import os

import aiofiles
import aiofiles.os
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import jmeter_xml
from app.core.audit import stamp_create
from app.core.codes import Codes
from app.core.context import UserContext
from app.core.enums import JMeterScript
from app.core.exceptions import MysteriousException
from app.core.response import PageVO
from app.crud import csv as csv_crud
from app.crud import jmx as jmx_crud
from app.crud import testcase as testcase_crud
from app.models.csv import Csv
from app.schemas.csv import CsvQuery, CsvVO
from app.services import node as node_service

log = logging.getLogger(__name__)


def _check_csv_name(name: str | None) -> None:
    if not name or " " in name or not (name.endswith(".csv") or name.endswith(".dat")):
        raise MysteriousException(Codes.CSV_NAME_ERROR)


def _to_vo(obj: Csv) -> CsvVO:
    return CsvVO.model_validate(obj)


async def upload_csv(
    db: AsyncSession,
    testcase_id: int,
    csv_file: UploadFile,
    user: UserContext,
) -> bool:
    # 1. 用例必须存在
    testcase = await testcase_crud.get_by_id(db, testcase_id)
    if testcase is None:
        raise MysteriousException(Codes.TESTCASE_NOT_EXIST)

    # 2. 必须先有 JMX
    jmx = await jmx_crud.get_by_test_case_id(db, testcase_id)
    if jmx is None:
        raise MysteriousException(Codes.JMX_NOT_EXIST)

    # 3. 文件名校验
    src_name = csv_file.filename or ""
    _check_csv_name(src_name)
    dst_name = src_name

    csv_dir = os.path.join(testcase.test_case_dir, "csv") + os.sep
    csv_filepath = csv_dir + dst_name

    # 4. JMX 必须有同名的 <CSVDataSet>（约定）
    jmx_filepath = jmx.jmx_dir + jmx.dst_name
    debug_jmx_filepath = jmx.jmx_dir + "debug_" + jmx.dst_name

    if jmx.jmeter_script_type == JMeterScript.UPLOAD_JMX.value:
        if not jmeter_xml.exist_csv_filename(jmx_filepath, src_name):
            log.warning("JMX %s 里没有 testname=%s 的 CSVDataSet 节点", jmx_filepath, src_name)
            raise MysteriousException(Codes.CSV_NAME_ERROR)

    # 5. 已存在判存
    existing = await csv_crud.get_exist_list(db, testcase_id, src_name, csv_dir)
    if existing:
        raise MysteriousException(Codes.CSV_IS_EXIST)

    # 6. 入库
    obj = Csv(
        src_name=src_name,
        dst_name=dst_name,
        description=testcase.name,
        csv_dir=csv_dir,
        test_case_id=testcase_id,
    )
    stamp_create(obj, user)
    await csv_crud.add(db, obj)

    # 7. 落盘
    await aiofiles.os.makedirs(csv_dir, exist_ok=True)
    content = await csv_file.read()
    async with aiofiles.open(csv_filepath, "wb") as f:
        await f.write(content)

    # 7.5. 同步到所有 enabled slave（对齐 Java：失败仅日志，不阻塞）
    await node_service.scp_to_enabled_slaves(db, csv_filepath, csv_dir)

    # 8. 修改 JMX 把 CSV 节点的 filename 改为绝对路径（含 debug 副本）
    if os.path.exists(jmx_filepath):
        jmeter_xml.update_csv_filename(jmx_filepath, src_name, csv_filepath)
    if os.path.exists(debug_jmx_filepath):
        jmeter_xml.update_csv_filename(debug_jmx_filepath, src_name, csv_filepath)

    return True


async def delete_csv(db: AsyncSession, id: int) -> bool:
    obj = await csv_crud.get_by_id(db, id)
    if obj is None:
        raise MysteriousException(Codes.FILE_NOT_EXIST)

    log.info("删除 CSV id=%s, file=%s%s", id, obj.csv_dir, obj.dst_name)
    await csv_crud.delete(db, id)

    # 删 master 节点磁盘文件
    csv_filepath = obj.csv_dir + obj.dst_name
    if os.path.exists(csv_filepath):
        await aiofiles.os.remove(csv_filepath)

    # 同步删除所有 enabled slave 上的对应文件
    await node_service.rm_on_enabled_slaves(db, csv_filepath)
    return True


async def get_csv_list(db: AsyncSession, query: CsvQuery) -> PageVO[CsvVO]:
    page_vo: PageVO[CsvVO] = PageVO(page=query.page, size=query.size, total=0, list=[])
    total = await csv_crud.count(db, src_name=query.src_name, test_case_id=query.test_case_id)
    if total <= 0:
        return page_vo
    page_vo.total = total
    offset = PageVO.offset(query.page, query.size)
    items = await csv_crud.list_csvs(
        db, src_name=query.src_name, test_case_id=query.test_case_id, offset=offset, limit=query.size
    )
    page_vo.list = [_to_vo(o) for o in items]
    return page_vo


async def get_by_test_case_id(db: AsyncSession, test_case_id: int) -> list[CsvVO]:
    items = await csv_crud.get_by_test_case_id(db, test_case_id)
    return [_to_vo(o) for o in items]


async def get_csv_vo(db: AsyncSession, id: int) -> CsvVO:
    obj = await csv_crud.get_by_id(db, id)
    if obj is None:
        raise MysteriousException(Codes.FILE_NOT_EXIST)
    return _to_vo(obj)
