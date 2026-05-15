"""/csv/* 路由的集成测试。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from httpx import AsyncClient
from lxml import etree

from app.core import jmeter_xml


async def _create_testcase_with_jmx(
    auth_client: AsyncClient,
    name: str,
    jmx_bytes: bytes,
) -> int:
    """辅助：建一个用例 + 上传 JMX，返回 testcase_id"""
    resp = await auth_client.post("/testcase/add", json={"name": name})
    case_id = resp.json()["data"]
    files = {"jmxFile": ("test.jmx", jmx_bytes, "application/octet-stream")}
    await auth_client.post(f"/jmx/upload/{case_id}", files=files)
    return case_id


@pytest.mark.asyncio
async def test_csv_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/csv/list")
    assert resp.json()["code"] == 1007


@pytest.mark.asyncio
async def test_upload_csv_without_jmx(
    auth_client: AsyncClient,
    data_home: Path,
) -> None:
    """用例没 JMX 时直接上传 CSV 应失败"""
    resp = await auth_client.post("/testcase/add", json={"name": "no_jmx_case"})
    case_id = resp.json()["data"]

    files = {"csvFile": ("data.csv", b"a,b,c\n1,2,3\n", "text/csv")}
    resp = await auth_client.post(f"/csv/upload/{case_id}", files=files)
    assert resp.json()["code"] == 1014  # JMX_NOT_EXIST


@pytest.mark.asyncio
async def test_upload_csv_bad_extension(
    auth_client: AsyncClient,
    data_home: Path,
    sample_jmx_bytes: bytes,
) -> None:
    case_id = await _create_testcase_with_jmx(auth_client, "c1", sample_jmx_bytes)
    files = {"csvFile": ("notcsv.txt", b"data", "text/plain")}
    resp = await auth_client.post(f"/csv/upload/{case_id}", files=files)
    assert resp.json()["code"] == 1015  # CSV_NAME_ERROR


@pytest.mark.asyncio
async def test_upload_csv_not_in_jmx(
    auth_client: AsyncClient,
    data_home: Path,
    sample_jmx_bytes: bytes,
) -> None:
    """sample.jmx 里只有 testname=data.csv 的 CSVDataSet。上传别的名字应失败"""
    case_id = await _create_testcase_with_jmx(auth_client, "c2", sample_jmx_bytes)
    files = {"csvFile": ("other.csv", b"a,b,c\n", "text/csv")}
    resp = await auth_client.post(f"/csv/upload/{case_id}", files=files)
    assert resp.json()["code"] == 1015  # CSV_NAME_ERROR


@pytest.mark.asyncio
async def test_upload_csv_success_modifies_jmx(
    auth_client: AsyncClient,
    data_home: Path,
    sample_jmx_bytes: bytes,
) -> None:
    case_id = await _create_testcase_with_jmx(auth_client, "c3", sample_jmx_bytes)
    csv_content = b"user,pass\nalice,123\n"
    files = {"csvFile": ("data.csv", csv_content, "text/csv")}
    resp = await auth_client.post(f"/csv/upload/{case_id}", files=files)
    assert resp.json()["code"] == 0

    # 验证 CSV 文件落盘
    csv_path = data_home / f"c3_*" / "csv" / "data.csv"
    # 用 glob 找
    csvs = list(data_home.glob("c3_*/csv/data.csv"))
    assert len(csvs) == 1
    assert csvs[0].read_bytes() == csv_content

    # 验证 JMX 里 CSV 节点的 filename 已经被改写
    jmxs = list(data_home.glob("c3_*/jmx/test.jmx"))
    assert len(jmxs) == 1
    tree = etree.parse(str(jmxs[0]))
    for el in tree.iter("CSVDataSet"):
        if el.get("testname") == "data.csv":
            for prop in el.iter():
                if prop.get("name") == "filename":
                    assert prop.text == str(csvs[0])
                    return
    pytest.fail("CSVDataSet/filename 节点未被找到")


@pytest.mark.asyncio
async def test_upload_csv_duplicate(
    auth_client: AsyncClient,
    data_home: Path,
    sample_jmx_bytes: bytes,
) -> None:
    case_id = await _create_testcase_with_jmx(auth_client, "c4", sample_jmx_bytes)
    files = {"csvFile": ("data.csv", b"a,b\n1,2\n", "text/csv")}
    await auth_client.post(f"/csv/upload/{case_id}", files=files)
    resp = await auth_client.post(f"/csv/upload/{case_id}", files=files)
    assert resp.json()["code"] == 1018  # CSV_IS_EXIST


@pytest.mark.asyncio
async def test_list_csv_and_get_by_testcase(
    auth_client: AsyncClient,
    data_home: Path,
    sample_jmx_bytes: bytes,
) -> None:
    case_id = await _create_testcase_with_jmx(auth_client, "c5", sample_jmx_bytes)
    files = {"csvFile": ("data.csv", b"a,b\n1,2\n", "text/csv")}
    await auth_client.post(f"/csv/upload/{case_id}", files=files)

    list_resp = await auth_client.get("/csv/list?page=1&size=10")
    assert list_resp.json()["data"]["total"] == 1

    by_case = await auth_client.get(f"/csv/getByTestCaseId?testCaseId={case_id}")
    items = by_case.json()["data"]
    assert len(items) == 1
    assert items[0]["srcName"] == "data.csv"


@pytest.mark.asyncio
async def test_delete_csv(
    auth_client: AsyncClient,
    data_home: Path,
    sample_jmx_bytes: bytes,
    db,
) -> None:
    from sqlalchemy import select

    from app.models.csv import Csv

    case_id = await _create_testcase_with_jmx(auth_client, "c6", sample_jmx_bytes)
    files = {"csvFile": ("data.csv", b"a,b\n1,2\n", "text/csv")}
    await auth_client.post(f"/csv/upload/{case_id}", files=files)
    obj = (await db.execute(select(Csv).where(Csv.test_case_id == case_id))).scalar_one()
    filepath = obj.csv_dir + obj.dst_name

    resp = await auth_client.get(f"/csv/delete/{obj.id}")
    assert resp.json()["data"] is True
    assert not os.path.exists(filepath)


@pytest.mark.asyncio
async def test_view_csv_has_utf8_bom(
    auth_client: AsyncClient,
    data_home: Path,
    sample_jmx_bytes: bytes,
    db,
) -> None:
    from sqlalchemy import select

    from app.models.csv import Csv

    case_id = await _create_testcase_with_jmx(auth_client, "c7", sample_jmx_bytes)
    files = {"csvFile": ("data.csv", b"user,pass\nalice,123\n", "text/csv")}
    await auth_client.post(f"/csv/upload/{case_id}", files=files)
    obj = (await db.execute(select(Csv).where(Csv.test_case_id == case_id))).scalar_one()

    resp = await auth_client.get(f"/csv/view/{obj.id}")
    assert resp.status_code == 200
    # 响应前 3 字节是 UTF-8 BOM
    assert resp.content.startswith(b"\xef\xbb\xbf")
    assert b"alice,123" in resp.content
