"""上传接口文件资源业务服务。"""

from __future__ import annotations

import logging
import ntpath
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
from app.crud import jmx as jmx_crud
from app.crud import testcase as testcase_crud
from app.crud import upload_file as upload_file_crud
from app.models.upload_file import UploadFileResource
from app.schemas.upload_file import UploadFileQuery, UploadFileVO
from app.services import node as node_service

log = logging.getLogger(__name__)


def _check_upload_file_name(name: str | None) -> str:
    value = (name or "").strip()
    basename = os.path.basename(ntpath.basename(value))
    if not value or basename != value or value in (".", "..") or "/" in value or "\\" in value:
        raise MysteriousException(Codes.PARAM_WRONG, message="上传文件名称异常")
    return value


def _to_vo(obj: UploadFileResource) -> UploadFileVO:
    return UploadFileVO.model_validate(obj)


async def upload_file(
    db: AsyncSession,
    testcase_id: int,
    upload_file: UploadFile,
    user: UserContext,
) -> bool:
    testcase = await testcase_crud.get_by_id(db, testcase_id)
    if testcase is None:
        raise MysteriousException(Codes.TESTCASE_NOT_EXIST)

    jmx = await jmx_crud.get_by_test_case_id(db, testcase_id)
    if jmx is None:
        raise MysteriousException(Codes.JMX_NOT_EXIST)

    src_name = _check_upload_file_name(upload_file.filename)
    dst_name = src_name
    file_dir = os.path.join(testcase.test_case_dir, "upload") + os.sep
    filepath = file_dir + dst_name

    jmx_filepath = jmx.jmx_dir + jmx.dst_name
    debug_jmx_filepath = jmx.jmx_dir + "debug_" + jmx.dst_name
    if jmx.jmeter_script_type == JMeterScript.UPLOAD_JMX.value:
        if not jmeter_xml.exist_upload_file_path(jmx_filepath, src_name):
            log.warning("JMX %s 里没有引用上传文件 %s", jmx_filepath, src_name)
            raise MysteriousException(
                Codes.FAIL,
                message=f"JMX脚本中未引用上传文件「{src_name}」，请先在HTTP请求文件上传配置中填写同名文件",
            )

    existing = await upload_file_crud.get_exist_list(db, testcase_id, src_name, file_dir)
    if existing:
        raise MysteriousException(Codes.UPLOAD_FILE_ERROR, message=f"上传文件「{src_name}」已存在")

    obj = UploadFileResource(
        src_name=src_name,
        dst_name=dst_name,
        description=testcase.name,
        file_dir=file_dir,
        test_case_id=testcase_id,
    )
    stamp_create(obj, user)
    await upload_file_crud.add(db, obj)

    await aiofiles.os.makedirs(file_dir, exist_ok=True)
    content = await upload_file.read()
    async with aiofiles.open(filepath, "wb") as f:
        await f.write(content)

    await node_service.scp_to_enabled_slaves(db, filepath, file_dir)

    rewrites = {src_name: filepath}
    if os.path.exists(jmx_filepath):
        jmeter_xml.update_upload_file_paths(jmx_filepath, rewrites)
    if os.path.exists(debug_jmx_filepath):
        jmeter_xml.update_upload_file_paths(debug_jmx_filepath, rewrites)

    return True


async def delete_upload_file(db: AsyncSession, id: int) -> bool:
    obj = await upload_file_crud.get_by_id(db, id)
    if obj is None:
        raise MysteriousException(Codes.FILE_NOT_EXIST)

    await upload_file_crud.delete(db, id)
    filepath = obj.file_dir + obj.dst_name
    if os.path.exists(filepath):
        await aiofiles.os.remove(filepath)
    await node_service.rm_on_enabled_slaves(db, filepath)
    return True


async def get_upload_file_list(db: AsyncSession, query: UploadFileQuery) -> PageVO[UploadFileVO]:
    page_vo: PageVO[UploadFileVO] = PageVO(page=query.page, size=query.size, total=0, list=[])
    total = await upload_file_crud.count(db, src_name=query.src_name, test_case_id=query.test_case_id)
    if total <= 0:
        return page_vo
    page_vo.total = total
    offset = PageVO.offset(query.page, query.size)
    items = await upload_file_crud.list_upload_files(
        db, src_name=query.src_name, test_case_id=query.test_case_id, offset=offset, limit=query.size
    )
    page_vo.list = [_to_vo(o) for o in items]
    return page_vo


async def get_by_test_case_id(db: AsyncSession, test_case_id: int) -> list[UploadFileVO]:
    items = await upload_file_crud.get_by_test_case_id(db, test_case_id)
    return [_to_vo(o) for o in items]


async def get_upload_file_vo(db: AsyncSession, id: int) -> UploadFileVO:
    obj = await upload_file_crud.get_by_id(db, id)
    if obj is None:
        raise MysteriousException(Codes.FILE_NOT_EXIST)
    return _to_vo(obj)
