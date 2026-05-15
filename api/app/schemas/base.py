"""Pydantic 公共基类。

约定：所有对外的 schema 都继承 CamelModel
- 输出 JSON：camelCase（前端兼容）
- 接收 JSON：camelCase / snake_case 都接受（populate_by_name）
- 可以直接 `model_validate(orm_obj)`（from_attributes）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, field_serializer
from pydantic.alias_generators import to_camel

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _fmt_dt(dt: datetime | None) -> str | None:
    """对齐 Java @JsonFormat: yyyy-MM-dd HH:mm:ss"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return dt.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


class CamelModel(BaseModel):
    """对外 schema 基类：JSON 字段 camelCase，Python 字段 snake_case"""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        from_attributes=True,
    )

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        """默认按 alias 序列化，保证输出 camelCase"""
        kwargs.setdefault("by_alias", True)
        return super().model_dump(**kwargs)

    def model_dump_json(self, **kwargs: Any) -> str:
        kwargs.setdefault("by_alias", True)
        return super().model_dump_json(**kwargs)


class BaseQuery(CamelModel):
    """分页查询基类，对齐 Java BaseQuery"""

    page: int = 1
    size: int = 10


class BaseVO(CamelModel):
    """对外 VO 基类，对齐 Java BaseVO：id + 审计字段。

    datetime 字段输出 `yyyy-MM-dd HH:mm:ss` 字符串，对齐 Java @JsonFormat。
    """

    id: int | None = None
    creator_id: str | None = None
    creator: str | None = None
    modifier_id: str | None = None
    modifier: str | None = None
    create_time: datetime | None = None
    modify_time: datetime | None = None

    @field_serializer("create_time", "modify_time", when_used="json")
    def _ser_audit_dt(self, v: datetime | None) -> str | None:
        return _fmt_dt(v)
