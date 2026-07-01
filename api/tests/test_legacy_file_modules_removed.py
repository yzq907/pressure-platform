"""旧本地 CSV/上传文件模块移除回归测试。"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.db.base import Base


@pytest.mark.asyncio
async def test_legacy_local_file_routes_are_removed(auth_client: AsyncClient) -> None:
    legacy_routes = [
        ("get", "/csv/list?page=1&size=10"),
        ("post", "/csv/upload/1"),
        ("get", "/csv/delete/1"),
        ("get", "/csv/view/1"),
        ("get", "/csv/download/1"),
        ("get", "/csv/getByTestCaseId?testCaseId=1"),
        ("get", "/uploadFile/list?page=1&size=10"),
        ("post", "/uploadFile/upload/1"),
        ("get", "/uploadFile/delete/1"),
        ("get", "/uploadFile/download/1"),
        ("get", "/uploadFile/getByTestCaseId?testCaseId=1"),
    ]

    for method, url in legacy_routes:
        resp = await getattr(auth_client, method)(url)
        body = resp.json()
        assert body["code"] == -1, url
        assert "HTTP 404" in body["message"], url


def test_legacy_local_file_tables_are_not_registered() -> None:
    assert "mysterious_csv" not in Base.metadata.tables
    assert "mysterious_upload_file" not in Base.metadata.tables
