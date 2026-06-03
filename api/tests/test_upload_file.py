"""上传接口文件资源的集成测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient
from lxml import etree


UPLOAD_JMX = b"""<?xml version="1.0" encoding="UTF-8"?>
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


async def _create_testcase_with_upload_jmx(auth_client: AsyncClient, name: str) -> int:
    resp = await auth_client.post("/testcase/add", json={"name": name})
    case_id = resp.json()["data"]
    await auth_client.post(
        f"/jmx/upload/{case_id}",
        files={"jmxFile": ("upload.jmx", UPLOAD_JMX, "application/octet-stream")},
    )
    return case_id


@pytest.mark.asyncio
async def test_upload_file_requires_jmx(auth_client: AsyncClient, data_home: Path) -> None:
    resp = await auth_client.post("/testcase/add", json={"name": "upload_no_jmx"})
    case_id = resp.json()["data"]

    resp = await auth_client.post(
        f"/uploadFile/upload/{case_id}",
        files={"uploadFile": ("avatar.jpg", b"img", "image/jpeg")},
    )

    assert resp.json()["code"] == 1014


@pytest.mark.asyncio
async def test_upload_file_must_be_referenced_by_jmx(auth_client: AsyncClient, data_home: Path) -> None:
    case_id = await _create_testcase_with_upload_jmx(auth_client, "upload_missing_ref")

    resp = await auth_client.post(
        f"/uploadFile/upload/{case_id}",
        files={"uploadFile": ("other.jpg", b"img", "image/jpeg")},
    )

    assert resp.json()["code"] == -1
    assert "未引用上传文件" in resp.json()["message"]


@pytest.mark.asyncio
async def test_upload_file_success_modifies_jmx_and_get_full(
    auth_client: AsyncClient,
    data_home: Path,
) -> None:
    case_id = await _create_testcase_with_upload_jmx(auth_client, "upload_ok")

    resp = await auth_client.post(
        f"/uploadFile/upload/{case_id}",
        files={"uploadFile": ("avatar.jpg", b"img", "image/jpeg")},
    )

    assert resp.json()["code"] == 0
    files = list(data_home.glob("upload_ok_*/upload/avatar.jpg"))
    assert len(files) == 1
    assert files[0].read_bytes() == b"img"

    jmxs = list(data_home.glob("upload_ok_*/jmx/upload.jmx"))
    assert len(jmxs) == 1
    tree = etree.parse(str(jmxs[0]))
    values = [el.text for el in tree.iter() if el.get("name") == "File.path"]
    assert values == [str(files[0])]

    full = await auth_client.get(f"/testcase/getFull/{case_id}")
    upload_files = full.json()["data"]["uploadFileVOList"]
    assert len(upload_files) == 1
    assert upload_files[0]["srcName"] == "avatar.jpg"


@pytest.mark.asyncio
async def test_upload_file_duplicate(auth_client: AsyncClient, data_home: Path) -> None:
    case_id = await _create_testcase_with_upload_jmx(auth_client, "upload_dup")

    await auth_client.post(
        f"/uploadFile/upload/{case_id}",
        files={"uploadFile": ("avatar.jpg", b"img", "image/jpeg")},
    )
    resp = await auth_client.post(
        f"/uploadFile/upload/{case_id}",
        files={"uploadFile": ("avatar.jpg", b"img", "image/jpeg")},
    )

    assert resp.json()["code"] == 1023
    assert "已存在" in resp.json()["message"]


@pytest.mark.asyncio
async def test_upload_file_description_follows_testcase_rename(
    auth_client: AsyncClient,
    data_home: Path,
) -> None:
    case_id = await _create_testcase_with_upload_jmx(auth_client, "upload_rename")
    await auth_client.post(
        f"/uploadFile/upload/{case_id}",
        files={"uploadFile": ("avatar.jpg", b"img", "image/jpeg")},
    )

    resp = await auth_client.post(f"/testcase/update/{case_id}", json={"name": "upload_renamed"})
    assert resp.json()["code"] == 0

    full = await auth_client.get(f"/testcase/getFull/{case_id}")
    assert full.json()["data"]["uploadFileVOList"][0]["description"] == "upload_renamed"
