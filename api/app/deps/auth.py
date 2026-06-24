"""FastAPI 鉴权依赖。

1. 从 Header `token` 取登录凭证
2. 反查 `mysterious_user` 表，找不到 → USER_NOT_EXIST
3. 用户存在但 expire_time 已过 → USER_TOKEN_EXPIRE
4. 通过后把 UserContext 写到 ContextVar
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import Codes
from app.core.context import UserContext, set_current_user
from app.core.exceptions import MysteriousException
from app.core.permissions import DEFAULT_ROLE_CODE
from app.crud import role as role_crud
from app.crud import user as user_crud
from app.crud import user_session as user_session_crud
from app.db.session import get_db

log = logging.getLogger(__name__)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _extract_token(request: Request) -> str | None:
    """只接受 Header token，避免 URL token 进入历史记录、访问日志或 Referer。"""
    token = request.headers.get("token")
    if token:
        return token.strip()
    return None


async def get_current_user_dep(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> UserContext:
    """FastAPI 依赖：从请求中提取 token 并返回登录用户上下文。失败抛 MysteriousException。"""
    token = _extract_token(request)
    if not token:
        log.warning("missing token at %s", request.url.path)
        raise MysteriousException(Codes.USER_NOT_LOGIN)

    session = await user_session_crud.get_by_token(db, token)
    user = await user_crud.get_by_id(db, session.user_id) if session is not None else None
    token_expire_time = session.expire_time if session is not None else None
    if user is None:
        user = await user_crud.get_by_token(db, token)
        token_expire_time = user.expire_time if user is not None else None
    if user is None:
        log.warning("user not found for token at %s", request.url.path)
        raise MysteriousException(Codes.USER_NOT_EXIST)

    # MySQL DATETIME 是无时区的，按 Asia/Shanghai 本地时间解读
    now_local = datetime.now(SHANGHAI).replace(tzinfo=None)
    if token_expire_time < now_local or user.expire_time < now_local:
        log.warning(
            "token expired at %s (expire=%s user_expire=%s now=%s)",
            request.url.path,
            token_expire_time,
            user.expire_time,
            now_local,
        )
        raise MysteriousException(Codes.USER_TOKEN_EXPIRE)

    role = await role_crud.get_by_id(db, user.role_id) if user.role_id else None
    ctx = UserContext(
        id=user.id,
        username=user.username,
        real_name=user.real_name or "",
        role_id=role.id if role else 0,
        role_code=role.code if role else DEFAULT_ROLE_CODE,
        role_name=role.name if role else "普通用户",
    )
    set_current_user(ctx)
    return ctx
