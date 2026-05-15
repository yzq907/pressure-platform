"""/jmx/addOnline /getOnline /updateOnline /forceDelete 集成测试。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from httpx import AsyncClient
from lxml import etree
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import JMeterSample, JMeterScript, JMeterThreads
from app.models.jmx import Jmx
from app.models.jmx_assertion import JmxAssertion
from app.models.jmx_csv import JmxCsv
from app.models.jmx_http import JmxHttp
from app.models.jmx_http_header import JmxHttpHeader
from app.models.jmx_http_param import JmxHttpParam
from app.models.jmx_java import JmxJava
from app.models.jmx_stepping_thread_group import JmxSteppingThreadGroup
from app.models.jmx_thread_group import JmxThreadGroup


async def _create_testcase(auth_client: AsyncClient, name: str = "online_case") -> int:
    resp = await auth_client.post("/testcase/add", json={"name": name})
    return resp.json()["data"]


def _jmx_xml_text(jmx_path: str, tag: str, name: str) -> str | None:
    tree = etree.parse(jmx_path)
    for el in tree.iter(tag):
        if el.get("name") == name:
            return el.text
    return None


def _make_online_payload(case_id: int) -> dict:
    """构造一个完整的在线 JMX payload（ThreadGroup + HTTP + header + param + assertion + CSV）"""
    return {
        "srcName": "online_demo.jmx",
        "testCaseId": case_id,
        "jmeterThreadsType": JMeterThreads.THREAD_GROUP.value,
        "jmeterSampleType": JMeterSample.HTTP_REQUEST.value,
        "threadGroupVO": {
            "numThreads": "10",
            "rampTime": "1",
            "loops": "1",
            "scheduler": 0,
            "delayedStart": 0,
            "sameUserOnNextIteration": 1,
        },
        "httpVO": {
            "method": "GET",
            "protocol": "HTTPS",
            "domain": "example.com",
            "port": "443",
            "path": "/api",
            "contentEncoding": "UTF-8",
            "httpHeaderVOList": [{"headerKey": "X-Token", "headerValue": "abc"}],
            "httpParamVOList": [{"paramKey": "page", "paramValue": "1"}],
            "body": "",
        },
        "assertionVO": {
            "responseCode": "200",
            "responseMessage": "OK",
            "jsonPath": "",
            "expectedValue": "",
        },
        "csvDataVO": {
            "csvFileVOList": [{"filename": "data.csv", "variableNames": "user,pass"}],
            "fileEncoding": "UTF-8",
            "delimiter": ",",
            "ignoreFirstLine": 1,
            "allowQuotedData": 0,
            "recycleOnEof": 1,
            "stopThreadOnEof": 0,
            "sharingMode": "Current thread group",
        },
    }


@pytest.mark.asyncio
async def test_online_jmx_requires_auth(client: AsyncClient) -> None:
    resp = await client.post("/jmx/addOnline", json={})
    assert resp.json()["code"] == 1007


@pytest.mark.asyncio
async def test_add_online_jmx_success(
    auth_client: AsyncClient,
    db: AsyncSession,
    data_home: Path,
) -> None:
    case_id = await _create_testcase(auth_client)
    payload = _make_online_payload(case_id)
    resp = await auth_client.post("/jmx/addOnline", json=payload)
    assert resp.json()["code"] == 0
    assert resp.json()["data"] is True

    # DB 检查：JmxDO
    jmx = (await db.execute(select(Jmx).where(Jmx.test_case_id == case_id))).scalar_one()
    assert jmx.src_name == "online_demo.jmx"
    assert jmx.jmeter_script_type == JMeterScript.ONLINE_JMX.value
    assert jmx.jmeter_threads_type == JMeterThreads.THREAD_GROUP.value
    assert jmx.jmeter_sample_type == JMeterSample.HTTP_REQUEST.value

    # DB 检查：线程组
    tg = (await db.execute(select(JmxThreadGroup).where(JmxThreadGroup.jmx_id == jmx.id))).scalar_one()
    assert tg.num_threads == "10"

    # DB 检查：HTTP
    http = (await db.execute(select(JmxHttp).where(JmxHttp.jmx_id == jmx.id))).scalar_one()
    assert http.domain == "example.com"

    # DB 检查：header + param
    headers = (await db.execute(select(JmxHttpHeader).where(JmxHttpHeader.jmx_id == jmx.id))).scalars().all()
    assert len(headers) == 1
    assert headers[0].header_key == "X-Token"
    params = (await db.execute(select(JmxHttpParam).where(JmxHttpParam.jmx_id == jmx.id))).scalars().all()
    assert len(params) == 1
    assert params[0].param_key == "page"

    # DB 检查：断言
    assertion = (await db.execute(select(JmxAssertion).where(JmxAssertion.jmx_id == jmx.id))).scalar_one()
    assert assertion.response_code == "200"

    # DB 检查：CSV
    csv_rows = (await db.execute(select(JmxCsv).where(JmxCsv.jmx_id == jmx.id))).scalars().all()
    assert len(csv_rows) == 1
    assert csv_rows[0].filename == "data.csv"

    # 磁盘检查：JMX 文件存在且 XML 字段正确
    jmx_path = jmx.jmx_dir + "online_demo.jmx"
    assert os.path.exists(jmx_path)
    assert _jmx_xml_text(jmx_path, "stringProp", "ThreadGroup.num_threads") == "10"
    assert _jmx_xml_text(jmx_path, "stringProp", "HTTPSampler.domain") == "example.com"
    assert _jmx_xml_text(jmx_path, "stringProp", "filename") == "data.csv"

    # debug 副本也存在
    assert os.path.exists(jmx.jmx_dir + "debug_online_demo.jmx")


@pytest.mark.asyncio
async def test_add_online_jmx_duplicate_testcase(
    auth_client: AsyncClient,
    data_home: Path,
) -> None:
    case_id = await _create_testcase(auth_client)
    payload = _make_online_payload(case_id)
    await auth_client.post("/jmx/addOnline", json=payload)

    # 第二次添加同 testcase
    resp = await auth_client.post("/jmx/addOnline", json=payload)
    assert resp.json()["code"] == 1034  # TESTCASE_HAS_JMX


@pytest.mark.asyncio
async def test_get_online_jmx_returns_nested_vo(
    auth_client: AsyncClient,
    db: AsyncSession,
    data_home: Path,
) -> None:
    case_id = await _create_testcase(auth_client)
    payload = _make_online_payload(case_id)
    await auth_client.post("/jmx/addOnline", json=payload)

    jmx = (await db.execute(select(Jmx).where(Jmx.test_case_id == case_id))).scalar_one()

    resp = await auth_client.get(f"/jmx/getOnline/{jmx.id}")
    assert resp.json()["code"] == 0
    data = resp.json()["data"]
    # camelCase alias 校验
    assert data["srcName"] == "online_demo.jmx"
    assert data["threadGroupVO"]["numThreads"] == "10"
    assert data["httpVO"]["domain"] == "example.com"
    assert len(data["httpVO"]["httpHeaderVOList"]) == 1
    assert data["httpVO"]["httpHeaderVOList"][0]["headerKey"] == "X-Token"
    assert len(data["httpVO"]["httpParamVOList"]) == 1
    assert data["assertionVO"]["responseCode"] == "200"
    assert data["csvDataVO"]["csvFileVOList"][0]["filename"] == "data.csv"


@pytest.mark.asyncio
async def test_update_online_jmx_change_headers(
    auth_client: AsyncClient,
    db: AsyncSession,
    data_home: Path,
) -> None:
    case_id = await _create_testcase(auth_client)
    payload = _make_online_payload(case_id)
    await auth_client.post("/jmx/addOnline", json=payload)
    jmx = (await db.execute(select(Jmx).where(Jmx.test_case_id == case_id))).scalar_one()

    # 修改 header：从 1 个变成 2 个新的
    payload["httpVO"]["httpHeaderVOList"] = [
        {"headerKey": "Authorization", "headerValue": "Bearer x"},
        {"headerKey": "Accept", "headerValue": "application/json"},
    ]
    payload["httpVO"]["httpParamVOList"] = []  # 清空 param

    resp = await auth_client.post(f"/jmx/updateOnline/{jmx.id}", json=payload)
    assert resp.json()["code"] == 0

    # DB 验证
    headers = (await db.execute(select(JmxHttpHeader).where(JmxHttpHeader.jmx_id == jmx.id))).scalars().all()
    assert len(headers) == 2
    header_keys = {h.header_key for h in headers}
    assert header_keys == {"Authorization", "Accept"}

    params = (await db.execute(select(JmxHttpParam).where(JmxHttpParam.jmx_id == jmx.id))).scalars().all()
    assert len(params) == 0

    # JMX 文件验证
    jmx_path = jmx.jmx_dir + "online_demo.jmx"
    tree = etree.parse(jmx_path)
    header_names = []
    for ep in tree.iter("elementProp"):
        if ep.get("elementType") == "Header":
            for child in ep:
                if child.get("name") == "Header.name":
                    header_names.append(child.text)
    assert "Authorization" in header_names
    assert "Accept" in header_names
    assert "X-Token" not in header_names


@pytest.mark.asyncio
async def test_update_online_jmx_switch_thread_group(
    auth_client: AsyncClient,
    db: AsyncSession,
    data_home: Path,
) -> None:
    case_id = await _create_testcase(auth_client)
    payload = _make_online_payload(case_id)
    await auth_client.post("/jmx/addOnline", json=payload)
    jmx = (await db.execute(select(Jmx).where(Jmx.test_case_id == case_id))).scalar_one()

    # 切换线程组：普通 → 梯度
    payload["jmeterThreadsType"] = JMeterThreads.STEPPING_THREAD_GROUP.value
    payload["threadGroupVO"] = None
    payload["steppingThreadGroupVO"] = {
        "numThreads": "500",
        "firstWaitForSeconds": "0",
        "thenStartThreads": "10",
        "nextAddThreads": "50",
        "nextAddThreadsEverySeconds": "30",
        "usingRampUpSeconds": "5",
        "thenHoldLoadForSeconds": "60",
        "finallyStopThreads": "50",
        "finallyStopThreadsEverySeconds": "10",
    }

    resp = await auth_client.post(f"/jmx/updateOnline/{jmx.id}", json=payload)
    assert resp.json()["code"] == 0

    # DB：旧的 thread_group 被删，新的 stepping 存在
    old_tg = (await db.execute(select(JmxThreadGroup).where(JmxThreadGroup.jmx_id == jmx.id))).scalar_one_or_none()
    assert old_tg is None
    new_tg = (await db.execute(select(JmxSteppingThreadGroup).where(JmxSteppingThreadGroup.jmx_id == jmx.id))).scalar_one()
    assert new_tg.num_threads == "500"

    # JMX 文件：梯度线程组参数正确
    jmx_path = jmx.jmx_dir + "online_demo.jmx"
    assert _jmx_xml_text(jmx_path, "stringProp", "ThreadGroup.num_threads") == "500"
    assert _jmx_xml_text(jmx_path, "stringProp", "flighttime") == "60"


@pytest.mark.asyncio
async def test_force_delete_jmx_cascades_subtables(
    auth_client: AsyncClient,
    db: AsyncSession,
    data_home: Path,
) -> None:
    case_id = await _create_testcase(auth_client)
    payload = _make_online_payload(case_id)
    await auth_client.post("/jmx/addOnline", json=payload)
    jmx = (await db.execute(select(Jmx).where(Jmx.test_case_id == case_id))).scalar_one()
    jmx_dir = jmx.jmx_dir

    resp = await auth_client.get(f"/jmx/forceDelete/{jmx.id}")
    assert resp.json()["code"] == 0
    assert resp.json()["data"] is True

    # JmxDO 已删
    remaining = (await db.execute(select(Jmx).where(Jmx.id == jmx.id))).scalar_one_or_none()
    assert remaining is None

    # 所有子表都清空
    assert (await db.execute(select(JmxThreadGroup).where(JmxThreadGroup.jmx_id == jmx.id))).scalar_one_or_none() is None
    assert (await db.execute(select(JmxHttp).where(JmxHttp.jmx_id == jmx.id))).scalar_one_or_none() is None
    assert (await db.execute(select(JmxHttpHeader).where(JmxHttpHeader.jmx_id == jmx.id))).scalar_one_or_none() is None
    assert (await db.execute(select(JmxHttpParam).where(JmxHttpParam.jmx_id == jmx.id))).scalar_one_or_none() is None
    assert (await db.execute(select(JmxAssertion).where(JmxAssertion.jmx_id == jmx.id))).scalar_one_or_none() is None
    assert (await db.execute(select(JmxCsv).where(JmxCsv.jmx_id == jmx.id))).scalar_one_or_none() is None

    # 目录也删了
    assert not os.path.exists(jmx_dir)


@pytest.mark.asyncio
async def test_delete_jmx_online_cascades_subtables(
    auth_client: AsyncClient,
    db: AsyncSession,
    data_home: Path,
) -> None:
    """上传模式的 delete_jmx 已有测试；此用例验证 ONLINE_JMX 模式也会级联删子表"""
    case_id = await _create_testcase(auth_client)
    payload = _make_online_payload(case_id)
    await auth_client.post("/jmx/addOnline", json=payload)
    jmx = (await db.execute(select(Jmx).where(Jmx.test_case_id == case_id))).scalar_one()

    resp = await auth_client.get(f"/jmx/delete/{jmx.id}")
    assert resp.json()["code"] == 0

    # 子表全清
    assert (await db.execute(select(JmxThreadGroup).where(JmxThreadGroup.jmx_id == jmx.id))).scalar_one_or_none() is None
    assert (await db.execute(select(JmxHttp).where(JmxHttp.jmx_id == jmx.id))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_add_online_java_request(
    auth_client: AsyncClient,
    db: AsyncSession,
    data_home: Path,
) -> None:
    """Java Sample 路径：在线新增 Java Request JMX"""
    case_id = await _create_testcase(auth_client)
    payload = {
        "srcName": "java_demo.jmx",
        "testCaseId": case_id,
        "jmeterThreadsType": JMeterThreads.THREAD_GROUP.value,
        "jmeterSampleType": JMeterSample.JAVA_REQUEST.value,
        "threadGroupVO": {
            "numThreads": "5",
            "rampTime": "1",
            "loops": "1",
            "scheduler": 0,
            "delayedStart": 0,
            "sameUserOnNextIteration": 1,
        },
        "javaVO": {
            "javaRequestClassPath": "com.example.MySampler",
            "javaParamVOList": [
                {"paramKey": "threads", "paramValue": "100"},
                {"paramKey": "duration", "paramValue": "60"},
            ],
        },
        "assertionVO": {"responseCode": "", "responseMessage": "", "jsonPath": "", "expectedValue": ""},
        "csvDataVO": {"csvFileVOList": []},
    }
    resp = await auth_client.post("/jmx/addOnline", json=payload)
    assert resp.json()["code"] == 0

    jmx = (await db.execute(select(Jmx).where(Jmx.test_case_id == case_id))).scalar_one()
    assert jmx.jmeter_sample_type == JMeterSample.JAVA_REQUEST.value

    # DB：Java 表 2 行
    java_rows = (await db.execute(select(JmxJava).where(JmxJava.jmx_id == jmx.id))).scalars().all()
    assert len(java_rows) == 2
    assert java_rows[0].java_request_class_path == "com.example.MySampler"

    # JMX 文件验证
    jmx_path = jmx.jmx_dir + "java_demo.jmx"
    assert _jmx_xml_text(jmx_path, "stringProp", "classname") == "com.example.MySampler"
