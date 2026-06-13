"""Phase 1 用户认证模块的集成测试

覆盖：
- /user/add, /user/login, /user/getById, /user/update, /user/list, /user/delete
- get_current_user_dep 鉴权依赖（通过临时注册一个测试路由触发）
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import Depends
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import UserContext
from app.core.response import success
from app.core.security import hash_password
from app.deps.auth import get_current_user_dep
from app.main import app
from app.models.user import User


# ---------------------------------------------------------------------------
# 给鉴权依赖测试用的"私有"路由（仅在 tests 包加载时注册一次）
# ---------------------------------------------------------------------------
async def _whoami(current: UserContext = Depends(get_current_user_dep)) -> Any:
    return success({"id": current.id, "username": current.username})


app.add_api_route("/_test/whoami", _whoami, methods=["GET"], include_in_schema=False)


async def _create_user(db: AsyncSession, username: str, password: str = "Password123", real_name: str = "") -> User:
    now = datetime.now()
    user = User(
        username=username,
        password=hash_password(password),
        real_name=real_name,
        effect_time=now,
        expire_time=now + timedelta(hours=12),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


# ---------------------------------------------------------------------------
# /user/add
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_add_user_success(auth_client: AsyncClient, db: AsyncSession) -> None:
    resp = await auth_client.post(
        "/user/add",
        json={"username": "alice", "password": "Secret123", "realName": "爱丽丝"},
    )
    body = resp.json()
    assert body["code"] == 0
    assert body["success"] is True
    new_id = body["data"]
    assert isinstance(new_id, int) and new_id > 0

    # 数据库里能查到
    user = (await db.execute(select(User).where(User.username == "alice"))).scalar_one()
    assert user.real_name == "爱丽丝"
    # 密码已 bcrypt 加密，不是明文
    assert user.password != "Secret123"
    assert user.password.startswith("$2")  # bcrypt 标识前缀
    # token 已生成
    assert len(user.token) >= 32


@pytest.mark.asyncio
async def test_add_user_duplicate(auth_client: AsyncClient) -> None:
    await auth_client.post("/user/add", json={"username": "bob", "password": "Password123"})
    resp = await auth_client.post("/user/add", json={"username": "bob", "password": "Password123"})
    body = resp.json()
    assert body["code"] == 1004  # USER_EXIST
    assert body["success"] is False
    assert "用户已存在" in body["message"]


@pytest.mark.asyncio
async def test_add_user_missing_param(auth_client: AsyncClient) -> None:
    # 缺 password
    resp = await auth_client.post("/user/add", json={"username": "carol"})
    body = resp.json()
    assert body["code"] == 1003  # PARAM_MISSING


# ---------------------------------------------------------------------------
# /user/login
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_login_success(client: AsyncClient, db: AsyncSession) -> None:
    await _create_user(db, "dave", "hunter2", "Dave")
    resp = await client.post("/user/login", json={"username": "dave", "password": "hunter2"})
    body = resp.json()
    assert body["code"] == 0
    token = body["data"]
    # 36 字符长度的 UUID v4
    assert isinstance(token, str) and len(token) == 36


@pytest.mark.asyncio
async def test_login_allows_multiple_active_tokens_for_same_user(client: AsyncClient, db: AsyncSession) -> None:
    await _create_user(db, "multi_login", "Password123", "Multi")

    first_login = await client.post("/user/login", json={"username": "multi_login", "password": "Password123"})
    first_token = first_login.json()["data"]
    second_login = await client.post("/user/login", json={"username": "multi_login", "password": "Password123"})
    second_token = second_login.json()["data"]

    assert first_token != second_token
    first_resp = await client.get("/_test/whoami", headers={"token": first_token})
    second_resp = await client.get("/_test/whoami", headers={"token": second_token})

    assert first_resp.json()["code"] == 0
    assert first_resp.json()["data"]["username"] == "multi_login"
    assert second_resp.json()["code"] == 0
    assert second_resp.json()["data"]["username"] == "multi_login"


@pytest.mark.asyncio
async def test_login_wrong_password(client: AsyncClient, db: AsyncSession) -> None:
    await _create_user(db, "eve", "correct")
    resp = await client.post("/user/login", json={"username": "eve", "password": "wrong"})
    body = resp.json()
    assert body["code"] == 1006  # USER_PASSWORD_ERROR


@pytest.mark.asyncio
async def test_login_user_not_exist(client: AsyncClient) -> None:
    resp = await client.post("/user/login", json={"username": "ghost", "password": "x"})
    body = resp.json()
    assert body["code"] == 1005  # USER_NOT_EXIST


# ---------------------------------------------------------------------------
# /user/getById
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_by_id_returns_camelcase_and_masks_password(auth_client: AsyncClient) -> None:
    add_resp = await auth_client.post(
        "/user/add",
        json={"username": "frank", "password": "Password123", "realName": "弗兰克"},
    )
    user_id = add_resp.json()["data"]

    resp = await auth_client.get(f"/user/getById/{user_id}")
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["id"] == user_id
    assert data["username"] == "frank"
    assert data["password"] == "******"
    # 关键：camelCase
    assert data["realName"] == "弗兰克"
    assert "real_name" not in data
    assert "effectTime" in data and "expireTime" in data
    assert "effect_time" not in data and "expire_time" not in data


@pytest.mark.asyncio
async def test_get_by_id_not_found(client: AsyncClient) -> None:
    resp = await client.get("/user/getById/999999")
    body = resp.json()
    assert body["code"] == 1007


@pytest.mark.asyncio
async def test_get_by_id_not_found_for_logged_in_user(auth_client: AsyncClient) -> None:
    resp = await auth_client.get("/user/getById/999999")
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] is None


@pytest.mark.asyncio
async def test_get_by_id_forbidden_for_other_user(auth_client: AsyncClient, client: AsyncClient) -> None:
    await auth_client.post("/user/add", json={"username": "viewer", "password": "Password123"})
    target_resp = await auth_client.post("/user/add", json={"username": "target", "password": "Password123"})
    target_id = target_resp.json()["data"]
    login_resp = await client.post("/user/login", json={"username": "viewer", "password": "Password123"})
    token = login_resp.json()["data"]

    resp = await client.get(f"/user/getById/{target_id}", headers={"token": token})

    body = resp.json()
    assert body["code"] != 0
    assert "无权" in body["message"]


# ---------------------------------------------------------------------------
# /user/update
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_update_password_refreshes_token(
    auth_client: AsyncClient, client: AsyncClient, db: AsyncSession
) -> None:
    """验证 /user/updatePassword 修改密码后 token 刷新，且能用新密码登录"""
    await auth_client.post(
        "/user/add",
        json={"username": "gina", "password": "Oldpass123", "realName": "Gina"},
    )
    login_resp = await client.post("/user/login", json={"username": "gina", "password": "Oldpass123"})
    token = login_resp.json()["data"]

    user_before = (await db.execute(select(User).where(User.username == "gina"))).scalar_one()
    old_token = user_before.token

    # 通过 /user/updatePassword 修改密码
    update_resp = await client.post(
        "/user/updatePassword",
        json={"oldPassword": "Oldpass123", "newPassword": "Newpass123"},
        headers={"token": token},
    )
    assert update_resp.json()["code"] == 0

    # Token 已变化
    user_after = (
        await db.execute(select(User).where(User.username == "gina"))
    ).scalar_one()
    await db.refresh(user_after)
    assert user_after.token != old_token

    # 用新密码能登录
    login_resp = await client.post("/user/login", json={"username": "gina", "password": "Newpass123"})
    assert login_resp.json()["code"] == 0, login_resp.json()

    # 用旧密码不能登录
    bad_login = await client.post("/user/login", json={"username": "gina", "password": "Oldpass123"})
    assert bad_login.json()["code"] == 1006


@pytest.mark.asyncio
async def test_update_user_info(auth_client: AsyncClient, client: AsyncClient) -> None:
    """验证 /user/update/{id} 可修改 username/real_name"""
    add_resp = await auth_client.post("/user/add", json={"username": "gina", "password": "Password123", "realName": "Gina"})
    user_id = add_resp.json()["data"]

    login_resp = await client.post("/user/login", json={"username": "gina", "password": "Password123"})
    token = login_resp.json()["data"]

    resp = await auth_client.post(
        f"/user/update/{user_id}",
        json={"realName": "吉娜"},
    )
    assert resp.json()["data"] is True

    get_resp = await client.get(f"/user/getById/{user_id}", headers={"token": token})
    assert get_resp.json()["data"]["realName"] == "吉娜"


@pytest.mark.asyncio
async def test_update_user_rejects_duplicate_username(auth_client: AsyncClient, db: AsyncSession) -> None:
    first_resp = await auth_client.post(
        "/user/add",
        json={"username": "rename_source", "password": "Password123"},
    )
    first_id = first_resp.json()["data"]
    await auth_client.post("/user/add", json={"username": "rename_target", "password": "Password123"})

    resp = await auth_client.post(f"/user/update/{first_id}", json={"username": "rename_target"})

    body = resp.json()
    assert body["code"] == 1004
    assert "用户已存在" in body["message"]
    user = (await db.execute(select(User).where(User.id == first_id))).scalar_one()
    assert user.username == "rename_source"


@pytest.mark.asyncio
async def test_update_user_not_exist(auth_client: AsyncClient) -> None:
    # 需要先登录获取 token，因为 update_user 现在需要鉴权
    await auth_client.post("/user/add", json={"username": " updater ", "password": "Password123"})
    resp = await auth_client.post("/user/update/999999", json={"realName": "无"})
    assert resp.json()["data"] is False


@pytest.mark.asyncio
async def test_update_user_forbidden_for_other_user(auth_client: AsyncClient, client: AsyncClient) -> None:
    """普通用户不能修改其他用户的信息"""
    # 创建用户 A
    await auth_client.post("/user/add", json={"username": "user_a", "password": "Password123", "realName": "A"})
    a_login = await client.post("/user/login", json={"username": "user_a", "password": "Password123"})
    a_token = a_login.json()["data"]

    # 创建用户 B
    b_resp = await auth_client.post("/user/add", json={"username": "user_b", "password": "Password123", "realName": "B"})
    b_id = b_resp.json()["data"]

    # A 尝试修改 B
    resp = await client.post(
        f"/user/update/{b_id}",
        json={"realName": "被改了"},
        headers={"token": a_token},
    )
    assert resp.json()["code"] != 0
    assert "无权" in resp.json()["message"]


@pytest.mark.asyncio
async def test_add_user_rejects_unknown_role_id(auth_client: AsyncClient) -> None:
    resp = await auth_client.post(
        "/user/add",
        json={"username": "bad_role_user", "password": "Password123", "roleId": 999999},
    )

    body = resp.json()
    assert body["code"] == 1002
    assert "角色不存在" in body["message"]


@pytest.mark.asyncio
async def test_update_user_rejects_unknown_role_id(auth_client: AsyncClient, db: AsyncSession) -> None:
    add_resp = await auth_client.post(
        "/user/add",
        json={"username": "role_update_user", "password": "Password123"},
    )
    user_id = add_resp.json()["data"]
    before = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    original_role_id = before.role_id

    resp = await auth_client.post(f"/user/update/{user_id}", json={"roleId": 999999})

    body = resp.json()
    assert body["code"] == 1002
    assert "角色不存在" in body["message"]
    await db.refresh(before)
    assert before.role_id == original_role_id


# ---------------------------------------------------------------------------
# /user/list
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_users_paginated_and_password_masked(auth_client: AsyncClient) -> None:
    for name in ["u1", "u2", "u3"]:
        await auth_client.post("/user/add", json={"username": name, "password": "Password123", "realName": name.upper()})

    resp = await auth_client.get("/user/list?page=1&size=10&username=u")
    body = resp.json()
    assert body["code"] == 0
    page = body["data"]
    assert page["page"] == 1
    assert page["size"] == 10
    assert page["total"] == 3
    assert len(page["list"]) == 3
    for u in page["list"]:
        assert u["password"] == "******"
        # camelCase 字段
        assert "realName" in u


@pytest.mark.asyncio
async def test_list_users_search_by_username(auth_client: AsyncClient) -> None:
    await auth_client.post("/user/add", json={"username": "search_alpha", "password": "Password123"})
    await auth_client.post("/user/add", json={"username": "search_beta", "password": "Password123"})
    await auth_client.post("/user/add", json={"username": "other", "password": "Password123"})

    resp = await auth_client.get("/user/list?page=1&size=10&username=search")
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["total"] == 2


# ---------------------------------------------------------------------------
# /user/delete
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_delete_user(auth_client: AsyncClient, client: AsyncClient) -> None:
    add_resp = await auth_client.post("/user/add", json={"username": "to_delete", "password": "Password123"})
    user_id = add_resp.json()["data"]

    del_resp = await auth_client.get(f"/user/delete/{user_id}")
    assert del_resp.json()["data"] is True

    # 再查就找不到
    get_resp = await auth_client.get(f"/user/getById/{user_id}")
    assert get_resp.json()["data"] is None


@pytest.mark.asyncio
async def test_delete_user_not_exist(auth_client: AsyncClient) -> None:
    resp = await auth_client.get("/user/delete/999999")
    assert resp.json()["data"] is False


@pytest.mark.asyncio
async def test_delete_admin_user_forbidden(auth_client: AsyncClient) -> None:
    """初始管理员（username=admin）不可删除"""
    add_resp = await auth_client.post("/user/add", json={"username": "admin", "password": "Password123", "realName": "管理员"})
    admin_id = add_resp.json()["data"]

    del_resp = await auth_client.get(f"/user/delete/{admin_id}")
    body = del_resp.json()
    assert body["code"] != 0
    assert "不可删除" in body["message"]


# ---------------------------------------------------------------------------
# 鉴权依赖（通过 /_test/whoami 触发）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_auth_dep_with_valid_token(auth_client: AsyncClient, client: AsyncClient) -> None:
    await auth_client.post("/user/add", json={"username": "henry", "password": "Password123"})
    login_resp = await client.post("/user/login", json={"username": "henry", "password": "Password123"})
    token = login_resp.json()["data"]

    resp = await client.get("/_test/whoami", headers={"token": token})
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["username"] == "henry"


@pytest.mark.asyncio
async def test_auth_dep_with_token_via_query_param(auth_client: AsyncClient, client: AsyncClient) -> None:
    """复刻 Java TokenUtils 行为：header 没有时也接受 query param"""
    await auth_client.post("/user/add", json={"username": "iris", "password": "Password123"})
    login_resp = await client.post("/user/login", json={"username": "iris", "password": "Password123"})
    token = login_resp.json()["data"]

    resp = await client.get(f"/_test/whoami?token={token}")
    assert resp.json()["code"] == 0


@pytest.mark.asyncio
async def test_auth_dep_missing_token(client: AsyncClient) -> None:
    resp = await client.get("/_test/whoami")
    body = resp.json()
    assert body["code"] == 1007  # USER_NOT_LOGIN


@pytest.mark.asyncio
async def test_auth_dep_expired_token(auth_client: AsyncClient, client: AsyncClient, db: AsyncSession) -> None:
    await auth_client.post("/user/add", json={"username": "jack", "password": "Password123"})
    login_resp = await client.post("/user/login", json={"username": "jack", "password": "Password123"})
    token = login_resp.json()["data"]

    # 把 expire_time 改到过去
    user = (await db.execute(select(User).where(User.username == "jack"))).scalar_one()
    user.expire_time = datetime.now() - timedelta(hours=1)
    await db.commit()

    resp = await client.get("/_test/whoami", headers={"token": token})
    body = resp.json()
    assert body["code"] == 1008  # USER_TOKEN_EXPIRE


@pytest.mark.asyncio
async def test_update_password_success(auth_client: AsyncClient, client: AsyncClient) -> None:
    """测试修改密码正常流程"""
    # 先创建用户
    add_resp = await auth_client.post("/user/add", json={"username": "pwd_test", "password": "Oldpass123", "realName": "Pwd"})
    assert add_resp.json()["code"] == 0

    # 登录获取 token
    login_resp = await client.post("/user/login", json={"username": "pwd_test", "password": "Oldpass123"})
    body = login_resp.json()
    assert body["code"] == 0
    token = body["data"]

    # 用 token 修改密码
    resp = await client.post(
        "/user/updatePassword",
        json={"oldPassword": "Oldpass123", "newPassword": "Newpass123"},
        headers={"token": token},
    )
    result = resp.json()
    print("update_password result:", result)
    assert result["code"] == 0, result
    assert result["data"] is True

    # 用新密码能登录
    new_login = await client.post("/user/login", json={"username": "pwd_test", "password": "Newpass123"})
    assert new_login.json()["code"] == 0


@pytest.mark.asyncio
async def test_update_password_without_token_returns_not_login(client: AsyncClient) -> None:
    """不带 token 请求修改密码应返回 USER_NOT_LOGIN (1007)，不是 USER_NOT_EXIST"""
    resp = await client.post("/user/updatePassword", json={"oldPassword": "x", "newPassword": "y"})
    body = resp.json()
    assert body["code"] == 1007, f"expected 1007, got {body}"


@pytest.mark.asyncio
async def test_update_password_with_invalid_token_returns_not_exist(client: AsyncClient) -> None:
    """带无效 token 请求修改密码应返回 USER_NOT_EXIST (1005)"""
    resp = await client.post(
        "/user/updatePassword",
        json={"oldPassword": "x", "newPassword": "y"},
        headers={"token": "invalid-token-12345"},
    )
    body = resp.json()
    assert body["code"] == 1005, f"expected 1005, got {body}"


@pytest.mark.asyncio
async def test_update_password_for_another_user_by_admin(auth_client: AsyncClient, client: AsyncClient) -> None:
    """admin 可以跳过旧密码校验，直接重置其他用户的密码"""
    # 创建普通用户
    add_resp = await auth_client.post("/user/add", json={"username": "normal_user", "password": "Oldpass123", "realName": "Normal"})
    normal_id = add_resp.json()["data"]

    # admin 重置普通用户密码（旧密码随便填）
    resp = await client.post(
        "/user/updatePassword",
        json={"id": normal_id, "oldPassword": "whatever", "newPassword": "Newpass123"},
        headers={"token": auth_client.headers["token"]},
    )
    result = resp.json()
    assert result["code"] == 0, result

    # 普通用户用新密码能登录
    new_login = await client.post("/user/login", json={"username": "normal_user", "password": "Newpass123"})
    assert new_login.json()["code"] == 0
