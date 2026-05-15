"""/report/* 路由集成测试（Phase 5 + Phase 6）。"""

from __future__ import annotations

import os

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ExecType
from app.models.config import Config
from app.models.report import Report


async def _insert_report(
    db: AsyncSession,
    name: str = "rpt",
    test_case_id: int = 1,
    exec_type: int = 1,
    status: int = 0,
    report_dir: str = "/tmp/r",
) -> int:
    r = Report(
        name=name,
        description=name + " desc",
        test_case_id=test_case_id,
        report_dir=report_dir,
        exec_type=exec_type,
        status=status,
        response_data="",
        jmeter_log_file_path=report_dir + "/jmeter.log",
    )
    db.add(r)
    await db.commit()
    await db.refresh(r)
    return r.id


@pytest.mark.asyncio
async def test_report_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/report/list")
    assert resp.json()["code"] == 1007


@pytest.mark.asyncio
async def test_report_list_paginates_and_filters_by_name(
    auth_client: AsyncClient, db: AsyncSession
) -> None:
    await _insert_report(db, name="alpha")
    await _insert_report(db, name="beta")
    await _insert_report(db, name="alpha_v2")

    resp = await auth_client.get("/report/list?page=1&size=10&name=alpha")
    page = resp.json()["data"]
    assert page["total"] == 2
    names = sorted(item["name"] for item in page["list"])
    assert names == ["alpha", "alpha_v2"]


@pytest.mark.asyncio
async def test_report_list_by_test_case(auth_client: AsyncClient, db: AsyncSession) -> None:
    await _insert_report(db, name="a", test_case_id=10)
    await _insert_report(db, name="b", test_case_id=10)
    await _insert_report(db, name="c", test_case_id=20)

    resp = await auth_client.get("/report/listByTestCase?page=1&size=10&testCaseId=10")
    page = resp.json()["data"]
    assert page["total"] == 2


@pytest.mark.asyncio
async def test_report_get_by_id(auth_client: AsyncClient, db: AsyncSession) -> None:
    rid = await _insert_report(db, name="findme")
    resp = await auth_client.get(f"/report/getById/{rid}")
    item = resp.json()["data"]
    assert item["name"] == "findme"
    assert item["id"] == rid


# ---------------------------------------------------------------------------
# Phase 6 — clean / download / view
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_report_deletes_db_and_disk(
    auth_client: AsyncClient, db: AsyncSession, tmp_path
) -> None:
    report_dir = str(tmp_path / "2026-05-13-10:00:00" / "data")
    os.makedirs(report_dir, exist_ok=True)
    (tmp_path / "2026-05-13-10:00:00" / "extra.txt").write_text("extra")

    rid = await _insert_report(db, name="cleanme", report_dir=report_dir)
    resp = await auth_client.get(f"/report/clean/{rid}")
    assert resp.json()["code"] == 0
    assert resp.json()["data"] is True

    # DB 已删
    remaining = await db.get(Report, rid)
    assert remaining is None

    # 磁盘父目录已删
    assert not os.path.exists(tmp_path / "2026-05-13-10:00:00")


@pytest.mark.asyncio
async def test_clean_report_not_exist(auth_client: AsyncClient) -> None:
    resp = await auth_client.get("/report/clean/99999")
    assert resp.json()["code"] == 1044  # REPORT_NOT_EXIST


@pytest.mark.asyncio
async def test_download_debug_report_blocked(auth_client: AsyncClient, db: AsyncSession) -> None:
    rid = await _insert_report(db, name="debug_rpt", exec_type=ExecType.DEBUG.value)
    resp = await auth_client.get(f"/report/download/{rid}")
    assert resp.json()["code"] == 1045  # DEBUG_REPORT_NOT_DOWNLOAD


@pytest.mark.asyncio
async def test_download_report_dir_not_exist(auth_client: AsyncClient, db: AsyncSession) -> None:
    rid = await _insert_report(db, name="missing", exec_type=ExecType.EXEC.value, report_dir="/nonexistent/path/data")
    resp = await auth_client.get(f"/report/download/{rid}")
    assert resp.json()["code"] == 1046  # REPORT_DIR_NOT_EXIST


@pytest.mark.asyncio
async def test_download_report_empty_dir(auth_client: AsyncClient, db: AsyncSession, tmp_path) -> None:
    report_dir = str(tmp_path / "empty" / "data")
    os.makedirs(report_dir, exist_ok=True)
    rid = await _insert_report(db, name="empty_rpt", exec_type=ExecType.EXEC.value, report_dir=report_dir)
    resp = await auth_client.get(f"/report/download/{rid}")
    assert resp.json()["code"] == 1047  # REPORT_DIR_IS_EMPTY


@pytest.mark.asyncio
async def test_download_report_success(auth_client: AsyncClient, db: AsyncSession, tmp_path) -> None:
    report_dir = str(tmp_path / "2026-05-13-11:00:00" / "data")
    os.makedirs(report_dir, exist_ok=True)
    (tmp_path / "2026-05-13-11:00:00" / "data" / "index.html").write_text("<html>report</html>")

    rid = await _insert_report(db, name="exec_rpt", exec_type=ExecType.EXEC.value, report_dir=report_dir)
    resp = await auth_client.get(f"/report/download/{rid}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "exec_rpt.zip" in resp.headers["content-disposition"]


@pytest.mark.asyncio
async def test_view_debug_report_blocked(auth_client: AsyncClient, db: AsyncSession) -> None:
    rid = await _insert_report(db, name="debug_view", exec_type=ExecType.DEBUG.value)
    resp = await auth_client.get(f"/report/view/{rid}")
    assert resp.json()["code"] == 1048  # DEBUG_REPORT_NOT_VIEW


@pytest.mark.asyncio
async def test_view_report_success(auth_client: AsyncClient, db: AsyncSession, tmp_path) -> None:
    report_dir = str(tmp_path / "2026-05-13-12:00:00" / "data")
    os.makedirs(report_dir, exist_ok=True)
    (tmp_path / "2026-05-13-12:00:00" / "data" / "index.html").write_text("report")

    # 需要 MASTER_HOST_PORT config
    async with db.begin():
        db.add(Config(config_key="MASTER_HOST_PORT", config_value="localhost:1234", description="host"))

    rid = await _insert_report(db, name="view_rpt", exec_type=ExecType.EXEC.value, report_dir=report_dir)
    resp = await auth_client.get(f"/report/view/{rid}")
    assert resp.json()["code"] == 0
    assert resp.json()["data"].endswith("/index.html")
