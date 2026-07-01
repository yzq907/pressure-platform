"""Csv 业务服务。"""

from __future__ import annotations

import logging
import hashlib
import ntpath
import os
from pathlib import Path

import aiofiles
import aiofiles.os
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import jmeter_xml
from app.core.audit import stamp_create, stamp_modify
from app.core.codes import Codes
from app.core.context import UserContext
from app.core.enums import JMeterScript
from app.core.exceptions import MysteriousException
from app.core.response import PageVO
from app.services import config as config_service
from app.crud import csv_resource as csv_resource_crud
from app.crud import jmx as jmx_crud
from app.crud import testcase_csv_binding as binding_crud
from app.crud import testcase_upload_file_binding as upload_file_binding_crud
from app.crud import testcase as testcase_crud
from app.models.csv_resource import CsvResource
from app.models.testcase_csv_binding import TestcaseCsvBinding
from app.schemas.csv import CsvBindingParam, CsvBindingVO, CsvResourceQuery, CsvResourceVO

log = logging.getLogger(__name__)
CSV_DISTRIBUTION_SHARED = "shared"
CSV_DISTRIBUTION_SPLIT_BY_SLAVE = "split_by_slave"
PUBLIC_FILE_TYPE_CSV = "csv"
PUBLIC_FILE_TYPE_UPLOAD_FILE = "upload_file"
_CSV_DISTRIBUTION_STRATEGIES = {CSV_DISTRIBUTION_SHARED, CSV_DISTRIBUTION_SPLIT_BY_SLAVE}


def _check_csv_name(name: str | None) -> None:
    lower_name = (name or "").lower()
    if (
        not name
        or " " in name
        or "/" in name
        or "\\" in name
        or not (lower_name.endswith(".csv") or lower_name.endswith(".dat"))
    ):
        raise MysteriousException(Codes.CSV_NAME_ERROR)


def _check_public_file_name(name: str | None) -> str:
    value = (name or "").strip()
    basename = os.path.basename(ntpath.basename(value))
    if not value or basename != value or value in (".", "..") or "/" in value or "\\" in value:
        raise MysteriousException(Codes.PARAM_WRONG, message="公共文件名称异常")
    return value


def _resource_file_type(filename: str) -> str:
    lower_name = filename.lower()
    if lower_name.endswith(".csv") or lower_name.endswith(".dat"):
        return PUBLIC_FILE_TYPE_CSV
    return PUBLIC_FILE_TYPE_UPLOAD_FILE


async def _common_csv_dir(db: AsyncSession) -> str:
    data_home = await config_service.get_value(db, "MASTER_DATA_HOME")
    return str(Path(data_home).resolve() / "common" / "csv") + os.sep


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resource_path(file_dir: str, filename: str) -> str:
    return str(Path(file_dir).resolve() / filename)


async def _resource_exists(db: AsyncSession, filename: str) -> bool:
    csv_dir = await _common_csv_dir(db)
    return Path(csv_dir, filename).exists()


async def _to_resource_vo(db: AsyncSession, obj: CsvResource, ref_count: int | None = None) -> CsvResourceVO:
    if ref_count is None:
        csv_ref_count = await binding_crud.count_by_filename(db, obj.filename)
        upload_file_ref_count = await upload_file_binding_crud.count_by_filename(db, obj.filename)
    else:
        csv_ref_count = ref_count
        upload_file_ref_count = await upload_file_binding_crud.count_by_filename(db, obj.filename)
    data = CsvResourceVO.model_validate(obj).model_dump(by_alias=False)
    if not data.get("file_type"):
        data["file_type"] = _resource_file_type(obj.filename)
    data["csv_reference_count"] = csv_ref_count
    data["upload_file_reference_count"] = upload_file_ref_count
    data["reference_count"] = csv_ref_count + upload_file_ref_count
    data["exists"] = Path(_resource_path(obj.file_dir, obj.filename)).exists()
    return CsvResourceVO(**data)


async def _to_binding_vo(db: AsyncSession, obj: TestcaseCsvBinding) -> CsvBindingVO:
    data = CsvBindingVO.model_validate(obj).model_dump(by_alias=False)
    data["exists"] = await _resource_exists(db, obj.filename)
    return CsvBindingVO(**data)


