"""User 业务服务层。对齐 Java IUserService + UserService。"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import Codes
from app.core.context import UserContext
from app.core.exceptions import MysteriousException
from app.core.permissions import ADMIN_ROLE_CODE, ALL_PERMISSION_CODES, DEFAULT_ROLE_CODE, PERMISSION_USER
from app.core.response import PageVO
from app.core.security import check_password_strength, generate_token, hash_password, token_expire_time, verify_password
from app.crud import role as role_crud
from app.crud import user as user_crud
from app.crud import user_session as user_session_crud
from app.models.role import Role
from app.models.user import User
from app.models.user_session import UserSession
from app.schemas.user import CurrentUserVO, UpdatePasswordParam, UserParam, UserQuery, UserVO

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


async def _ensure_role(db: AsyncSession, *, code: str, name: str, builtin: int, permissions: list[str]) -> Role:
    role = await role_crud.get_by_code(db, code)
    if role is None:
        role = Role(name=name, code=code, description=name, builtin=builtin)
        db.add(role)
        await db.flush()
    else:
        role.name = role.name or name
        if code == ADMIN_ROLE_CODE:
            role.builtin = builtin
    if permissions:
        await role_crud.replace_permissions(db, role.id, permissions)
        role = await role_crud.get_by_code(db, code) or role
    return role


async def _default_role_id(db: AsyncSession) -> int:
    role = await role_crud.get_by_code(db, DEFAULT_ROLE_CODE)
    if role is None:
        role = await _ensure_role(
            db,
            code=DEFAULT_ROLE_CODE,
            name="普通用户",
            builtin=1,
            permissions=["testcase", "case-generation", "jmx", "csv", "jar", "execution", "report"],
        )
        await db.commit()
    return role.id


async def _can_manage_users(db: AsyncSession, current: UserContext) -> bool:
    if current.role_code == ADMIN_ROLE_CODE:
        return True
    if not current.role_id:
        return False
    return PERMISSION_USER in await role_crud.list_permissions(db, current.role_id)


async def _resolve_role_id(db: AsyncSession, role_id: int | None) -> int:
    if not role_id:
        return await _default_role_id(db)
    role = await role_crud.get_by_id(db, role_id)
    if role is None:
        raise MysteriousException(Codes.PARAM_WRONG, message="角色不存在")
    return role.id


async def _to_vo(db: AsyncSession, user: User, mask_password: bool = False) -> UserVO:
    role = await role_crud.get_by_id(db, user.role_id) if user.role_id else None
    return UserVO(
        id=user.id,
        username=user.username,
        password="******" if mask_password else (user.password or ""),
        real_name=user.real_name or "",
        role_id=role.id if role else 0,
        role_name=role.name if role else "",
        role_code=role.code if role else "",
        effect_time=user.effect_time,
        expire_time=user.expire_time,
    )


async def add_user(db: AsyncSession, param: UserParam) -> int:
    _check_param(param)
    existing = await user_crud.get_by_username(db, param.username or "")
    if existing is not None:
        raise MysteriousException(Codes.USER_EXIST)

    ok, reason = check_password_strength(param.password or "", param.username or "")
    if not ok:
        raise MysteriousException(Codes.USER_PASSWORD_TOO_WEAK, message=reason)

    user = User(
        username=param.username or "",
        password=hash_password(param.password or ""),
        real_name=param.real_name or "",
        role_id=await _resolve_role_id(db, param.role_id),
    )
    _refresh_token(user)
    user = await user_crud.add(db, user)
    return user.id


async def delete_user(db: AsyncSession, id: int, current: UserContext) -> bool:
    existing = await user_crud.get_by_id(db, id)
    if existing is None:
        return False
    if existing.username == "admin":
        raise MysteriousException(Codes.FAIL, message="初始管理员不可删除")
    if existing.id != current.id and not await _can_manage_users(db, current):
        raise MysteriousException(Codes.FAIL, message="无权删除其他用户")
    return await user_crud.delete(db, id)


async def update_user(db: AsyncSession, id: int, param: UserParam, current: UserContext) -> bool:
    """对齐 Java updateUser。密码修改统一走 /user/updatePassword，此处只处理 username/real_name。"""
    existing = await user_crud.get_by_id(db, id)
    if existing is None:
        return False
    can_manage = await _can_manage_users(db, current)
    if existing.id != current.id and not can_manage:
        raise MysteriousException(Codes.FAIL, message="无权修改其他用户")

    sent = param.model_dump(exclude_unset=True, exclude_none=True, by_alias=False)
    if "username" in sent:
        same_name_user = await user_crud.get_by_username(db, sent["username"])
        if same_name_user is not None and same_name_user.id != existing.id:
            raise MysteriousException(Codes.USER_EXIST)
        existing.username = sent["username"]
    if "real_name" in sent:
        existing.real_name = sent["real_name"]
    if "role_id" in sent and can_manage:
        existing.role_id = await _resolve_role_id(db, sent["role_id"])
    # 密码修改统一走 /user/updatePassword，避免 /user/update/{id} 成为绕过旧密码校验的后门
    return await user_crud.update(db, existing)


async def get_by_id(db: AsyncSession, id: int, current: UserContext) -> UserVO | None:
    user = await user_crud.get_by_id(db, id)
    if user is None:
        return None
    if user.id != current.id and not await _can_manage_users(db, current):
        raise MysteriousException(Codes.FAIL, message="无权查看其他用户")
    return await _to_vo(db, user, mask_password=True)


async def login(db: AsyncSession, param: UserParam) -> str:
    _check_param(param)
    user = await user_crud.get_by_username(db, param.username or "")
    if user is None:
        raise MysteriousException(Codes.USER_NOT_EXIST)
    if not verify_password(param.password or "", user.password or ""):
        raise MysteriousException(Codes.USER_PASSWORD_ERROR)

    now_local = datetime.now(SHANGHAI).replace(tzinfo=None)
    expire = token_expire_time().astimezone(SHANGHAI).replace(tzinfo=None)
    user.effect_time = now_local
    user.expire_time = expire
    session = UserSession(
        user_id=user.id,
        token=generate_token(),
        effect_time=now_local,
        expire_time=expire,
    )
    await user_session_crud.add(db, session)
    return session.token


async def ensure_admin_user(db: AsyncSession) -> None:
    """启动时检查并创建初始 admin 用户和内置角色。"""
    admin_role = await _ensure_role(
        db,
        code=ADMIN_ROLE_CODE,
        name="超级管理员",
        builtin=1,
        permissions=ALL_PERMISSION_CODES,
    )
    await _ensure_role(
        db,
        code=DEFAULT_ROLE_CODE,
        name="普通用户",
        builtin=1,
        permissions=["testcase", "case-generation", "jmx", "csv", "jar", "execution", "report"],
    )
    default_role_id = await _default_role_id(db)
    legacy_users = await user_crud.list_by_role_id(db, 0)
    changed = False
    for legacy_user in legacy_users:
        if legacy_user.username != "admin":
            legacy_user.role_id = default_role_id
            changed = True
    existing = await user_crud.get_by_username(db, "admin")
    if existing is not None:
        if not existing.role_id:
            existing.role_id = admin_role.id
            changed = True
        if changed:
            await db.commit()
        return

    user = User(
        username="admin",
        password=hash_password("Emm@2025"),
        real_name="管理员",
        role_id=admin_role.id,
    )
    _refresh_token(user)
    await user_crud.add(db, user)
    log.info("初始 admin 用户已创建: admin / Emm@2025")


async def update_password(
    db: AsyncSession, param: UpdatePasswordParam, current: UserContext
) -> bool:
    """修改密码：校验旧密码，更新为新密码（bcrypt 加密）。

    如果 param.id 有值且与当前用户不同，视为管理员重置他人密码，跳过旧密码校验。
    """
    target_id = param.id if param.id is not None else current.id
    is_self = target_id == current.id

    if not param.new_password:
        raise MysteriousException(Codes.PARAM_MISSING)

    user = await user_crud.get_by_id(db, target_id)
    if user is None:
        raise MysteriousException(Codes.USER_NOT_EXIST)
    if not is_self and not await _can_manage_users(db, current):
        raise MysteriousException(Codes.FAIL, message="无权重置其他用户密码")

    if is_self:
        if not param.old_password:
            raise MysteriousException(Codes.PARAM_MISSING)
        if param.old_password == param.new_password:
            raise MysteriousException(Codes.FAIL, message="新密码不能与旧密码相同")
        if not verify_password(param.old_password, user.password or ""):
            raise MysteriousException(Codes.USER_PASSWORD_ERROR)

    ok, reason = check_password_strength(param.new_password, user.username or "")
    if not ok:
        raise MysteriousException(Codes.USER_PASSWORD_TOO_WEAK, message=reason)

    user.password = hash_password(param.new_password)
    await user_session_crud.delete_by_user_id(db, user.id)
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
    page_vo.list = [await _to_vo(db, u, mask_password=True) for u in users]
    return page_vo


async def get_current_user_info(db: AsyncSession, current: UserContext) -> CurrentUserVO:
    permissions = (
        ALL_PERMISSION_CODES
        if current.role_code == ADMIN_ROLE_CODE
        else await role_crud.list_permissions(db, current.role_id)
    )
    return CurrentUserVO(
        id=current.id,
        username=current.username,
        real_name=current.real_name,
        role_id=current.role_id,
        role_code=current.role_code,
        role_name=current.role_name,
        permissions=permissions,
    )
