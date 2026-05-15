"""/jar/* 路由的集成测试。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from httpx import AsyncClient
from lxml import etree


async def _create_testcase_with_jmx(
    auth_client: AsyncClient,
    name: str,
    jmx_bytes: bytes,
) -> int:
    resp = await auth_client.post("/testcase/add", json={"name": name})
    case_id = resp.json()["data"]
    files = {"jmxFile": ("test.jmx", jmx_bytes, "application/octet-stream")}
    await auth_client.post(f"/jmx/upload/{case_id}", files=files)
    return case_id


@pytest.mark.asyncio
async def test_jar_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/jar/list")
    assert resp.json()["code"] == 1007


@pytest.mark.asyncio
async def test_upload_jar_without_jmx(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_home: Path,
) -> None:
    resp = await auth_client.post("/testcase/add", json={"name": "j_no_jmx"})
    case_id = resp.json()["data"]

    files = {"jarFile": ("dep.jar", b"fakejar", "application/java-archive")}
    resp = await auth_client.post(f"/jar/upload/{case_id}", files=files)
    assert resp.json()["code"] == 1014  # JMX_NOT_EXIST


@pytest.mark.asyncio
async def test_upload_jar_bad_extension(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_home: Path,
    sample_jmx_bytes: bytes,
) -> None:
    case_id = await _create_testcase_with_jmx(auth_client, "j1", sample_jmx_bytes)
    files = {"jarFile": ("notjar.txt", b"x", "text/plain")}
    resp = await auth_client.post(f"/jar/upload/{case_id}", files=files)
    assert resp.json()["code"] == 1031  # JAR_NAME_ERROR


@pytest.mark.asyncio
async def test_upload_normal_jar(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_home: Path,
    sample_jmx_bytes: bytes,
) -> None:
    """普通 JAR：存到 {testcaseDir}/jar/，JMX TestPlan classpath 被更新"""
    case_id = await _create_testcase_with_jmx(auth_client, "j2", sample_jmx_bytes)
    files = {"jarFile": ("mylib.jar", b"FAKEJARBINARY", "application/java-archive")}
    resp = await auth_client.post(f"/jar/upload/{case_id}", files=files)
    assert resp.json()["code"] == 0

    # JAR 落盘在用例的 jar 子目录
    jars = list(data_home.glob("j2_*/jar/mylib.jar"))
    assert len(jars) == 1
    assert jars[0].read_bytes() == b"FAKEJARBINARY"

    # JMX 里 classpath 被改成用例 jar 目录
    jmxs = list(data_home.glob("j2_*/jmx/test.jmx"))
    tree = etree.parse(str(jmxs[0]))
    cps = []
    for tp in tree.iter("TestPlan"):
        for prop in tp.iter():
            if prop.get("name") == "TestPlan.user_define_classpath":
                cps.append(prop.text)
    assert len(cps) == 1
    expected_dir = str(jars[0].parent)  # /tmp/.../j2_xxx/jar
    assert cps[0] == expected_dir


@pytest.mark.asyncio
async def test_upload_plugin_jar(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_home: Path,
    sample_jmx_bytes: bytes,
) -> None:
    """jmeter-plugins-*.jar 应放到 MASTER_JMETER_HOME/lib/ext/"""
    case_id = await _create_testcase_with_jmx(auth_client, "j3", sample_jmx_bytes)
    files = {
        "jarFile": ("jmeter-plugins-cmd.jar", b"PLUGINJARBINARY", "application/java-archive")
    }
    resp = await auth_client.post(f"/jar/upload/{case_id}", files=files)
    assert resp.json()["code"] == 0

    # 文件落盘在 jmeter_home/lib/ext/
    plugin_file = jmeter_home / "lib" / "ext" / "jmeter-plugins-cmd.jar"
    assert plugin_file.exists()
    assert plugin_file.read_bytes() == b"PLUGINJARBINARY"


@pytest.mark.asyncio
async def test_upload_jar_duplicate(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_home: Path,
    sample_jmx_bytes: bytes,
) -> None:
    case_id = await _create_testcase_with_jmx(auth_client, "j4", sample_jmx_bytes)
    files = {"jarFile": ("dup.jar", b"x", "application/java-archive")}
    await auth_client.post(f"/jar/upload/{case_id}", files=files)
    resp = await auth_client.post(f"/jar/upload/{case_id}", files=files)
    assert resp.json()["code"] == 1032  # JAR_IS_EXIST


@pytest.mark.asyncio
async def test_delete_normal_jar(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_home: Path,
    sample_jmx_bytes: bytes,
    db,
) -> None:
    from sqlalchemy import select

    from app.models.jar import Jar

    case_id = await _create_testcase_with_jmx(auth_client, "j5", sample_jmx_bytes)
    files = {"jarFile": ("normal.jar", b"x", "application/java-archive")}
    await auth_client.post(f"/jar/upload/{case_id}", files=files)
    obj = (await db.execute(select(Jar).where(Jar.test_case_id == case_id))).scalar_one()
    filepath = obj.jar_dir + obj.dst_name

    resp = await auth_client.get(f"/jar/delete/{obj.id}")
    assert resp.json()["data"] is True
    assert not os.path.exists(filepath)


@pytest.mark.asyncio
async def test_delete_plugin_jar_keeps_file(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_home: Path,
    sample_jmx_bytes: bytes,
    db,
) -> None:
    """插件 JAR 删除时磁盘文件保留"""
    from sqlalchemy import select

    from app.models.jar import Jar

    case_id = await _create_testcase_with_jmx(auth_client, "j6", sample_jmx_bytes)
    files = {"jarFile": ("jmeter-plugins-keepme.jar", b"x", "application/java-archive")}
    await auth_client.post(f"/jar/upload/{case_id}", files=files)
    obj = (await db.execute(select(Jar).where(Jar.test_case_id == case_id))).scalar_one()
    filepath = obj.jar_dir + obj.dst_name

    resp = await auth_client.get(f"/jar/delete/{obj.id}")
    assert resp.json()["data"] is True
    # 磁盘文件保留
    assert os.path.exists(filepath)


@pytest.mark.asyncio
async def test_get_jar_by_testcase_id(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_home: Path,
    sample_jmx_bytes: bytes,
) -> None:
    case_id = await _create_testcase_with_jmx(auth_client, "j7", sample_jmx_bytes)
    files = {"jarFile": ("dep.jar", b"x", "application/java-archive")}
    await auth_client.post(f"/jar/upload/{case_id}", files=files)

    resp = await auth_client.get(f"/jar/getByTestCaseId?testCaseId={case_id}")
    items = resp.json()["data"]
    assert len(items) == 1
    assert items[0]["srcName"] == "dep.jar"
