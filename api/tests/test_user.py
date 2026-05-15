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
from app.deps.auth import get_current_user_dep
from app.main import app
from app.models.user import User


# ---------------------------------------------------------------------------
# 给鉴权依赖测试用的"私有"路由（仅在 tests 包加载时注册一次）
# ---------------------------------------------------------------------------
async def _whoami(current: UserContext = Depends(get_current_user_dep)) -> Any:
    return success({"id": current.id, "username": current.username})


app.add_api_route("/_test/whoami", _whoami, methods=["GET"], include_in_schema=False)


# ---------------------------------------------------------------------------
# /user/add
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_add_user_success(client: AsyncClient, db: AsyncSession) -> None:
    resp = await client.post(
        "/user/add",
        json={"username": "alice", "password": "secret", "realName": "爱丽丝"},
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
    assert user.password != "secret"
    assert user.password.startswith("$2")  # bcrypt 标识前缀
    # token 已生成
    assert len(user.token) >= 32


@pytest.mark.asyncio
async def test_add_user_duplicate(client: AsyncClient) -> None:
    await client.post("/user/add", json={"username": "bob", "password": "p"})
    resp = await client.post("/user/add", json={"username": "bob", "password": "p"})
    body = resp.json()
    assert body["code"] == 1004  # USER_EXIST
    assert body["success"] is False
    assert "用户已存在" in body["message"]


@pytest.mark.asyncio
async def test_add_user_missing_param(client: AsyncClient) -> None:
    # 缺 password
    resp = await client.post("/user/add", json={"username": "carol"})
    body = resp.json()
    assert body["code"] == 1003  # PARAM_MISSING


# ---------------------------------------------------------------------------
# /user/login
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_login_success(client: AsyncClient) -> None:
    await client.post(
        "/user/add",
        json={"username": "dave", "password": "hunter2", "realName": "Dave"},
    )
    resp = await client.post("/user/login", json={"username": "dave", "password": "hunter2"})
    body = resp.json()
    assert body["code"] == 0
    token = body["data"]
    # 36 字符长度的 UUID v4
    assert isinstance(token, str) and len(token) == 36


@pytest.mark.asyncio
async def test_login_wrong_password(client: AsyncClient) -> None:
    await client.post("/user/add", json={"username": "eve", "password": "correct"})
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
async def test_get_by_id_returns_camelcase(client: AsyncClient) -> None:
    add_resp = await client.post(
        "/user/add",
        json={"username": "frank", "password": "p", "realName": "弗兰克"},
    )
    user_id = add_resp.json()["data"]

    resp = await client.get(f"/user/getById/{user_id}")
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["id"] == user_id
    assert data["username"] == "frank"
    # 关键：camelCase
    assert data["realName"] == "弗兰克"
    assert "real_name" not in data
    assert "effectTime" in data and "expireTime" in data
    assert "effect_time" not in data and "expire_time" not in data


@pytest.mark.asyncio
async def test_get_by_id_not_found(client: AsyncClient) -> None:
    resp = await client.get("/user/getById/999999")
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] is None


# ---------------------------------------------------------------------------
# /user/update
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_update_user_refreshes_token_and_rehashes_password(
    client: AsyncClient, db: AsyncSession
) -> None:
    """验证：
    1. 更新后 token 变了（Java 行为）
    2. 更新密码后能用新密码登录（修复了 Java 的不加密 bug）
    """
    add_resp = await client.post(
        "/user/add",
        json={"username": "gina", "password": "old_pw", "realName": "Gina"},
    )
    user_id = add_resp.json()["data"]

    user_before = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    old_token = user_before.token
    await db.commit()  # flush 隔离层

    # 更新密码
    update_resp = await client.post(
        f"/user/update/{user_id}",
        json={"password": "new_pw"},
    )
    assert update_resp.json()["data"] is True

    # Token 已变化
    user_after = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    await db.refresh(user_after)
    assert user_after.token != old_token

    # 用新密码能登录
    login_resp = await client.post("/user/login", json={"username": "gina", "password": "new_pw"})
    assert login_resp.json()["code"] == 0, login_resp.json()

    # 用旧密码不能登录
    bad_login = await client.post("/user/login", json={"username": "gina", "password": "old_pw"})
    assert bad_login.json()["code"] == 1006