def _check_distribution_strategy(strategy: str | None) -> str:
    value = (strategy or "").strip() or CSV_DISTRIBUTION_SHARED
    if value not in _CSV_DISTRIBUTION_STRATEGIES:
        raise MysteriousException(Codes.PARAM_WRONG, message=f"不支持的参数文件分布式策略: {value}")
    return value


async def upload_public_csv(
    db: AsyncSession,
    csv_file: UploadFile,
    user: UserContext,
    *,
    overwrite: bool = False,
) -> bool:
    filename = _check_public_file_name(csv_file.filename)
    csv_dir = await _common_csv_dir(db)
    csv_path = _resource_path(csv_dir, filename)
    file_type = _resource_file_type(filename)
    existing = await csv_resource_crud.get_by_filename(db, filename)
    if existing is not None and not overwrite:
        raise MysteriousException(Codes.CSV_IS_EXIST, message=f"公共参数化文件「{filename}」已存在，确认后可覆盖上传")

    content = await csv_file.read()
    await aiofiles.os.makedirs(csv_dir, exist_ok=True)
    async with aiofiles.open(csv_path, "wb") as f:
        await f.write(content)

    checksum = _sha256(content)
    if existing is None:
        obj = CsvResource(
            filename=filename,
            file_dir=csv_dir,
            file_type=file_type,
            description=filename,
            file_size=len(content),
            checksum=checksum,
        )
        stamp_create(obj, user)
        await csv_resource_crud.add(db, obj)
    else:
        existing.file_dir = csv_dir
        existing.file_type = file_type
        existing.file_size = len(content)
        existing.checksum = checksum
        stamp_modify(existing, user)
        await csv_resource_crud.update(db, existing)
    return True


async def get_public_csv_list(db: AsyncSession, query: CsvResourceQuery) -> PageVO[CsvResourceVO]:
    page_vo: PageVO[CsvResourceVO] = PageVO(page=query.page, size=query.size, total=0, list=[])
    total = await csv_resource_crud.count(db, filename=query.filename, file_type=query.file_type)
    if total <= 0:
        return page_vo
    page_vo.total = total
    offset = PageVO.offset(query.page, query.size)
    items = await csv_resource_crud.list_resources(db, query.filename, query.file_type, offset, query.size)
    filenames = [item.filename for item in items]
    csv_ref_counts = await binding_crud.count_by_filenames(db, filenames)
    upload_file_ref_counts = await upload_file_binding_crud.count_by_filenames(db, filenames)
    page_vo.list = []
    for item in items:
        data = CsvResourceVO.model_validate(item).model_dump(by_alias=False)
        if not data.get("file_type"):
            data["file_type"] = _resource_file_type(item.filename)
        data["csv_reference_count"] = csv_ref_counts.get(item.filename, 0)
        data["upload_file_reference_count"] = upload_file_ref_counts.get(item.filename, 0)
        data["reference_count"] = data["csv_reference_count"] + data["upload_file_reference_count"]
        data["exists"] = Path(_resource_path(item.file_dir, item.filename)).exists()
        page_vo.list.append(CsvResourceVO(**data))
    return page_vo


async def get_public_csv_vo(db: AsyncSession, filename: str) -> CsvResourceVO:
    _check_public_file_name(filename)
    obj = await csv_resource_crud.get_by_filename(db, filename)
    if obj is None:
        raise MysteriousException(Codes.CSV_NOT_EXIST)
    return await _to_resource_vo(db, obj)


