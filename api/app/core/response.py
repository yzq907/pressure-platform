"""统一响应包装。对齐 Java 端的 Response<T> / ResponseStatus / PageVO<T>。

关键约束（前端兼容性）：
1. JSON 字段名必须是驼峰 `currentTime`，不能是 snake_case
2. `currentTime` 格式：`yyyy-MM-dd HH:mm:ss`，时区 Asia/Shanghai
3. 所有响应（包括异常）HTTP 状态码都是 200，错误信息靠 `code` 字段区分
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, List, TypeVar
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.core.codes import Code, Codes

T = TypeVar("T")

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _now_shanghai() -> datetime:
    return datetime.now(SHANGHAI)


class ResponseStatus(BaseModel):
    """无 data 字段的状态响应"""

    model_config = ConfigDict(populate_by_name=True)

    code: int
    message: str
    success: bool
    current_time: datetime = Field(default_factory=_now_shanghai, alias="currentTime")

    @field_serializer("current_time")
    def _serialize_time(self, v: datetime) -> str:
        return v.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


class Response(BaseModel, Generic[T]):
    """带 data 字段的响应包装"""

    model_config = ConfigDict(populate_by_name=True)

    code: int
    message: str
    success: bool
    current_time: datetime = Field(default_factory=_now_shanghai, alias="currentTime")
    data: T | None = None

    @field_serializer("current_time")
    def _serialize_time(self, v: datetime) -> str:
        return v.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


class PageVO(BaseModel, Generic[T]):
    """分页数据"""

    page: int = 0
    size: int = 0
    total: int = 0
    list: List[T] = Field(default_factory=list)

    @staticmethod
    def offset(page: int, size: int) -> int:
        if page <= 0:
            return 0
        return (page - 1) * size


def success(data: Any = None) -> Response[Any]:
    c = Codes.SUCCESS
    return Response(code=c.code, message=c.message, success=c.success, data=data)


def success_status() -> ResponseStatus:
    c = Codes.SUCCESS
    return ResponseStatus(code=c.code, message=c.message, success=c.success)


def fail(code: Code) -> ResponseStatus:
    return ResponseStatus(code=code.code, message=code.message, success=code.success)


def fail_with_message(code: int, message: str) -> ResponseStatus:
    return ResponseStatus(code=code, message=message, success=False)