@pytest.mark.asyncio
async def test_update_user_not_exist(client: AsyncClient) -> None:
    resp = await client.post("/user/update/999999", json={"realName": "无"})
    assert resp.json()["data"] is False


# ---------------------------------------------------------------------------
# /user/list
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_users_paginated_and_password_masked(client: AsyncClient) -> None:
    for name in ["u1", "u2", "u3"]:
        await client.post("/user/add", json={"username": name, "password": "p", "realName": name.upper()})

    resp = await client.get("/user/list?page=1&size=10")
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
async def test_list_users_search_by_username(client: AsyncClient) -> None:
    await client.post("/user/add", json={"username": "search_alpha", "password": "p"})
    await client.post("/user/add", json={"username": "search_beta", "password": "p"})
    await client.post("/user/add", json={"username": "other", "password": "p"})

    resp = await client.get("/user/list?page=1&size=10&username=search")
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["total"] == 2


# ---------------------------------------------------------------------------
# /user/delete
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_delete_user(client: AsyncClient) -> None:
    add_resp = await client.post("/user/add", json={"username": "to_delete", "password": "p"})
    user_id = add_resp.json()["data"]

    del_resp = await client.get(f"/user/delete/{user_id}")
    assert del_resp.json()["data"] is True

    # 再查就找不到
    get_resp = await client.get(f"/user/getById/{user_id}")
    assert get_resp.json()["data"] is None


@pytest.mark.asyncio
async def test_delete_user_not_exist(client: AsyncClient) -> None:
    resp = await client.get("/user/delete/999999")
    assert resp.json()["data"] is False


# ---------------------------------------------------------------------------
# 鉴权依赖（通过 /_test/whoami 触发）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_auth_dep_with_valid_token(client: AsyncClient) -> None:
    await client.post("/user/add", json={"username": "henry", "password": "p"})
    login_resp = await client.post("/user/login", json={"username": "henry", "password": "p"})
    token = login_resp.json()["data"]

    resp = await client.get("/_test/whoami", headers={"token": token})
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["username"] == "henry"


@pytest.mark.asyncio
async def test_auth_dep_with_token_via_query_param(client: AsyncClient) -> None:
    """复刻 Java TokenUtils 行为：header 没有时也接受 query param"""
    await client.post("/user/add", json={"username": "iris", "password": "p"})
    login_resp = await client.post("/user/login", json={"username": "iris", "password": "p"})
    token = login_resp.json()["data"]

    resp = await client.get(f"/_test/whoami?token={token}")
    assert resp.json()["code"] == 0


@pytest.mark.asyncio
async def test_auth_dep_missing_token(client: AsyncClient) -> None:
    resp = await client.get("/_test/whoami")
    body = resp.json()
    assert body["code"] == 1007  # USER_NOT_LOGIN


@pytest.mark.asyncio
async def test_auth_dep_expired_token(client: AsyncClient, db: AsyncSession) -> None:
    await client.post("/user/add", json={"username": "jack", "password": "p"})
    login_resp = await client.post("/user/login", json={"username": "jack", "password": "p"})
    token = login_resp.json()["data"]

    # 把 expire_time 改到过去
    user = (await db.execute(select(User).where(User.username == "jack"))).scalar_one()
    user.expire_time = datetime.now() - timedelta(hours=1)
    await db.commit()

    resp = await client.get("/_test/whoami", headers={"token": token})
    body = resp.json()
    assert body["code"] == 1008  # USER_TOKEN_EXPIRE