async def delete_public_csv(db: AsyncSession, filename: str, *, force: bool = False) -> bool:
    filename = _check_public_file_name(filename)
    obj = await csv_resource_crud.get_by_filename(db, filename)
    if obj is None:
        raise MysteriousException(Codes.CSV_NOT_EXIST)
    csv_ref_count = await binding_crud.count_by_filename(db, filename)
    upload_file_ref_count = await upload_file_binding_crud.count_by_filename(db, filename)
    ref_count = csv_ref_count + upload_file_ref_count
    if ref_count > 0 and not force:
        raise MysteriousException(
            Codes.FAIL,
            message=f"公共参数化文件「{filename}」已被 {ref_count} 个用例引用，确认后可删除；绑定会保留，重新上传同名文件后自动恢复",
        )
    filepath = _resource_path(obj.file_dir, obj.filename)
    if os.path.exists(filepath):
        await aiofiles.os.remove(filepath)
    await csv_resource_crud.delete_by_filename(db, filename)
    return True


async def update_public_csv_content(db: AsyncSession, filename: str, content: str, user: UserContext) -> bool:
    _check_public_file_name(filename)
    obj = await csv_resource_crud.get_by_filename(db, filename)
    if obj is None:
        raise MysteriousException(Codes.CSV_NOT_EXIST)
    data = content.encode("utf-8")
    filepath = _resource_path(obj.file_dir, obj.filename)
    async with aiofiles.open(filepath, "w", encoding="utf-8", newline="") as f:
        await f.write(content)
    obj.file_size = len(data)
    obj.checksum = _sha256(data)
    stamp_modify(obj, user)
    await csv_resource_crud.update(db, obj)
    return True


async def bind_public_csv(db: AsyncSession, param: CsvBindingParam, user: UserContext) -> bool:
    testcase = await testcase_crud.get_by_id(db, param.test_case_id)
    if testcase is None:
        raise MysteriousException(Codes.TESTCASE_NOT_EXIST)
    jmx = await jmx_crud.get_by_test_case_id(db, param.test_case_id)
    if jmx is None:
        raise MysteriousException(Codes.JMX_NOT_EXIST)
    filename = param.filename or ""
    _check_csv_name(filename)
    resource = await csv_resource_crud.get_by_filename(db, filename)
    if resource is None:
        raise MysteriousException(Codes.CSV_NOT_EXIST, message=f"公共参数化文件不存在: {filename}")
    if resource.file_type and resource.file_type != PUBLIC_FILE_TYPE_CSV:
        raise MysteriousException(Codes.CSV_NAME_ERROR, message=f"公共文件「{filename}」不是参数化文件")
    strategy = _check_distribution_strategy(param.distribution_strategy)
    jmx_filepath = jmx.jmx_dir + jmx.dst_name
    if jmx.jmeter_script_type == JMeterScript.UPLOAD_JMX.value and not jmeter_xml.exist_csv_filename(jmx_filepath, filename):
        raise MysteriousException(
            Codes.CSV_NAME_ERROR,
            message=f"JMX脚本中未引用参数化文件「{filename}」，请先在CSV Data Set中配置同名文件",
        )
    existing = await binding_crud.get_by_case_filename(db, param.test_case_id, filename)
    if existing is not None:
        existing.distribution_strategy = strategy
        existing.description = param.description or existing.description
        stamp_modify(existing, user)
        await binding_crud.update(db, existing)
        return True

    obj = TestcaseCsvBinding(
        test_case_id=param.test_case_id,
        filename=filename,
        distribution_strategy=strategy,
        description=param.description or testcase.name,
    )
    stamp_create(obj, user)
    await binding_crud.add(db, obj)
    return True


async def get_bindings_by_test_case_id(db: AsyncSession, test_case_id: int) -> list[CsvBindingVO]:
    items = await binding_crud.get_by_test_case_id(db, test_case_id)
    return [await _to_binding_vo(db, item) for item in items]


async def delete_binding(db: AsyncSession, id: int) -> bool:
    obj = await binding_crud.get_by_id(db, id)
    if obj is None:
        raise MysteriousException(Codes.FILE_NOT_EXIST)
    return await binding_crud.delete(db, id)


async def update_binding_strategy(db: AsyncSession, id: int, strategy: str, user: UserContext) -> bool:
    obj = await binding_crud.get_by_id(db, id)
    if obj is None:
        raise MysteriousException(Codes.FILE_NOT_EXIST)
    obj.distribution_strategy = _check_distribution_strategy(strategy)
    stamp_modify(obj, user)
    await binding_crud.update(db, obj)
    return True
