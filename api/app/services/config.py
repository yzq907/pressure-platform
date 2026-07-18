"""Config 业务服务，对齐 Java IConfigService + ConfigService。"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import stamp_create, stamp_modify
from app.core.codes import Codes
from app.core.config_catalog import (
    CATEGORIES,
    DEFAULT_CONFIG_VALUES,
    get_category_name,
    get_category_sort,
    get_config_meta,
)
from app.core.context import UserContext
from app.core.exceptions import MysteriousException
from app.core.response import PageVO
from app.crud import config as crud
from app.models.config import Config
from app.schemas.config import ConfigCategoryVO, ConfigParam, ConfigQuery, ConfigVO

log = logging.getLogger(__name__)
_LEGACY_DEFAULT_VALUE_MIGRATIONS = {
    "REPORT_RUNNING_METRIC_REFRESH_SECONDS": {"30": "5"},
}


def _check_param(param: ConfigParam) -> None:
    if param is None:
        raise MysteriousException(Codes.PARAMS_EMPTY)
    if not (param.config_key and param.config_value and param.description):
        raise MysteriousException(Codes.PARAM_MISSING)


def _to_vo(obj: Config) -> ConfigVO:
    meta = get_config_meta(obj.config_key)
    vo = ConfigVO.model_validate(obj)
    vo.category = meta.category
    vo.category_name = get_category_name(meta.category)
    vo.display_name = meta.display_name
    vo.value_type = meta.value_type
    vo.sort = get_category_sort(meta.category) * 1000 + meta.sort
    return vo


def _matches_category(obj: Config, category: str | None) -> bool:
    if not category or category == "all":
        return True
    return get_config_meta(obj.config_key).category == category


async def add_config(db: AsyncSession, param: ConfigParam, user: UserContext) -> int:
    _check_param(param)
    existing = await crud.get_by_key(db, param.config_key or "")
    if existing is not None:
        raise MysteriousException(Codes.CONFIG_EXIST)

    obj = Config(
        config_key=param.config_key or "",
        config_value=param.config_value or "",
        description=param.description or "",
    )
    stamp_create(obj, user)
    await crud.add(db, obj)
    return obj.id


async def update_config(
    db: AsyncSession, id: int, param: ConfigParam, user: UserContext
) -> bool:
    """Java 行为：用 param 整体覆盖，不查 key 冲突。"""
    existing = await crud.get_by_id(db, id)
    if existing is None:
        return False

    sent = param.model_dump(exclude_unset=True, exclude_none=True, by_alias=False)
    if "config_key" in sent:
        existing.config_key = sent["config_key"]
    if "config_value" in sent:
        existing.config_value = sent["config_value"]
    if "description" in sent:
        existing.description = sent["description"]
    stamp_modify(existing, user)
    return await crud.update(db, existing)


async def delete_config(db: AsyncSession, id: int) -> bool:
    existing = await crud.get_by_id(db, id)
    if existing is None:
        return False
    return await crud.delete(db, id)


async def get_config_list(db: AsyncSession, query: ConfigQuery) -> PageVO[ConfigVO]:
    page_vo: PageVO[ConfigVO] = PageVO(page=query.page, size=query.size, total=0, list=[])
    if query.category:
        configs = await crud.list_all_configs(db, config_key=query.config_key)
        vos = [_to_vo(c) for c in configs if _matches_category(c, query.category)]
        vos.sort(key=lambda item: (item.sort, item.config_key))
        page_vo.total = len(vos)
        offset = PageVO.offset(query.page, query.size)
        page_vo.list = vos[offset:offset + query.size]
        return page_vo

    total = await crud.count(db, config_key=query.config_key)
    if total <= 0:
        return page_vo
    page_vo.total = total
    offset = PageVO.offset(query.page, query.size)
    configs = await crud.list_configs(db, config_key=query.config_key, offset=offset, limit=query.size)
    page_vo.list = sorted([_to_vo(c) for c in configs], key=lambda item: (item.sort, item.config_key))
    return page_vo


async def get_categories() -> list[ConfigCategoryVO]:
    return [ConfigCategoryVO(key=item.key, name=item.name, sort=item.sort) for item in CATEGORIES]


async def ensure_default_configs(db: AsyncSession) -> int:
    """补齐新增的内置配置项，不覆盖用户已有配置。"""
    created = 0
    migrated = 0
    for key, default_value in DEFAULT_CONFIG_VALUES.items():
        existing = await crud.get_by_key(db, key)
        if existing is not None:
            migration = _LEGACY_DEFAULT_VALUE_MIGRATIONS.get(key, {})
            migrated_value = migration.get(existing.config_value)
            if migrated_value is not None:
                existing.config_value = migrated_value
                migrated += 1
            continue
        meta = get_config_meta(key)
        db.add(
            Config(
                config_key=key,
                config_value=default_value,
                description=meta.display_name,
            )
        )
        created += 1
    if created or migrated:
        await db.commit()
        log.info("补齐默认配置项 %d 个，迁移旧默认值 %d 个", created, migrated)
    return created


async def get_value(db: AsyncSession, key: str) -> str:
    """对齐 Java IConfigService.getValue：找不到抛 CONFIG_NOT_EXIST + key"""
    value = await crud.get_value(db, key)
    if value is None or value == "":
        raise MysteriousException(Codes.CONFIG_NOT_EXIST, message=f"配置不存在: {key}")
    return value


async def get_value_or_default(db: AsyncSession, key: str, default: str = "") -> str:
    value = await crud.get_value(db, key)
    return value if value not in (None, "") else default


async def get_options(db: AsyncSession, type: str) -> list[str]:
    """获取指定类型的选项列表（biz/service/version）。

    约定使用 config_key 为 BIZ_OPTIONS / SERVICE_OPTIONS / VERSION_OPTIONS，
    值用逗号分隔存储多个选项。
    """
    key_map = {
        "biz": "BIZ_OPTIONS",
        "service": "SERVICE_OPTIONS",
        "version": "VERSION_OPTIONS",
        "region": "REGION_OPTIONS",
    }
    key = key_map.get(type)
    if key is None:
        raise MysteriousException(Codes.PARAM_WRONG, message=f"不支持的类型: {type}")

    value = await crud.get_value(db, key)
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]
