"""上传接口文件资源业务服务。"""

from __future__ import annotations

import ntpath
import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import jmeter_xml
from app.core.audit import stamp_create, stamp_modify
from app.core.codes import Codes
from app.core.context import UserContext
from app.core.enums import JMeterScript
from app.core.exceptions import MysteriousException
from app.services import config as config_service
from app.crud import csv_resource as csv_resource_crud
from app.crud import jmx as jmx_crud
from app.crud import testcase as testcase_crud
from app.crud import testcase_upload_file_binding as binding_crud
from app.models.testcase_upload_file_binding import TestcaseUploadFileBinding
from app.schemas.upload_file import UploadFileBindingParam, UploadFileBindingVO


def _check_upload_file_name(name: str | None) -> str:
    value = (name or "").strip()
    basename = os.path.basename(ntpath.basename(value))
    if not value or basename != value or value in (".", "..") or "/" in value or "\\" in value:
        raise MysteriousException(Codes.PARAM_WRONG, message="上传文件名称异常")
    return value


async def _common_file_dir(db: AsyncSession) -> str:
    data_home = await config_service.get_value(db, "MASTER_DATA_HOME")
    return str(Path(data_home).resolve() / "common" / "csv") + os.sep


async def _public_file_exists(db: AsyncSession, filename: str) -> bool:
    return Path(await _common_file_dir(db), filename).exists()


async def _to_binding_vo(db: AsyncSession, obj: TestcaseUploadFileBinding) -> UploadFileBindingVO:
    data = UploadFileBindingVO.model_validate(obj).model_dump(by_alias=False)
    data["exists"] = await _public_file_exists(db, obj.filename)
    return UploadFileBindingVO(**data)


async def bind_public_upload_file(db: AsyncSession, param: UploadFileBindingParam, user: UserContext) -> bool:
    testcase = await testcase_crud.get_by_id(db, param.test_case_id)
    if testcase is None:
        raise MysteriousException(Codes.TESTCASE_NOT_EXIST)
    jmx = await jmx_crud.get_by_test_case_id(db, param.test_case_id)
    if jmx is None:
        raise MysteriousException(Codes.JMX_NOT_EXIST)

    filename = _check_upload_file_name(param.filename)
    resource = await csv_resource_crud.get_by_filename(db, filename)
    if resource is None or not Path(resource.file_dir, resource.filename).exists():
        raise MysteriousException(Codes.FILE_NOT_EXIST, message=f"公共上传文件不存在: {filename}，请先在数据管理上传")

    jmx_filepath = jmx.jmx_dir + jmx.dst_name
    if jmx.jmeter_script_type == JMeterScript.UPLOAD_JMX.value:
        if not jmeter_xml.exist_upload_file_path(jmx_filepath, filename):
            raise MysteriousException(
                Codes.FAIL,
                message=f"JMX脚本中未引用上传文件「{filename}」，请先在HTTP请求文件上传配置中填写同名文件",
            )

    existing = await binding_crud.get_by_case_filename(db, param.test_case_id, filename)
    if existing is not None:
        existing.description = param.description or existing.description
        stamp_modify(existing, user)
        await binding_crud.update(db, existing)
        return True

    obj = TestcaseUploadFileBinding(
        test_case_id=param.test_case_id,
        filename=filename,
        description=param.description or testcase.name,
    )
    stamp_create(obj, user)
    await binding_crud.add(db, obj)
    return True


async def get_bindings_by_test_case_id(db: AsyncSession, test_case_id: int) -> list[UploadFileBindingVO]:
    items = await binding_crud.get_by_test_case_id(db, test_case_id)
    return [await _to_binding_vo(db, item) for item in items]


async def delete_binding(db: AsyncSession, id: int) -> bool:
    obj = await binding_crud.get_by_id(db, id)
    if obj is None:
        raise MysteriousException(Codes.FILE_NOT_EXIST)
    return await binding_crud.delete(db, id)
