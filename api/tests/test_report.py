"""/report/* 路由集成测试（Phase 5 + Phase 6）。"""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ExecType
from app.models.config import Config
from app.models.report import Report
from app.models.testcase import TestCase
from app.services.report import _parse_jtl_metrics


async def _insert_report(
    db: AsyncSession,
    name: str = "rpt",
    test_case_id: int = 1,
    exec_type: int = 1,
    status: int = 0,
    report_dir: str = "/tmp/r",
    service_name: str = "",
    total_threads: int = 0,
    slave_count: int = 0,
    grafana_instance: str = "",
    artifact_dir: str = "",
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
        service_name=service_name,
        total_threads=total_threads,
        slave_count=slave_count,
        grafana_instance=grafana_instance,
        artifact_dir=artifact_dir,
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
    rid = await _insert_report(
        db,
        name="findme",
        service_name="EMM-API",
        total_threads=30,
        slave_count=2,
        grafana_instance="10.10.27.42:9200",
        artifact_dir="/tmp/r/artifacts",
    )
    resp = await auth_client.get(f"/report/getById/{rid}")
    item = resp.json()["data"]
    assert item["name"] == "findme"
    assert item["id"] == rid
    assert item["serviceName"] == "EMM-API"
    assert item["totalThreads"] == 30
    assert item["slaveCount"] == 2
    assert item["grafanaInstance"] == "10.10.27.42:9200"
    assert item["artifactDir"] == "/tmp/r/artifacts"


@pytest.mark.asyncio
async def test_report_stats_counts_history_reports(
    auth_client: AsyncClient, db: AsyncSession
) -> None:
    await _insert_report(db, name="stat_api_1", status=0)
    await _insert_report(db, name="stat_api_2", status=1)
    await _insert_report(db, name="stat_api_3", status=2)
    await _insert_report(db, name="stat_api_4", status=2)
    await _insert_report(db, name="stat_api_5", status=3)
    await _insert_report(db, name="other", status=2)

    resp = await auth_client.get("/report/stats?name=stat_api")
    body = resp.json()

    assert body["code"] == 0
    assert body["data"] == {
        "total": 5,
        "running": 1,
        "success": 2,
        "failed": 1,
        "idle": 1,
        "successRate": 66.7,
    }


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


@pytest.mark.asyncio
async def test_grafana_url_uses_dashboard_template_and_service_instance(
    auth_client: AsyncClient,
    db: AsyncSession,
) -> None:
    db.add(TestCase(id=10, name="case-1", service="EMM-API"))
    db.add(
        Config(
            config_key="GRAFANA_DASHBOARD_URL",
            config_value=(
                "http://10.10.27.210:3000/d/StarsL-TenSunS-node/0d50bf8"
                "?var-interval=3m&orgId=1&from=now-30m&to=now"
                "&var-instance=10.10.27.42:9200&refresh=1m"
            ),
            description="grafana url",
        )
    )
    db.add(
        Config(
            config_key="GRAFANA_INSTANCE_MAP",
            config_value='{"EMM-API":"10.10.27.42:9200"}',
            description="service instance map",
        )
    )
    await db.commit()

    rid = await _insert_report(db, name="rpt", test_case_id=10, exec_type=ExecType.EXEC.value)
    resp = await auth_client.get(f"/report/grafana/{rid}")

    assert resp.json()["code"] == 0
    url = resp.json()["data"]
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert parts.netloc == "10.10.27.210:3000"
    assert query["var-instance"] == ["10.10.27.42:9200"]
    assert query["orgId"] == ["1"]
    assert query["refresh"] == ["1m"]
    assert query["from"][0].isdigit()
    assert query["to"][0].isdigit()
    assert "var-region" not in query


@pytest.mark.asyncio
async def test_grafana_url_prefers_report_snapshot_instance(
    auth_client: AsyncClient,
    db: AsyncSession,
) -> None:
    db.add(TestCase(id=20, name="case-2", service="EMM-CORE"))
    db.add(
        Config(
            config_key="GRAFANA_DASHBOARD_URL",
            config_value="http://10.10.27.210:3000/d/StarsL-TenSunS-node/0d50bf8?orgId=1",
            description="grafana url",
        )
    )
    db.add(
        Config(
            config_key="GRAFANA_INSTANCE_MAP",
            config_value='{"EMM-CORE":"10.10.27.43:9200"}',
            description="service instance map",
        )
    )
    await db.commit()

    rid = await _insert_report(
        db,
        name="rpt",
        test_case_id=20,
        exec_type=ExecType.EXEC.value,
        service_name="EMM-API",
        grafana_instance="10.10.27.42:9200",
    )
    resp = await auth_client.get(f"/report/grafana/{rid}")

    query = parse_qs(urlsplit(resp.json()["data"]).query)
    assert query["var-instance"] == ["10.10.27.42:9200"]


def test_parse_jtl_metrics_converts_distributed_threads_to_total(tmp_path) -> None:
    jtl = tmp_path / "result.jtl"
    jtl.write_text(
        "\n".join(
            [
                "timeStamp,elapsed,success,allThreads,grpThreads",
                "1700000000000,100,true,15,15",
                "1700000001000,120,true,15,15",
            ]
        ),
        encoding="utf-8",
    )

    items = _parse_jtl_metrics(
        str(jtl),
        5,
        {"total_threads": 30, "slave_count": 2, "per_slave_threads": 15},
    )

    assert len(items) == 1
    assert items[0]["threads"] == 30


def test_parse_jtl_metrics_keeps_single_machine_threads(tmp_path) -> None:
    jtl = tmp_path / "result.jtl"
    jtl.write_text(
        "\n".join(
            [
                "timeStamp,elapsed,success,allThreads,grpThreads",
                "1700000000000,100,true,10,10",
                "1700000001000,120,true,15,15",
            ]
        ),
        encoding="utf-8",
    )

    items = _parse_jtl_metrics(str(jtl), 5)

    assert len(items) == 1
    assert items[0]["threads"] == 15
