"""验证 /health 接口和统一返回格式"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_returns_unified_success(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200

    body = resp.json()
    # 统一返回格式必须有的 5 个字段
    assert body["code"] == 0
    assert body["message"] == "操作成功"
    assert body["success"] is True
    # 关键：必须是驼峰 currentTime，不能是 snake_case
    assert "currentTime" in body
    assert "current_time" not in body

    # data 字段结构
    data = body["data"]
    assert data["db"] == "ok"
    assert data["redis"] == "ok"
    assert data["version"] == "0.1.0"
    assert isinstance(data["uptime_seconds"], int)


@pytest.mark.asyncio
async def test_404_returns_unified_format(client: AsyncClient) -> None:
    """访问不存在的路径，也必须返回统一格式而非 FastAPI 默认 404 JSON"""
    resp = await client.get("/foo")
    assert resp.status_code == 200  # HTTP 永远 200
    body = resp.json()
    assert body["code"] != 0
    assert body["success"] is False
    assert "currentTime" in body


@pytest.mark.asyncio
async def test_openapi_json_at_v2_api_docs(client: AsyncClient) -> None:
    """Swagger JSON 路径必须是 /v2/api-docs（与 Java 端兼容）"""
    resp = await client.get("/v2/api-docs")
    assert resp.status_code == 200
    spec = resp.json()
    assert spec["info"]["title"].startswith("Mysterious")


@pytest.mark.asyncio
async def test_swagger_ui_path(client: AsyncClient) -> None:
    """Swagger UI 路径必须是 /swagger-ui.html"""
    resp = await client.get("/swagger-ui.html")
    assert resp.status_code == 200
    assert "swagger" in resp.text.lower() or "openapi" in resp.text.lower()


@pytest.mark.asyncio
async def test_cors_preflight(client: AsyncClient) -> None:
    """跨域预检：OPTIONS /health 应允许跨域"""
    resp = await client.options(
        "/health",
        headers={
            "Origin": "http://localhost:1234",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 200
    assert "access-control-allow-origin" in {k.lower() for k in resp.headers.keys()}
