"""/config/* 路由的集成测试"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.config import Config


@pytest.mark.asyncio
async def test_config_requires_auth(client: AsyncClient) -> None:
    """没 token 时所有 /config/* 都应返回 1007 USER_NOT_LOGIN"""
    resp = await client.post("/config/add", json={"configKey": "K", "configValue": "V", "description": "D"})
    assert resp.json()["code"] == 1007


@pytest.mark.asyncio
async def test_add_config_success(auth_client: AsyncClient, db: AsyncSession) -> None:
    resp = await auth_client.post(
        "/config/add",
        json={"configKey": "FOO", "configValue": "bar", "description": "测试"},
    )
    body = resp.json()
    assert body["code"] == 0
    new_id = body["data"]
    assert isinstance(new_id, int)

    obj = (await db.execute(select(Config).where(Config.id == new_id))).scalar_one()
    assert obj.config_key == "FOO"
    assert obj.config_value == "bar"
    assert obj.description == "测试"
    # 审计字段
    assert obj.creator == "测试管理员"
    assert obj.modifier == "测试管理员"


@pytest.mark.asyncio
async def test_add_config_duplicate_key(auth_client: AsyncClient) -> None:
    await auth_client.post(
        "/config/add", json={"configKey": "DUP", "configValue": "v1", "description": "d"}
    )
    resp = await auth_client.post(
        "/config/add", json={"configKey": "DUP", "configValue": "v2", "description": "d"}
    )
    body = resp.json()
    assert body["code"] == 1011  # CONFIG_EXIST


@pytest.mark.asyncio
async def test_add_config_missing_param(auth_client: AsyncClient) -> None:
    resp = await auth_client.post(
        "/config/add", json={"configKey": "K", "configValue": ""}  # description 缺失，value 空
    )
    body = resp.json()
    assert body["code"] == 1003  # PARAM_MISSING


@pytest.mark.asyncio
async def test_update_config_success(auth_client: AsyncClient, db: AsyncSession) -> None:
    add_resp = await auth_client.post(
        "/config/add", json={"configKey": "K1", "configValue": "v1", "description": "d1"}
    )
    cfg_id = add_resp.json()["data"]
    resp = await auth_client.post(
        f"/config/update/{cfg_id}", json={"configValue": "v2"}
    )
    assert resp.json()["data"] is True

    obj = (await db.execute(select(Config).where(Config.id == cfg_id))).scalar_one()
    assert obj.config_value == "v2"
    # 未传的字段保持原值
    assert obj.config_key == "K1"


@pytest.mark.asyncio
async def test_update_config_not_exist(auth_client: AsyncClient) -> None:
    resp = await auth_client.post("/config/update/999999", json={"configValue": "x"})
    assert resp.json()["data"] is False


@pytest.mark.asyncio
async def test_list_config_search(auth_client: AsyncClient) -> None:
    for key in ["alpha_x", "alpha_y", "beta_z"]:
        await auth_client.post(
            "/config/add", json={"configKey": key, "configValue": "v", "description": "d"}
        )
    resp = await auth_client.get("/config/list?page=1&size=10&configKey=alpha")
    page = resp.json()["data"]
    assert page["total"] == 2
    assert len(page["list"]) == 2
    # camelCase 字段
    assert "configKey" in page["list"][0]
    assert "createTime" in page["list"][0]


@pytest.mark.asyncio
async def test_delete_config(auth_client: AsyncClient) -> None:
    add_resp = await auth_client.post(
        "/config/add", json={"configKey": "DEL", "configValue": "v", "description": "d"}
    )
    cfg_id = add_resp.json()["data"]
    assert (await auth_client.get(f"/config/delete/{cfg_id}")).json()["data"] is True
    # 二次删返回 false
    assert (await auth_client.get(f"/config/delete/{cfg_id}")).json()["data"] is False
