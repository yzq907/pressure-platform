"""User 业务服务层。对齐 Java IUserService + UserService。"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import Codes
from app.core.context import UserContext
from app.core.exceptions import MysteriousException
from app.core.response import PageVO
from app.core.security import generate_token, hash_password, token_expire_time, verify_password
from app.crud import user as user_crud
from app.models.user import User
from app.schemas.user import UpdatePasswordParam, UserParam, UserQuery, UserVO

log = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _check_param(param: UserParam) -> None:
    """对齐 Java checkUserParam"""
    if param is None:
        raise MysteriousException(Codes.PARAMS_EMPTY)
    if not param.username or not param.password:
        raise MysteriousException(Codes.PARAM_MISSING)


def _refresh_token(user: User) -> None:
    """新增/更新/登录时统一刷新 token，对齐 Java refreshToken"""
    now_local = datetime.now(SHANGHAI).replace(tzinfo=None)
    expire = token_expire_time().astimezone(SHANGHAI).replace(tzinfo=None)
    user.token = generate_token()
    user.effect_time = now_local
    user.expire_time = expire


def _to_vo(user: User, mask_password: bool = False) -> UserVO:
    return UserVO(
        id=user.id,
        username=user.username,
        password="******" if mask_password else (user.password or ""),
        real_name=user.real_name or "",
        effect_time=user.effect_time,
        expire_time=user.expire_time,
    )


async def add_user(db: AsyncSession, param: UserParam) -> int:
    _check_param(param)
    existing = await user_crud.get_by_username(db, param.username or "")
    if existing is not None:
        raise MysteriousException(Codes.USER_EXIST)

    user = User(
        username=param.username or "",
        password=hash_password(param.password or ""),
        real_name=param.real_name or "",
    )
    _refresh_token(user)
    user = await user_crud.add(db, user)
    return user.id


async def delete_user(db: AsyncSession, id: int) -> bool:
    existing = await user_crud.get_by_id(db, id)
    if existing is None:
        return False
    return await user_crud.delete(db, id)


async def update_user(db: AsyncSession, id: int, param: UserParam) -> bool:
    """对齐 Java updateUser，但修复了 Java 端"更新时不加密密码"的 bug"""
    existing = await user_crud.get_by_id(db, id)
    if existing is None:
        return False

    # Java 端 BeanConverter 把 UserParam 整体覆盖到 UserDO，但 MyBatis 动态 SET 只更新非 null 字段。
    # Python 这里用 model_dump(exclude_unset=True) 拿到客户端真正提交的字段，
    # 没传的字段保持原值。
    sent = param.model_dump(exclude_unset=True, exclude_none=True, by_alias=False)
    if "username" in sent:
        existing.username = sent["username"]
    if "real_name" in sent:
        existing.real_name = sent["real_name"]
    if "password" in sent:
        # 修复 Java bug：更新密码时也要 bcrypt
        existing.password = hash_password(sent["password"])

    _refresh_token(existing)
    return await user_crud.update(db, existing)


async def get_by_id(db: AsyncSession, id: int) -> UserVO | None:
    user = await user_crud.get_by_id(db, id)
    if user is None:
        return None
    # Java 端 getById 不脱敏密码，照搬
    return _to_vo(user, mask_password=False)


async def login(db: AsyncSession, param: UserParam) -> str:
    _check_param(param)
    user = await user_crud.get_by_username(db, param.username or "")
    if user is None:
        raise MysteriousException(Codes.USER_NOT_EXIST)
    if not verify_password(param.password or "", user.password or ""):
        raise MysteriousException(Codes.USER_PASSWORD_ERROR)

    _refresh_token(user)
    await user_crud.update(db, user)
    return user.token


async def ensure_admin_user(db: AsyncSession) -> None:
    """启动时检查并创建初始 admin 用户，仅在用户表为空时执行。"""
    existing = await user_crud.get_by_username(db, "admin")
    if existing is not None:
        return

    user = User(
        username="admin",
        password=hash_password("Emm@2025"),
        real_name="管理员",
    )
    _refresh_token(user)
    await user_crud.add(db, user)
    log.info("初始 admin 用户已创建: admin / Emm@2025")


async def update_password(
    db: AsyncSession, user_id: int, param: UpdatePasswordParam, current: UserContext
) -> bool:
    """修改密码：校验旧密码，更新为新密码（bcrypt 加密）。"""
    if not param.old_password or not param.new_password:
        raise MysteriousException(Codes.PARAM_MISSING)
    if param.old_password == param.new_password:
        raise MysteriousException(Codes.FAIL, message="新密码不能与旧密码相同")

    user = await user_crud.get_by_id(db, user_id)
    if user is None:
        raise MysteriousException(Codes.USER_NOT_EXIST)

    # 校验旧密码
    if not verify_password(param.old_password, user.password or ""):
        raise MysteriousException(Codes.USER_PASSWORD_ERROR)

    user.password = hash_password(param.new_password)
    _refresh_token(user)
    await user_crud.update(db, user)
    return True


async def get_user_list(db: AsyncSession, query: UserQuery) -> PageVO[UserVO]:
    page_vo: PageVO[UserVO] = PageVO(page=query.page, size=query.size, total=0, list=[])
    total = await user_crud.count(db, username=query.username, real_name=query.real_name)
    if total <= 0:
        return page_vo
    page_vo.total = total

    offset = PageVO.offset(query.page, query.size)
    users = await user_crud.list_users(
        db, username=query.username, real_name=query.real_name, offset=offset, limit=query.size
    )
    # Java getUserList 把 password 替换为 ******
    page_vo.list = [_to_vo(u, mask_password=True) for u in users]
    return page_vo
