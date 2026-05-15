"""审计字段工具，对齐 Java CRUDEntity.addT / updateT 行为。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

from app.core.context import UserContext

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _now_naive() -> datetime:
    """对齐 Java LocalDateTime.now()：Shanghai 本地时间，无时区"""
    return datetime.now(SHANGHAI).replace(tzinfo=None)


def stamp_create(orm_obj: Any, user: UserContext) -> None:
    """add 操作：同时设置 creator/modifier 和 create_time/modify_time"""
    now = _now_naive()
    name = user.real_name or user.username
    orm_obj.creator = name
    orm_obj.creator_id = str(user.id)
    orm_obj.modifier = name
    orm_obj.modifier_id = str(user.id)
    orm_obj.create_time = now
    orm_obj.modify_time = now


def stamp_modify(orm_obj: Any, user: UserContext) -> None:
    """update 操作：只刷新 modifier / modify_time"""
    orm_obj.modifier = user.real_name or user.username
    orm_obj.modifier_id = str(user.id)
    orm_obj.modify_time = _now_naive()
