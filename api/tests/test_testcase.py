"""/testcase/* CRUD 路由的集成测试（不含 debug/run/stop/syncNode/getFull/getJMeterResult）"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import TestCaseStatus
from app.models.testcase import TestCase


@pytest.mark.asyncio
async def test_testcase_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/testcase/list?page=1&size=10")
    assert resp.json()["code"] == 1007


@pytest.mark.asyncio
async def test_add_testcase_success(
    auth_client: AsyncClient, db: AsyncSession, data_home: Path
) -> None:
    resp = await auth_client.post(
        "/testcase/add",
        json={
            "name": "case_a",
            "description": "测试用例 A",
            "biz": "biz1",
            "service": "svc1",
            "version": "v1",
        },
    )
    body = resp.json()
    assert body["code"] == 0
    case_id = body["data"]

    obj = (await db.execute(select(TestCase).where(TestCase.id == case_id))).scalar_one()
    assert obj.name == "case_a"
    assert obj.status == TestCaseStatus.NOT_RUN.value
    # test_case_dir 在 data_home 下
    assert obj.test_case_dir.startswith(str(data_home))
    assert "case_a_" in obj.test_case_dir
    # 磁盘目录已创建
    assert os.path.isdir(obj.test_case_dir)


@pytest.mark.asyncio
async def test_add_testcase_name_with_space(
    auth_client: AsyncClient, data_home: Path
) -> None:
    resp = await auth_client.post("/testcase/add", json={"name": "bad name"})
    assert resp.json()["code"] == 1039  # TESTCASE_NAME_ERROR


@pytest.mark.asyncio
async def test_add_testcase_name_with_hash(
    auth_client: AsyncClient, data_home: Path
) -> None:
    resp = await auth_client.post("/testcase/add", json={"name": "bad#name"})
    assert resp.json()["code"] == 1039


@pytest.mark.asyncio
async def test_add_testcase_duplicate(
    auth_client: AsyncClient, data_home: Path
) -> None:
    await auth_client.post("/testcase/add", json={"name": "dup_case"})
    resp = await auth_client.post("/testcase/add", json={"name": "dup_case"})
    assert resp.json()["code"] == 1040  # TESTCASE_IS_EXIST


@pytest.mark.asyncio
async def test_update_testcase_success(
    auth_client: AsyncClient, db: AsyncSession, data_home: Path
) -> None:
    add_resp = await auth_client.post("/testcase/add", json={"name": "upd_case"})
    case_id = add_resp.json()["data"]
    resp = await auth_client.post(
        f"/testcase/update/{case_id}",
        json={"name": "upd_case_v2", "description": "updated"},
    )
    assert resp.json()["data"] is True
    obj = (await db.execute(select(TestCase).where(TestCase.id == case_id))).scalar_one()
    assert obj.name == "upd_case_v2"
    assert obj.description == "updated"


@pytest.mark.asyncio
async def test_update_testcase_not_exist(auth_client: AsyncClient) -> None:
    resp = await auth_client.post("/testcase/update/999999", json={"name": "x"})
    assert resp.json()["code"] == 1041  # TESTCASE_NOT_EXIST


@pytest.mark.asyncio
async def test_list_testcases_search_by_name(
    auth_client: AsyncClient, data_home: Path
) -> None:
    for name in ["case_alpha", "case_beta", "other"]:
        await auth_client.post("/testcase/add", json={"name": name})
    resp = await auth_client.get("/testcase/list?page=1&size=10&name=case")
    assert resp.json()["data"]["total"] == 2


@pytest.mark.asyncio
async def test_list_testcases_search_by_id_exact(
    auth_client: AsyncClient, data_home: Path
) -> None:
    """id 字段是精确匹配（Java 行为）"""
    add_resp = await auth_client.post("/testcase/add", json={"name": "case_id_test"})
    case_id = add_resp.json()["data"]
    resp = await auth_client.get(f"/testcase/list?page=1&size=10&id={case_id}")
    page = resp.json()["data"]
    assert page["total"] == 1
    assert page["list"][0]["id"] == case_id


@pytest.mark.asyncio
async def test_delete_testcase(
    auth_client: AsyncClient, data_home: Path
) -> None:
    add_resp = await auth_client.post("/testcase/add", json={"name": "to_del"})
    case_id = add_resp.json()["data"]
    assert (await auth_client.get(f"/testcase/delete/{case_id}")).json()["data"] is True
    assert (await auth_client.get(f"/testcase/delete/{case_id}")).json()["data"] is False


@pytest.mark.asyncio
async def test_batch_delete_testcase(
    auth_client: AsyncClient, data_home: Path
) -> None:
    ids = []
    for name in ["bd1", "bd2", "bd3"]:
        resp = await auth_client.post("/testcase/add", json={"name": name})
        ids.append(resp.json()["data"])

    resp = await auth_client.post("/testcase/batchDelete", json={"ids": ids})
    assert resp.json()["data"] is True

    # 全部不存在
    list_resp = await auth_client.get("/testcase/list?page=1&size=10")
    assert list_resp.json()["data"]["total"] == 0


@pytest.mark.asyncio
async def test_batch_delete_with_non_existing_ids(
    auth_client: AsyncClient, data_home: Path
) -> None:
    """Java 行为：循环里 delete 不存在的 id 也不抛异常"""
    add_resp = await auth_client.post("/testcase/add", json={"name": "real"})
    real_id = add_resp.json()["data"]
    resp = await auth_client.post(
        "/testcase/batchDelete", json={"ids": [real_id, 999999, 999998]}
    )
    assert resp.json()["data"] is True


# ---------------------------------------------------------------------------
# Phase 3 加：testcase 改名时级联更新 JMX/CSV/JAR 的 description
# ---------------------------------------------------------------------------


async def _setup_case_with_all_deps(
    auth_client: AsyncClient,
    name: str,
    jmx_bytes: bytes,
    csv_name: str = "data.csv",
) -> int:
    """辅助：创建用例 + 上传 JMX + CSV + JAR"""
    resp = await auth_client.post("/testcase/add", json={"name": name})
    case_id = resp.json()["data"]
    jmx_files = {"jmxFile": ("test.jmx", jmx_bytes, "application/octet-stream")}
    await auth_client.post(f"/jmx/upload/{case_id}", files=jmx_files)
    csv_files = {"csvFile": (csv_name, b"a,b\n1,2\n", "text/csv")}
    await auth_client.post(f"/csv/upload/{case_id}", files=csv_files)
    jar_files = {"jarFile": ("dep.jar", b"x", "application/java-archive")}
    await auth_client.post(f"/jar/upload/{case_id}", files=jar_files)
    return case_id


@pytest.mark.asyncio
async def test_rename_cascades_jmx_description(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
) -> None:
    from app.models.jmx import Jmx

    case_id = await _setup_case_with_all_deps(auth_client, "case_orig", sample_jmx_bytes)

    # 改名
    resp = await auth_client.post(f"/testcase/update/{case_id}", json={"name": "case_renamed"})
    assert resp.json()["data"] is True

    jmx = (await db.execute(select(Jmx).where(Jmx.test_case_id == case_id))).scalar_one()
    await db.refresh(jmx)
    assert jmx.description == "case_renamed"


@pytest.mark.asyncio
async def test_rename_cascades_csv_description(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
) -> None:
    from app.models.csv import Csv

    case_id = await _setup_case_with_all_deps(auth_client, "case_csv_orig", sample_jmx_bytes)

    await auth_client.post(f"/testcase/update/{case_id}", json={"name": "case_csv_renamed"})

    csvs = (await db.execute(select(Csv).where(Csv.test_case_id == case_id))).scalars().all()
    assert len(csvs) == 1
    await db.refresh(csvs[0])
    assert csvs[0].description == "case_csv_renamed"


@pytest.mark.asyncio
async def test_rename_cascades_jar_description(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
) -> None:
    from app.models.jar import Jar

    case_id = await _setup_case_with_all_deps(auth_client, "case_jar_orig", sample_jmx_bytes)

    await auth_client.post(f"/testcase/update/{case_id}", json={"name": "case_jar_renamed"})

    jars = (await db.execute(select(Jar).where(Jar.test_case_id == case_id))).scalars().all()
    assert len(jars) == 1
    await db.refresh(jars[0])
    assert jars[0].description == "case_jar_renamed"
