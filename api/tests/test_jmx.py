"""/jmx/* 路由的集成测试。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.jmx import Jmx

UPLOAD_FILE_JMX = b"""<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan>
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true"/>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="Thread Group" enabled="true">
        <stringProp name="ThreadGroup.num_threads">1</stringProp>
        <stringProp name="ThreadGroup.ramp_time">1</stringProp>
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController">
          <stringProp name="LoopController.loops">1</stringProp>
        </elementProp>
      </ThreadGroup>
      <hashTree>
        <HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="upload" enabled="true">
          <elementProp name="HTTPsampler.Files" elementType="HTTPFileArgs">
            <collectionProp name="HTTPFileArgs.files">
              <elementProp name="avatar.jpg" elementType="HTTPFileArg">
                <stringProp name="File.path">avatar.jpg</stringProp>
                <stringProp name="File.paramname">file</stringProp>
                <stringProp name="File.mimetype">image/jpeg</stringProp>
              </elementProp>
            </collectionProp>
          </elementProp>
        </HTTPSamplerProxy>
        <hashTree/>
      </hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
"""


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


@pytest.mark.asyncio
async def test_delete_jmx_blocked_by_public_csv_binding_until_unbound(
    auth_client: AsyncClient,
    data_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
) -> None:
    case_id = await _create_testcase(auth_client, name="case_block_public_csv")
    await auth_client.post(
        f"/jmx/upload/{case_id}",
        files={"jmxFile": ("test.jmx", sample_jmx_bytes, "application/octet-stream")},
    )
    await auth_client.post(
        "/csv/resource/upload",
        files={"csvFile": ("data.csv", b"a,b\n1,2\n", "text/csv")},
    )
    bind = await auth_client.post(
        "/csv/binding/add",
        json={"testCaseId": case_id, "filename": "data.csv", "distributionStrategy": "shared"},
    )
    assert bind.json()["code"] == 0
    full = await auth_client.get(f"/testcase/getFull/{case_id}")
    binding_id = full.json()["data"]["csvBindingVOList"][0]["id"]

    obj = (await db.execute(select(Jmx).where(Jmx.test_case_id == case_id))).scalar_one()
    blocked = await auth_client.get(f"/jmx/delete/{obj.id}")
    assert blocked.json()["code"] == 1037
    assert "先解绑" in blocked.json()["message"]

    unbind = await auth_client.get(f"/csv/binding/delete/{binding_id}")
    assert unbind.json()["code"] == 0
    deleted = await auth_client.get(f"/jmx/delete/{obj.id}")
    assert deleted.json()["code"] == 0
    assert not os.path.exists(obj.jmx_dir)


@pytest.mark.asyncio
async def test_delete_jmx_blocked_by_public_upload_file_binding_until_unbound(
    auth_client: AsyncClient,
    data_home: Path,
    db: AsyncSession,
) -> None:
    case_id = await _create_testcase(auth_client, name="case_block_public_upload")
    await auth_client.post(
        f"/jmx/upload/{case_id}",
        files={"jmxFile": ("upload.jmx", UPLOAD_FILE_JMX, "application/octet-stream")},
    )
    await auth_client.post(
        "/csv/resource/upload",
        files={"csvFile": ("avatar.jpg", b"image", "image/jpeg")},
    )
    bind = await auth_client.post(
        "/uploadFile/binding/add",
        json={"testCaseId": case_id, "filename": "avatar.jpg"},
    )
    assert bind.json()["code"] == 0
    full = await auth_client.get(f"/testcase/getFull/{case_id}")
    binding_id = full.json()["data"]["uploadFileBindingVOList"][0]["id"]

    obj = (await db.execute(select(Jmx).where(Jmx.test_case_id == case_id))).scalar_one()
    blocked = await auth_client.get(f"/jmx/delete/{obj.id}")
    assert blocked.json()["code"] == 1037
    assert "先解绑" in blocked.json()["message"]

    unbind = await auth_client.get(f"/uploadFile/binding/delete/{binding_id}")
    assert unbind.json()["code"] == 0
    deleted = await auth_client.get(f"/jmx/delete/{obj.id}")
    assert deleted.json()["code"] == 0
    assert not os.path.exists(obj.jmx_dir)
