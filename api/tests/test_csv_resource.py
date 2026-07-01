"""公共 CSV 资源和用例绑定测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient
from lxml import etree
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import NodeStatus, NodeType, TestCaseStatus
from app.models.node import Node
from app.models.testcase import TestCase

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


async def _create_case(auth_client: AsyncClient, name: str = "csv_case") -> int:
    resp = await auth_client.post("/testcase/add", json={"name": name})
    return resp.json()["data"]


async def _create_case_with_jmx(auth_client: AsyncClient, name: str, jmx_bytes: bytes) -> int:
    case_id = await _create_case(auth_client, name)
    await auth_client.post(
        f"/jmx/upload/{case_id}",
        files={"jmxFile": ("test.jmx", jmx_bytes, "application/octet-stream")},
    )
    return case_id


@pytest.mark.asyncio
async def test_public_csv_upload_overwrite_and_list(
    auth_client: AsyncClient,
    data_home: Path,
) -> None:
    resp = await auth_client.post(
        "/csv/resource/upload",
        files={"csvFile": ("data.csv", b"a,b\n1,2\n", "text/csv")},
    )
    assert resp.json()["code"] == 0

    duplicate = await auth_client.post(
        "/csv/resource/upload",
        files={"csvFile": ("data.csv", b"a,b\n3,4\n", "text/csv")},
    )
    assert duplicate.json()["code"] != 0
    assert "已存在" in duplicate.json()["message"]

    overwrite = await auth_client.post(
        "/csv/resource/upload?overwrite=true",
        files={"csvFile": ("data.csv", b"a,b\n3,4\n", "text/csv")},
    )
    assert overwrite.json()["code"] == 0

    csv_path = data_home / "common" / "csv" / "data.csv"
    assert csv_path.read_bytes() == b"a,b\n3,4\n"

    list_resp = await auth_client.get("/csv/resource/list?page=1&size=10")
    items = list_resp.json()["data"]["list"]
    assert len(items) == 1
    assert items[0]["filename"] == "data.csv"
    assert items[0]["referenceCount"] == 0


@pytest.mark.asyncio
async def test_bind_public_csv_keeps_binding_when_resource_deleted_and_recovers_after_reupload(
    auth_client: AsyncClient,
    data_home: Path,
    sample_jmx_bytes: bytes,
) -> None:
    case_id = await _create_case_with_jmx(auth_client, "bind_public_csv", sample_jmx_bytes)
    await auth_client.post(
        "/csv/resource/upload",
        files={"csvFile": ("data.csv", b"user,pass\nu1,p1\n", "text/csv")},
    )

    bind = await auth_client.post(
        "/csv/binding/add",
        json={"testCaseId": case_id, "filename": "data.csv", "distributionStrategy": "shared"},
    )
    assert bind.json()["code"] == 0

    full = await auth_client.get(f"/testcase/getFull/{case_id}")
    bindings = full.json()["data"]["csvBindingVOList"]
    assert len(bindings) == 1
    assert bindings[0]["filename"] == "data.csv"
    assert bindings[0]["exists"] is True

    delete_without_force = await auth_client.get("/csv/resource/delete/data.csv")
    assert delete_without_force.json()["code"] != 0
    assert "引用" in delete_without_force.json()["message"]

    delete_force = await auth_client.get("/csv/resource/delete/data.csv?force=true")
    assert delete_force.json()["code"] == 0
    assert not (data_home / "common" / "csv" / "data.csv").exists()

    full_after_delete = await auth_client.get(f"/testcase/getFull/{case_id}")
    binding_after_delete = full_after_delete.json()["data"]["csvBindingVOList"][0]
    assert binding_after_delete["filename"] == "data.csv"
    assert binding_after_delete["exists"] is False

    await auth_client.post(
        "/csv/resource/upload",
        files={"csvFile": ("data.csv", b"user,pass\nu2,p2\n", "text/csv")},
    )
    full_after_reupload = await auth_client.get(f"/testcase/getFull/{case_id}")
    assert full_after_reupload.json()["data"]["csvBindingVOList"][0]["exists"] is True


@pytest.mark.asyncio
async def test_bind_public_csv_requires_jmx_reference(
    auth_client: AsyncClient,
    data_home: Path,
    sample_jmx_bytes: bytes,
) -> None:
    case_id = await _create_case_with_jmx(auth_client, "bind_missing_ref", sample_jmx_bytes)
    await auth_client.post(
        "/csv/resource/upload",
        files={"csvFile": ("other.csv", b"a,b\n1,2\n", "text/csv")},
    )

    resp = await auth_client.post(
        "/csv/binding/add",
        json={"testCaseId": case_id, "filename": "other.csv", "distributionStrategy": "shared"},
    )

    assert resp.json()["code"] != 0
    assert "未引用参数化文件" in resp.json()["message"]


@pytest.mark.asyncio
async def test_public_csv_binding_rewrites_run_jmx(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_bin_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
    monkeypatch,
) -> None:
    from app.services import jmeter_runner

    captured: dict = {}
    real_launch = jmeter_runner.launch_jmeter

    async def spy(cmd, **kw):
        captured["cmd"] = cmd
        return await real_launch(cmd, **kw)

    monkeypatch.setattr(jmeter_runner, "launch_jmeter", spy)

    case_id = await _create_case_with_jmx(auth_client, "run_public_csv", sample_jmx_bytes)
    await auth_client.post(
        "/csv/resource/upload",
        files={"csvFile": ("data.csv", b"h1,h2\n1,a\n", "text/csv")},
    )
    bind = await auth_client.post(
        "/csv/binding/add",
        json={"testCaseId": case_id, "filename": "data.csv", "distributionStrategy": "shared"},
    )
    assert bind.json()["code"] == 0

    resp = await auth_client.post(
        f"/testcase/run/{case_id}",
        json={"numThreads": "1", "rampTime": "0", "duration": "1", "slaveCount": 0},
    )
    assert resp.json()["code"] == 0
    await jmeter_runner.wait_for_completion(case_id, timeout=10.0)

    tc = (await db.execute(select(TestCase).where(TestCase.id == case_id))).scalar_one()
    assert tc.status == TestCaseStatus.RUN_SUCCESS.value

    run_jmx = Path(captured["cmd"][captured["cmd"].index("-t") + 1])
    tree = etree.parse(str(run_jmx))
    filenames = [
        prop.text
        for prop in tree.iter()
        if prop.get("name") == "filename" and prop.text
    ]
    public_path = str(data_home / "common" / "csv" / "data.csv")
    assert public_path in filenames


@pytest.mark.asyncio
async def test_public_csv_binding_split_by_slave_uses_runtime_slices(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_bin_home: Path,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
    monkeypatch,
) -> None:
    from app.core import ssh as ssh_mod
    from app.services import jmeter_runner

    captured: dict = {}
    real_launch = jmeter_runner.launch_jmeter

    async def spy(cmd, **kw):
        captured["cmd"] = cmd
        return await real_launch(cmd, **kw)

    monkeypatch.setattr(jmeter_runner, "launch_jmeter", spy)

    case_id = await _create_case_with_jmx(auth_client, "run_public_split_csv", sample_jmx_bytes)
    await auth_client.post(
        "/csv/resource/upload",
        files={"csvFile": ("data.csv", b"h1,h2\n1,a\n2,b\n3,c\n4,d\n", "text/csv")},
    )
    bind = await auth_client.post(
        "/csv/binding/add",
        json={"testCaseId": case_id, "filename": "data.csv", "distributionStrategy": "split_by_slave"},
    )
    assert bind.json()["code"] == 0

    for host in ("10.0.9.1", "10.0.9.2"):
        db.add(
            Node(
                name=host,
                type=NodeType.SLAVE.value,
                host=host,
                username="root",
                password="x",
                port=22,
                status=NodeStatus.ENABLE.value,
                health_status=1,
            )
        )
    await db.commit()

    scp_payloads: dict[str, bytes] = {}

    async def capture_scp(self, local_path: str, remote_dir: str, *, raise_on_error: bool = False) -> None:
        if "runtime_csv" in local_path:
            scp_payloads[self.host] = Path(local_path).read_bytes()

    monkeypatch.setattr(ssh_mod.SSHClient, "scp_file", capture_scp)

    resp = await auth_client.post(
        f"/testcase/run/{case_id}",
        json={"numThreads": "2", "rampTime": "0", "duration": "1", "slaveCount": 2},
    )
    assert resp.json()["code"] == 0
    await jmeter_runner.wait_for_completion(case_id, timeout=10.0)

    assert scp_payloads["10.0.9.1"] == b"h1,h2\n1,a\n3,c\n"
    assert scp_payloads["10.0.9.2"] == b"h1,h2\n2,b\n4,d\n"

    run_jmx = Path(captured["cmd"][captured["cmd"].index("-t") + 1])
    assert "runtime_csv" in run_jmx.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_public_upload_file_binding_keeps_binding_when_resource_deleted_and_recovers_after_reupload(
    auth_client: AsyncClient,
    data_home: Path,
) -> None:
    case_id = await _create_case_with_jmx(auth_client, "bind_public_upload_file", UPLOAD_FILE_JMX)
    await auth_client.post(
        "/csv/resource/upload",
        files={"csvFile": ("avatar.jpg", b"image-v1", "image/jpeg")},
    )

    bind = await auth_client.post(
        "/uploadFile/binding/add",
        json={"testCaseId": case_id, "filename": "avatar.jpg"},
    )
    assert bind.json()["code"] == 0

    full = await auth_client.get(f"/testcase/getFull/{case_id}")
    bindings = full.json()["data"]["uploadFileBindingVOList"]
    assert len(bindings) == 1
    assert bindings[0]["filename"] == "avatar.jpg"
    assert bindings[0]["exists"] is True

    delete_without_force = await auth_client.get("/csv/resource/delete/avatar.jpg")
    assert delete_without_force.json()["code"] != 0
    assert "引用" in delete_without_force.json()["message"]

    delete_force = await auth_client.get("/csv/resource/delete/avatar.jpg?force=true")
    assert delete_force.json()["code"] == 0
    assert not (data_home / "common" / "csv" / "avatar.jpg").exists()

    full_after_delete = await auth_client.get(f"/testcase/getFull/{case_id}")
    assert full_after_delete.json()["data"]["uploadFileBindingVOList"][0]["exists"] is False

    await auth_client.post(
        "/csv/resource/upload",
        files={"csvFile": ("avatar.jpg", b"image-v2", "image/jpeg")},
    )
    full_after_reupload = await auth_client.get(f"/testcase/getFull/{case_id}")
    assert full_after_reupload.json()["data"]["uploadFileBindingVOList"][0]["exists"] is True


@pytest.mark.asyncio
async def test_public_upload_file_binding_rewrites_run_jmx_and_syncs_to_slave(
    auth_client: AsyncClient,
    data_home: Path,
    jmeter_bin_home: Path,
    db: AsyncSession,
    monkeypatch,
) -> None:
    from app.core import ssh as ssh_mod
    from app.services import jmeter_runner

    captured: dict = {}
    real_launch = jmeter_runner.launch_jmeter

    async def spy(cmd, **kw):
        captured["cmd"] = cmd
        return await real_launch(cmd, **kw)

    monkeypatch.setattr(jmeter_runner, "launch_jmeter", spy)

    case_id = await _create_case_with_jmx(auth_client, "run_public_upload_file", UPLOAD_FILE_JMX)
    await auth_client.post(
        "/csv/resource/upload",
        files={"csvFile": ("avatar.jpg", b"image", "image/jpeg")},
    )
    bind = await auth_client.post(
        "/uploadFile/binding/add",
        json={"testCaseId": case_id, "filename": "avatar.jpg"},
    )
    assert bind.json()["code"] == 0

    db.add(
        Node(
            name="public-upload-file-slave",
            type=NodeType.SLAVE.value,
            host="10.0.9.3",
            username="root",
            password="x",
            port=22,
            status=NodeStatus.ENABLE.value,
            health_status=1,
        )
    )
    await db.commit()

    scp_calls: list[tuple[str, str, bool]] = []

    async def tracking_scp(self, local_path: str, remote_dir: str, *, raise_on_error: bool = False) -> None:
        scp_calls.append((local_path, remote_dir, raise_on_error))

    monkeypatch.setattr(ssh_mod.SSHClient, "scp_file", tracking_scp)

    resp = await auth_client.post(
        f"/testcase/run/{case_id}",
        json={"numThreads": "1", "rampTime": "0", "duration": "1", "slaveCount": 1},
    )
    assert resp.json()["code"] == 0
    await jmeter_runner.wait_for_completion(case_id, timeout=10.0)

    public_path = str(data_home / "common" / "csv" / "avatar.jpg")
    assert any(local_path == public_path and raise_on_error is True for local_path, _, raise_on_error in scp_calls)

    run_jmx = Path(captured["cmd"][captured["cmd"].index("-t") + 1])
    tree = etree.parse(str(run_jmx))
    values = [el.text for el in tree.iter() if el.get("name") == "File.path"]
    assert values == [public_path]
