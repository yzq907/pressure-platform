"""/config/* 路由的集成测试"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.config import Config
from app.services.config import ensure_default_configs


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
async def test_config_categories(auth_client: AsyncClient) -> None:
    resp = await auth_client.get("/config/categories")
    body = resp.json()
    assert body["code"] == 0
    categories = body["data"]
    keys = [item["key"] for item in categories]
    assert keys[:3] == ["business", "jmeter", "grafana"]
    assert "retention" in keys
    assert categories[0]["name"] == "业务选项"


@pytest.mark.asyncio
async def test_list_config_by_category_adds_metadata(auth_client: AsyncClient) -> None:
    await auth_client.post(
        "/config/add",
        json={"configKey": "MASTER_JMETER_BIN_HOME", "configValue": "/opt/jmeter/bin", "description": "bin"},
    )
    await auth_client.post(
        "/config/add",
        json={"configKey": "GRAFANA_DASHBOARD_URL", "configValue": "http://grafana/d/abc", "description": "grafana"},
    )
    await auth_client.post(
        "/config/add",
        json={"configKey": "CUSTOM_UNKNOWN", "configValue": "x", "description": "custom"},
    )

    resp = await auth_client.get("/config/list?page=1&size=10&category=jmeter")
    page = resp.json()["data"]
    assert page["total"] == 1
    item = page["list"][0]
    assert item["configKey"] == "MASTER_JMETER_BIN_HOME"
    assert item["category"] == "jmeter"
    assert item["categoryName"] == "JMeter"
    assert item["displayName"] == "Master JMeter执行目录"
    assert item["valueType"] == "path"

    other_resp = await auth_client.get("/config/list?page=1&size=10&category=other")
    other_page = other_resp.json()["data"]
    assert other_page["total"] == 1
    assert other_page["list"][0]["configKey"] == "CUSTOM_UNKNOWN"
    assert other_page["list"][0]["category"] == "other"


@pytest.mark.asyncio
async def test_ensure_default_configs_adds_prometheus_step_to_config_list(
    auth_client: AsyncClient,
    db: AsyncSession,
) -> None:
    await ensure_default_configs(db)

    resp = await auth_client.get("/config/list?page=1&size=50&category=prometheus")
    page = resp.json()["data"]
    item = next(
        config for config in page["list"] if config["configKey"] == "PROMETHEUS_STEP_SECONDS"
    )

    assert item["configValue"] == "30"
    assert item["description"] == "Prometheus默认查询步长秒"
    assert item["category"] == "prometheus"
    assert item["displayName"] == "Prometheus默认查询步长秒"
    assert item["valueType"] == "number"

    resp = await auth_client.get("/config/list?page=1&size=50&category=report")
    page = resp.json()["data"]
    item = next(
        config for config in page["list"] if config["configKey"] == "REPORT_RUNNING_METRIC_REFRESH_SECONDS"
    )

    assert item["configValue"] == "30"
    assert item["description"] == "运行中报告指标刷新间隔秒"
    assert item["category"] == "report"
    assert item["displayName"] == "运行中报告指标刷新间隔秒"
    assert item["valueType"] == "number"


@pytest.mark.asyncio
async def test_ensure_default_configs_does_not_overwrite_existing_value(
    db: AsyncSession,
) -> None:
    db.add(
        Config(
            config_key="PROMETHEUS_STEP_SECONDS",
            config_value="45",
            description="自定义步长",
        )
    )
    await db.commit()

    await ensure_default_configs(db)

    rows = (
        await db.execute(
            select(Config).where(Config.config_key == "PROMETHEUS_STEP_SECONDS")
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].config_value == "45"
    assert rows[0].description == "自定义步长"


@pytest.mark.asyncio
async def test_delete_config(auth_client: AsyncClient) -> None:
    add_resp = await auth_client.post(
        "/config/add", json={"configKey": "DEL", "configValue": "v", "description": "d"}
    )
    cfg_id = add_resp.json()["data"]
    assert (await auth_client.get(f"/config/delete/{cfg_id}")).json()["data"] is True
    # 二次删返回 false
    assert (await auth_client.get(f"/config/delete/{cfg_id}")).json()["data"] is False
