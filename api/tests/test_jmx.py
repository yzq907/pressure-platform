"""/jmx/* 路由的集成测试。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.jmx import Jmx


async def _create_testcase(auth_client: AsyncClient, name: str = "case_a") -> int:
    resp = await auth_client.post("/testcase/add", json={"name": name})
    return resp.json()["data"]


@pytest.mark.asyncio
async def test_jmx_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/jmx/list")
    assert resp.json()["code"] == 1007


@pytest.mark.asyncio
async def test_upload_jmx_success(
    auth_client: AsyncClient,
    db: AsyncSession,
    data_home: Path,
    sample_jmx_bytes: bytes,
) -> None:
    case_id = await _create_testcase(auth_client)
    files = {"jmxFile": ("test.jmx", sample_jmx_bytes, "application/octet-stream")}
    resp = await auth_client.post(f"/jmx/upload/{case_id}", files=files)
    assert resp.json()["code"] == 0

    obj = (await db.execute(select(Jmx).where(Jmx.test_case_id == case_id))).scalar_one()
    assert obj.src_name == "test.jmx"
    # 磁盘有原文件 + debug 副本
    assert os.path.exists(obj.jmx_dir + "test.jmx")
    assert os.path.exists(obj.jmx_dir + "debug_test.jmx")


@pytest.mark.asyncio
async def test_upload_jmx_duplicate(
    auth_client: AsyncClient,
    data_home: Path,
    sample_jmx_bytes: bytes,
) -> None:
    case_id = await _create_testcase(auth_client)
    files = {"jmxFile": ("a.jmx", sample_jmx_bytes, "application/octet-stream")}
    await auth_client.post(f"/jmx/upload/{case_id}", files=files)
    # 第二次：用例已经有 JMX
    files2 = {"jmxFile": ("b.jmx", sample_jmx_bytes, "application/octet-stream")}
    resp = await auth_client.post(f"/jmx/upload/{case_id}", files=files2)
    assert resp.json()["code"] == 1034  # TESTCASE_HAS_JMX


@pytest.mark.asyncio
async def test_upload_jmx_bad_extension(
    auth_client: AsyncClient,
    data_home: Path,
    sample_jmx_bytes: bytes,
) -> None:
    case_id = await _create_testcase(auth_client)
    files = {"jmxFile": ("not_a_jmx.txt", sample_jmx_bytes, "application/octet-stream")}
    resp = await auth_client.post(f"/jmx/upload/{case_id}", files=files)
    assert resp.json()["code"] == 1035  # JMX_NAME_ERROR


@pytest.mark.asyncio
async def test_upload_jmx_with_space_in_name(
    auth_client: AsyncClient,
    data_home: Path,
    sample_jmx_bytes: bytes,
) -> None:
    case_id = await _create_testcase(auth_client)
    files = {"jmxFile": ("bad name.jmx", sample_jmx_bytes, "application/octet-stream")}
    resp = await auth_client.post(f"/jmx/upload/{case_id}", files=files)
    assert resp.json()["code"] == 1035


@pytest.mark.asyncio
async def test_upload_jmx_testcase_not_exist(
    auth_client: AsyncClient,
    data_home: Path,
    sample_jmx_bytes: bytes,
) -> None:
    files = {"jmxFile": ("test.jmx", sample_jmx_bytes, "application/octet-stream")}
    resp = await auth_client.post("/jmx/upload/999999", files=files)
    assert resp.json()["code"] == 1041  # TESTCASE_NOT_EXIST


@pytest.mark.asyncio
async def test_list_jmx(
    auth_client: AsyncClient,
    data_home: Path,
    sample_jmx_bytes: bytes,
) -> None:
    case_id = await _create_testcase(auth_client, name="case_list")
    files = {"jmxFile": ("test.jmx", sample_jmx_bytes, "application/octet-stream")}
    await auth_client.post(f"/jmx/upload/{case_id}", files=files)

    resp = await auth_client.get("/jmx/list?page=1&size=10")
    page = resp.json()["data"]
    assert page["total"] == 1
    assert page["list"][0]["srcName"] == "test.jmx"


@pytest.mark.asyncio
async def test_delete_jmx_success(
    auth_client: AsyncClient,
    data_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
) -> None:
    case_id = await _create_testcase(auth_client, name="case_del")
    files = {"jmxFile": ("test.jmx", sample_jmx_bytes, "application/octet-stream")}
    await auth_client.post(f"/jmx/upload/{case_id}", files=files)
    obj = (await db.execute(select(Jmx).where(Jmx.test_case_id == case_id))).scalar_one()

    resp = await auth_client.get(f"/jmx/delete/{obj.id}")
    assert resp.json()["data"] is True
    # jmx 目录被删
    assert not os.path.exists(obj.jmx_dir)


@pytest.mark.asyncio
async def test_download_jmx(
    auth_client: AsyncClient,
    data_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
) -> None:
    case_id = await _create_testcase(auth_client, name="case_dl")
    files = {"jmxFile": ("test.jmx", sample_jmx_bytes, "application/octet-stream")}
    await auth_client.post(f"/jmx/upload/{case_id}", files=files)
    obj = (await db.execute(select(Jmx).where(Jmx.test_case_id == case_id))).scalar_one()

    resp = await auth_client.get(f"/jmx/download/{obj.id}")
    assert resp.status_code == 200
    assert resp.content == sample_jmx_bytes
    assert "attachment" in resp.headers.get("content-disposition", "").lower()


@pytest.mark.asyncio
async def test_delete_jmx_blocked_by_csv(
    auth_client: AsyncClient,
    data_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
) -> None:
    case_id = await _create_testcase(auth_client, name="case_block_csv")
    files = {"jmxFile": ("test.jmx", sample_jmx_bytes, "application/octet-stream")}
    await auth_client.post(f"/jmx/upload/{case_id}", files=files)
    # 再传一个 CSV
    csv_files = {"csvFile": ("data.csv", b"a,b\n1,2\n", "text/csv")}
    await auth_client.post(f"/csv/upload/{case_id}", files=csv_files)
    obj = (await db.execute(select(Jmx).where(Jmx.test_case_id == case_id))).scalar_one()

    resp = await auth_client.get(f"/jmx/delete/{obj.id}")
    assert resp.json()["code"] == 1037  # JMX_HAS_CSV


@pytest.mark.asyncio
async def test_delete_jmx_blocked_by_jar(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
) -> None:
    case_id = await _create_testcase(auth_client, name="case_block_jar")
    files = {"jmxFile": ("test.jmx", sample_jmx_bytes, "application/octet-stream")}
    await auth_client.post(f"/jmx/upload/{case_id}", files=files)
    jar_files = {"jarFile": ("dep.jar", b"x", "application/java-archive")}
    await auth_client.post(f"/jar/upload/{case_id}", files=jar_files)
    obj = (await db.execute(select(Jmx).where(Jmx.test_case_id == case_id))).scalar_one()

    resp = await auth_client.get(f"/jmx/delete/{obj.id}")
    assert resp.json()["code"] == 1036  # JMX_HAS_JAR
