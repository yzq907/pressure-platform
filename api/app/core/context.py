"""当前用户上下文。Python 用 ContextVar 替代 Java 的 NamedThreadLocal，asyncio 安全。"""

from __future__ import annotations

from contextvars import ContextVar

from pydantic import BaseModel


class UserContext(BaseModel):
    """登录用户的上下文信息（不含密码）"""

    id: int
    username: str
    real_name: str = ""


_current_user_var: ContextVar[UserContext | None] = ContextVar("current_user", default=None)


def get_current_user() -> UserContext | None:
    return _current_user_var.get()


def set_current_user(user: UserContext) -> None:
    _current_user_var.set(user)


def clear_current_user() -> None:
    _current_user_var.set(None)
