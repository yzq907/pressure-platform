"""FastAPI 鉴权依赖。复刻 Java AuthInterceptor 行为：

1. 优先从 Header `token` 取，回退到 query param `token`
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
from app.crud import user as user_crud
from app.db.session import get_db

log = logging.getLogger(__name__)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _extract_token(request: Request) -> str | None:
    """Header 优先，query param 兜底（对齐 TokenUtils.java）"""
    token = request.headers.get("token")
    if token:
        return token.strip()
    token = request.query_params.get("token")
    return token.strip() if token else None


async def get_current_user_dep(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> UserContext:
    """FastAPI 依赖：从请求中提取 token 并返回登录用户上下文。失败抛 MysteriousException。"""
    token = _extract_token(request)
    if not token:
        log.warning("missing token at %s", request.url.path)
        raise MysteriousException(Codes.USER_NOT_LOGIN)

    user = await user_crud.get_by_token(db, token)
    if user is None:
        log.warning("user not found for token at %s", request.url.path)
        raise MysteriousException(Codes.USER_NOT_EXIST)

    # MySQL DATETIME 是无时区的，按 Asia/Shanghai 本地时间解读
    now_local = datetime.now(SHANGHAI).replace(tzinfo=None)
    if user.expire_time < now_local:
        log.warning(
            "token expired at %s (expire=%s now=%s)", request.url.path, user.expire_time, now_local
        )
        raise MysteriousException(Codes.USER_TOKEN_EXPIRE)

    ctx = UserContext(id=user.id, username=user.username, real_name=user.real_name or "")
    set_current_user(ctx)
    return ctx
