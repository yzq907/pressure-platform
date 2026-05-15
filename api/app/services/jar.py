"""Jar 业务服务。"""

from __future__ import annotations

import logging
import os
import re

import aiofiles
import aiofiles.os
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import jmeter_xml
from app.core.audit import stamp_create
from app.core.codes import Codes
from app.core.context import UserContext
from app.core.exceptions import MysteriousException
from app.core.response import PageVO
from app.crud import jar as jar_crud
from app.crud import jmx as jmx_crud
from app.crud import testcase as testcase_crud
from app.models.jar import Jar
from app.schemas.jar import JarQuery, JarVO
from app.services import config as config_service
from app.services import node as node_service

log = logging.getLogger(__name__)

# JMeter 配置 key
MASTER_JMETER_HOME_KEY = "MASTER_JMETER_HOME"

# 匹配 JMeter 插件的 JAR 文件名，对齐 Java JarService.isJmeterPluginJar
_PLUGIN_JAR_RE = re.compile(r"^jmeter-plugins-.*\.jar")


def _is_plugin_jar(filename: str) -> bool:
    return _PLUGIN_JAR_RE.search(filename) is not None


def _check_jar_name(name: str | None) -> None:
    if not name or " " in name or not name.endswith(".jar"):
        raise MysteriousException(Codes.JAR_NAME_ERROR)


def _to_vo(obj: Jar) -> JarVO:
    return JarVO.model_validate(obj)


async def upload_jar(
    db: AsyncSession,
    testcase_id: int,
    jar_file: UploadFile,
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
    src_name = jar_file.filename or ""
    _check_jar_name(src_name)
    dst_name = src_name

    # 4. 决定存储路径
    if _is_plugin_jar(src_name):
        jmeter_home = await config_service.get_value(db, MASTER_JMETER_HOME_KEY)
        jar_dir = os.path.join(jmeter_home, "lib", "ext") + os.sep
    else:
        jar_dir = os.path.join(testcase.test_case_dir, "jar") + os.sep
    jar_filepath = jar_dir + src_name

    # 5. 已存在判存
    existing = await jar_crud.get_exist_list(db, testcase_id, src_name, jar_dir)
    if existing:
        raise MysteriousException(Codes.JAR_IS_EXIST)

    # 6. 入库
    obj = Jar(
        src_name=src_name,
        dst_name=dst_name,
        description=testcase.name,
        jar_dir=jar_dir,
        test_case_id=testcase_id,
    )
    stamp_create(obj, user)
    await jar_crud.add(db, obj)

    # 7. 落盘
    await aiofiles.os.makedirs(jar_dir, exist_ok=True)
    content = await jar_file.read()
    async with aiofiles.open(jar_filepath, "wb") as f:
        await f.write(content)

    # 7.5. 同步到所有 enabled slave（含插件 JAR）
    await node_service.scp_to_enabled_slaves(db, jar_filepath, jar_dir)

    # 8. 修改 JMX 把 TestPlan.user_define_classpath 改为用例的 jar 目录
    # Java 端不论是插件还是普通 jar 都把 classpath 指向用例 jar 目录
    jmx_filepath = jmx.jmx_dir + jmx.dst_name
    debug_jmx_filepath = jmx.jmx_dir + "debug_" + jmx.dst_name
    testcase_jar_dir = os.path.join(testcase.test_case_dir, "jar")
    if os.path.exists(jmx_filepath):
        jmeter_xml.update_jar_classpath(jmx_filepath, testcase_jar_dir)
    if os.path.exists(debug_jmx_filepath):
        jmeter_xml.update_jar_classpath(debug_jmx_filepath, testcase_jar_dir)

    return True


async def delete_jar(db: AsyncSession, id: int) -> bool:
    obj = await jar_crud.get_by_id(db, id)
    if obj is None:
        raise MysteriousException(Codes.FILE_NOT_EXIST)

    log.info("删除 JAR id=%s", id)
    await jar_crud.delete(db, id)

    # 插件 JAR：master 和 slave 上的文件都不删（Java 行为：plugin 视为全局共享资源）
    jar_filepath = obj.jar_dir + obj.dst_name
    if _is_plugin_jar(obj.src_name):
        log.info("插件 JAR 不删任何节点的磁盘文件: %s", obj.src_name)
        return True

    if os.path.exists(jar_filepath):
        await aiofiles.os.remove(jar_filepath)

    # 同步删除所有 enabled slave 上的对应文件
    await node_service.rm_on_enabled_slaves(db, jar_filepath)
    return True


async def get_jar_list(db: AsyncSession, query: JarQuery) -> PageVO[JarVO]:
    page_vo: PageVO[JarVO] = PageVO(page=query.page, size=query.size, total=0, list=[])
    total = await jar_crud.count(db, src_name=query.src_name, test_case_id=query.test_case_id)
    if total <= 0:
        return page_vo
    page_vo.total = total
    offset = PageVO.offset(query.page, query.size)
    items = await jar_crud.list_jars(
        db, src_name=query.src_name, test_case_id=query.test_case_id, offset=offset, limit=query.size
    )
    page_vo.list = [_to_vo(o) for o in items]
    return page_vo


async def get_by_test_case_id(db: AsyncSession, test_case_id: int) -> list[JarVO]:
    items = await jar_crud.get_by_test_case_id(db, test_case_id)
    return [_to_vo(o) for o in items]


async def get_jar_vo(db: AsyncSession, id: int) -> JarVO:
    obj = await jar_crud.get_by_id(db, id)
    if obj is None:
        raise MysteriousException(Codes.FILE_NOT_EXIST)
    return _to_vo(obj)
