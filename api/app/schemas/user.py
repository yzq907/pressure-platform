"""User 相关的 Pydantic schemas。对齐 Java 端 UserParam / UserVO / UserQuery。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import field_serializer

from app.schemas.base import BaseQuery, CamelModel

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _fmt_dt(dt: datetime) -> str:
    """yyyy-MM-dd HH:mm:ss"""
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return dt.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


class UserParam(CamelModel):
    """新增/更新/登录使用的请求体"""

    username: str | None = None
    password: str | None = None
    real_name: str | None = None


class UserVO(CamelModel):
    """User 返回 VO"""

    id: int
    username: str = ""
    password: str = ""
    real_name: str = ""
    effect_time: datetime | None = None
    expire_time: datetime | None = None

    @field_serializer("effect_time", "expire_time", when_used="json")
    def _ser_dt(self, v: datetime | None) -> str | None:
        if v is None:
            return None
        return _fmt_dt(v)


class UpdatePasswordParam(CamelModel):
    """修改密码请求体"""

    id: int | None = None
    old_password: str | None = None
    new_password: str | None = None


class UserQuery(BaseQuery):
    """分页查询参数：基类提供 page/size，外加可选的 username/realName 模糊匹配"""

    username: str | None = None
    real_name: str | None = None
