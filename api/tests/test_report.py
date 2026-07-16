"""/report/* 路由集成测试（Phase 5 + Phase 6）。"""

from __future__ import annotations

import asyncio
import os
import json
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ExecType, TestCaseStatus
from app.core.exceptions import MysteriousException
from app.models.config import Config
from app.models.execution_node import ExecutionNode
from app.models.execution_run import ExecutionRun
from app.models.report import Report
from app.models.report_metric_snapshot import ReportMetricSnapshot
from app.models.report_transaction_metric_snapshot import ReportTransactionMetricSnapshot
from app.models.report_transaction_snapshot import ReportTransactionSnapshot
from app.models.testcase import TestCase
from app.services import report as report_service
from app.services import report_metrics as report_metrics_service
from app.services import prometheus as prometheus_service
from app.services.report import _parse_jtl_metrics


@pytest.fixture(autouse=True)
def _clear_prometheus_resource_metrics_cache():
    prometheus_service._RESOURCE_METRICS_CACHE.clear()
    yield
    prometheus_service._RESOURCE_METRICS_CACHE.clear()


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
async def test_report_list_filters_by_exec_type(auth_client: AsyncClient, db: AsyncSession) -> None:
    await _insert_report(db, name="type_debug", exec_type=ExecType.DEBUG.value)
    await _insert_report(db, name="type_run", exec_type=ExecType.EXEC.value)

    resp = await auth_client.get(f"/report/list?page=1&size=10&execType={ExecType.EXEC.value}")

    page = resp.json()["data"]
    assert page["total"] == 1
    assert page["list"][0]["name"] == "type_run"


@pytest.mark.asyncio
async def test_report_list_filters_by_status(auth_client: AsyncClient, db: AsyncSession) -> None:
    await _insert_report(db, name="status_running", status=TestCaseStatus.RUN_ING.value)
    await _insert_report(db, name="status_success", status=TestCaseStatus.RUN_SUCCESS.value)
    await _insert_report(db, name="status_failed", status=TestCaseStatus.RUN_FAILED.value)

    resp = await auth_client.get(
        f"/report/list?page=1&size=10&status={TestCaseStatus.RUN_SUCCESS.value}"
    )

    page = resp.json()["data"]
    assert page["total"] == 1
    assert page["list"][0]["name"] == "status_success"


@pytest.mark.asyncio
async def test_report_list_by_test_case_filters_by_exec_type(
    auth_client: AsyncClient, db: AsyncSession
) -> None:
    await _insert_report(db, name="case_debug", test_case_id=88, exec_type=ExecType.DEBUG.value)
    await _insert_report(db, name="case_run", test_case_id=88, exec_type=ExecType.EXEC.value)
    await _insert_report(db, name="case_other", test_case_id=99, exec_type=ExecType.EXEC.value)

    resp = await auth_client.get(
        f"/report/listByTestCase?page=1&size=10&testCaseId=88&execType={ExecType.DEBUG.value}"
    )

    page = resp.json()["data"]
    assert page["total"] == 1
    assert page["list"][0]["name"] == "case_debug"


@pytest.mark.asyncio
async def test_report_list_by_test_case_filters_by_status(
    auth_client: AsyncClient, db: AsyncSession
) -> None:
    await _insert_report(
        db,
        name="case_status_success",
        test_case_id=88,
        status=TestCaseStatus.RUN_SUCCESS.value,
    )
    await _insert_report(
        db,
        name="case_status_failed",
        test_case_id=88,
        status=TestCaseStatus.RUN_FAILED.value,
    )
    await _insert_report(
        db,
        name="case_status_other",
        test_case_id=99,
        status=TestCaseStatus.RUN_SUCCESS.value,
    )

    resp = await auth_client.get(
        f"/report/listByTestCase?page=1&size=10&testCaseId=88&status={TestCaseStatus.RUN_FAILED.value}"
    )

    page = resp.json()["data"]
    assert page["total"] == 1
    assert page["list"][0]["name"] == "case_status_failed"


@pytest.mark.asyncio
async def test_report_list_includes_active_occupied_node_hosts(
    auth_client: AsyncClient, db: AsyncSession
) -> None:
    rid = await _insert_report(db, name="running-with-node", status=TestCaseStatus.RUN_ING.value)
    db.add(
        ExecutionNode(
            report_id=rid,
            test_case_id=1,
            node_id=101,
            node_host="10.10.27.111",
            region="华南",
            status="leased",
        )
    )
    db.add(
        ExecutionNode(
            report_id=rid,
            test_case_id=1,
            node_id=102,
            node_host="10.10.27.97",
            region="华南",
            status="released",
        )
    )
    await db.commit()

    resp = await auth_client.get("/report/list?page=1&size=10&name=running-with-node")
    item = resp.json()["data"]["list"][0]
    assert item["occupiedNodeHosts"] == ["10.10.27.111"]


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
async def test_report_artifacts_hide_internal_error_sample_files(db: AsyncSession, tmp_path) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    (artifact_dir / "report.zip").write_text("public", encoding="utf-8")
    (artifact_dir / "error_samples.xml").write_text("<testResults/>", encoding="utf-8")
    (artifact_dir / "error_samples.10.10.27.97.jsonl").write_text("", encoding="utf-8")
    rid = await _insert_report(db, name="artifact-filter", artifact_dir=str(artifact_dir))

    items = await report_service.list_artifacts(db, rid)

    assert [item.name for item in items] == ["report.zip"]
    with pytest.raises(MysteriousException):
        await report_service.download_artifact(db, rid, "error_samples.xml")


@pytest.mark.asyncio
async def test_report_error_samples_reads_internal_jsonl_and_groups_by_error_type(
    auth_client: AsyncClient, db: AsyncSession, tmp_path
) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    error_dir = tmp_path / "_internal" / "error_samples"
    error_dir.mkdir(parents=True)
    (error_dir / "error_samples.jsonl").write_text(
        "\n".join(
            [
                json.dumps({
                    "sampleTime": 1700000000000,
                    "label": "登录接口",
                    "threadName": "Thread Group 1-1",
                    "responseCode": "403",
                    "responseMessage": "Forbidden",
                    "elapsed": 120,
                    "failureMessage": "",
                    "requestHeaders": "Authorization: Bearer abc\nCookie: sid=123",
                    "responseHeaders": "HTTP/1.1 403 Forbidden",
                    "responseBody": "{\"code\":403}",
                    "truncated": False,
                    "errorType": "403",
                }),
                json.dumps({
                    "sampleTime": 1700000001000,
                    "label": "查询接口",
                    "threadName": "Thread Group 1-2",
                    "responseCode": "504",
                    "responseMessage": "Gateway Timeout",
                    "elapsed": 3000,
                    "failureMessage": "",
                    "requestHeaders": "",
                    "responseHeaders": "HTTP/1.1 504 Gateway Timeout",
                    "responseBody": "timeout",
                    "truncated": False,
                    "errorType": "504",
                }),
                "not-json",
            ]
        ),
        encoding="utf-8",
    )
    rid = await _insert_report(db, name="error-samples", artifact_dir=str(artifact_dir))

    resp = await auth_client.get(f"/report/errorSamples/{rid}")

    assert resp.json()["code"] == 0
    data = resp.json()["data"]
    assert data["total"] == 2
    assert data["groups"] == {"403": 1, "504": 1}
    assert data["list"][0]["label"] == "查询接口"
    assert data["list"][0]["errorType"] == "504"
    assert "Bearer abc" not in data["list"][1]["requestHeaders"]
    assert "******" in data["list"][1]["requestHeaders"]


@pytest.mark.asyncio
async def test_report_error_samples_ignores_legacy_artifact_jsonl(
    auth_client: AsyncClient, db: AsyncSession, tmp_path
) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    (artifact_dir / "error_samples.jsonl").write_text(
        json.dumps(
            {
                "sampleTime": 1700000000000,
                "label": "旧位置错误",
                "responseCode": "500",
                "errorType": "500",
            }
        ),
        encoding="utf-8",
    )
    rid = await _insert_report(db, name="ignore-legacy-error-samples", artifact_dir=str(artifact_dir))

    resp = await auth_client.get(f"/report/errorSamples/{rid}")

    data = resp.json()["data"]
    assert data["total"] == 0
    assert data["groups"] == {}
    assert data["list"] == []


@pytest.mark.asyncio
async def test_report_error_samples_reads_internal_error_sample_dir(
    auth_client: AsyncClient, db: AsyncSession, tmp_path
) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    error_dir = tmp_path / "_internal" / "error_samples"
    error_dir.mkdir(parents=True)
    (error_dir / "error_samples.jsonl").write_text(
        json.dumps(
            {
                "sampleTime": 1700000000000,
                "label": "内部错误",
                "threadName": "Thread Group 1-1",
                "responseCode": "500",
                "responseMessage": "Internal Error",
                "elapsed": 100,
                "responseBody": "failed",
                "errorType": "500",
            }
        ),
        encoding="utf-8",
    )
    rid = await _insert_report(db, name="internal-error-samples", artifact_dir=str(artifact_dir))

    resp = await auth_client.get(f"/report/errorSamples/{rid}")

    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["groups"] == {"500": 1}
    assert data["list"][0]["label"] == "内部错误"


@pytest.mark.asyncio
async def test_report_error_samples_backfills_missing_error_types_from_jtl(
    auth_client: AsyncClient, db: AsyncSession, tmp_path
) -> None:
    report_root = tmp_path / "report" / "2026-06-26-13:39:25"
    artifact_dir = report_root / "artifacts"
    error_dir = report_root / "_internal" / "error_samples"
    jtl_dir = report_root / "jtl"
    artifact_dir.mkdir(parents=True)
    error_dir.mkdir(parents=True)
    jtl_dir.mkdir()
    (error_dir / "error_samples.jsonl").write_text(
        json.dumps({
            "sampleTime": 1700000000000,
            "label": "4_资源上传",
            "responseCode": "403",
            "failureMessage": "Test failed",
            "errorType": "403",
        })
        + "\n",
        encoding="utf-8",
    )
    (jtl_dir / "sample.jtl").write_text(
        "\n".join(
            [
                "timeStamp,elapsed,label,responseCode,responseMessage,threadName,dataType,success,failureMessage,bytes,sentBytes,grpThreads,allThreads,URL,Latency,IdleTime,Connect",
                "1700000001000,20,5_资源下载,404,,tg 1-1,text,false,The result was the wrong size,842,100,1,1,https://example.test/download,20,0,1",
                "1700000002000,30,6_获取设备信息,200,,tg 1-2,text,false,\"Test failed: text expected to contain /\"\"status\"\":2000/\",99,100,1,1,https://example.test/device,30,0,1",
                "1700000003000,10,7_正常接口,200,,tg 1-3,text,true,,99,100,1,1,https://example.test/ok,10,0,1",
            ]
        ),
        encoding="utf-8",
    )
    rid = await _insert_report(
        db,
        name="error-samples-jtl-backfill",
        report_dir=str(report_root / "data") + os.sep,
        artifact_dir=str(artifact_dir),
    )

    resp = await auth_client.get(f"/report/errorSamples/{rid}")

    assert resp.json()["code"] == 0
    data = resp.json()["data"]
    assert data["total"] == 3
    assert data["groups"] == {"403": 1, "404": 1, "ASSERTION_FAILED": 1}
    labels = {item["label"]: item for item in data["list"]}
    assert labels["5_资源下载"]["errorType"] == "404"
    assert labels["5_资源下载"]["requestUrl"] == "https://example.test/download"
    assert labels["6_获取设备信息"]["errorType"] == "ASSERTION_FAILED"


@pytest.mark.asyncio
async def test_report_error_samples_reads_error_xml_details(
    auth_client: AsyncClient, db: AsyncSession, tmp_path
) -> None:
    report_root = tmp_path / "report" / "2026-06-26-13:39:25"
    artifact_dir = report_root / "artifacts"
    error_dir = report_root / "_internal" / "error_samples"
    artifact_dir.mkdir(parents=True)
    error_dir.mkdir(parents=True)
    (error_dir / "error_samples.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<testResults version="1.2">
<httpSample t="120" it="0" lt="100" ct="10" ts="1700000004000" s="false" lb="登录接口" rc="403" rm="Forbidden" tn="tg 1-1" dt="text" by="99" sby="88" ng="1" na="1">
  <assertionResult>
    <name>业务断言</name>
    <failure>true</failure>
    <error>false</error>
    <failureMessage>登录失败</failureMessage>
  </assertionResult>
  <responseData class="java.lang.String">{&quot;status&quot;:6015}</responseData>
  <samplerData>GET https://example.test/login</samplerData>
  <requestHeader>Authorization: Bearer abc
Cookie: sid=123</requestHeader>
  <responseHeader>HTTP/1.1 403 Forbidden</responseHeader>
  <java.net.URL>https://example.test/login</java.net.URL>
</httpSample>
</testResults>
""",
        encoding="utf-8",
    )
    rid = await _insert_report(
        db,
        name="error-samples-xml",
        report_dir=str(report_root / "data") + os.sep,
        artifact_dir=str(artifact_dir),
    )

    resp = await auth_client.get(f"/report/errorSamples/{rid}")

    assert resp.json()["code"] == 0
    item = resp.json()["data"]["list"][0]
    assert item["label"] == "登录接口"
    assert item["errorType"] == "403"
    assert item["requestUrl"] == "https://example.test/login"
    assert "Bearer abc" not in item["requestHeaders"]
    assert "******" in item["requestHeaders"]
    assert item["responseHeaders"] == "HTTP/1.1 403 Forbidden"
    assert item["responseBody"] == '{"status":6015}'


@pytest.mark.asyncio
async def test_report_resource_metrics_queries_prometheus(
    auth_client: AsyncClient,
    db: AsyncSession,
    monkeypatch,
) -> None:
    db.add(Config(config_key="PROMETHEUS_BASE_URL", config_value="http://prometheus.example"))
    await db.commit()
    rid = await _insert_report(
        db,
        name="resource-metrics",
        exec_type=ExecType.EXEC.value,
        grafana_instance="10.10.27.42:9200",
    )
    captured: list[dict] = []

    async def fake_query(base_url, query, start, end, step, timeout):
        captured.append({
            "base_url": base_url,
            "query": query,
            "start": start,
            "end": end,
            "step": step,
            "timeout": timeout,
        })
        return [{"timestamp": "10:00:00", "timestamp_ms": 1717476000000, "value": 12.5}]

    monkeypatch.setattr(prometheus_service, "_query_prometheus_range", fake_query)

    resp = await auth_client.get(f"/report/resourceMetrics/{rid}?step=30")
    body = resp.json()

    assert body["code"] == 0
    data = body["data"]
    assert data["reportId"] == rid
    assert data["instance"] == "10.10.27.42:9200"
    assert data["step"] == 30
    assert data["series"]["cpu"][0]["value"] == 12.5
    assert data["series"]["diskUtil"][0]["value"] == 12.5
    assert captured
    assert all(item["base_url"] == "http://prometheus.example" for item in captured)
    assert any('instance="10.10.27.42:9200"' in item["query"] for item in captured)
    disk_queries = [item["query"] for item in captured if "node_disk_" in item["query"]]
    assert any("irate(node_disk_read_bytes_total" in query for query in disk_queries)
    assert any("irate(node_disk_written_bytes_total" in query for query in disk_queries)
    assert any("irate(node_disk_io_time_seconds_total" in query for query in disk_queries)
    assert all('device!~"^(dm-|loop|ram|fd|sr).*"' in query for query in disk_queries)


@pytest.mark.asyncio
async def test_report_resource_metrics_falls_back_to_dm_disk_queries_when_excluded_empty(
    auth_client: AsyncClient,
    db: AsyncSession,
    monkeypatch,
) -> None:
    db.add(Config(config_key="PROMETHEUS_BASE_URL", config_value="http://prometheus.example"))
    await db.commit()
    rid = await _insert_report(
        db,
        name="resource-metrics-dm-fallback",
        exec_type=ExecType.EXEC.value,
        grafana_instance="10.10.27.42:9200",
    )
    captured: list[str] = []

    async def fake_query(base_url, query, start, end, step, timeout):
        captured.append(query)
        if "node_disk_" in query and 'device!~"^(dm-|loop|ram|fd|sr).*"' in query:
            return []
        return [{"timestamp": "10:00:00", "timestamp_ms": 1717476000000, "value": 12.5}]

    monkeypatch.setattr(prometheus_service, "_query_prometheus_range", fake_query)

    resp = await auth_client.get(f"/report/resourceMetrics/{rid}?step=30")
    body = resp.json()

    assert body["code"] == 0
    assert body["data"]["series"]["diskRead"][0]["value"] == 12.5
    assert body["data"]["series"]["diskWrite"][0]["value"] == 12.5
    assert body["data"]["series"]["diskUtil"][0]["value"] == 12.5
    excluded_disk_queries = [
        query for query in captured
        if "node_disk_" in query and 'device!~"^(dm-|loop|ram|fd|sr).*"' in query
    ]
    fallback_disk_queries = [
        query for query in captured
        if "node_disk_" in query and 'device!~"^(dm-|loop|ram|fd|sr).*"' not in query
    ]
    assert len(excluded_disk_queries) == 3
    assert len(fallback_disk_queries) == 3
    assert any("node_disk_read_bytes_total" in query for query in fallback_disk_queries)
    assert any("node_disk_written_bytes_total" in query for query in fallback_disk_queries)
    assert any("node_disk_io_time_seconds_total" in query for query in fallback_disk_queries)


@pytest.mark.asyncio
async def test_report_resource_metrics_uses_configured_step_when_request_omits_step(
    auth_client: AsyncClient,
    db: AsyncSession,
    monkeypatch,
) -> None:
    db.add(Config(config_key="PROMETHEUS_BASE_URL", config_value="http://prometheus.example"))
    db.add(Config(config_key="PROMETHEUS_STEP_SECONDS", config_value="45"))
    await db.commit()
    rid = await _insert_report(
        db,
        name="resource-metrics-config-step",
        exec_type=ExecType.EXEC.value,
        grafana_instance="10.10.27.42:9200",
    )
    captured: list[int] = []

    async def fake_query(base_url, query, start, end, step, timeout):
        captured.append(step)
        return [{"timestamp": "10:00:00", "timestamp_ms": 1717476000000, "value": 12.5}]

    monkeypatch.setattr(prometheus_service, "_query_prometheus_range", fake_query)

    resp = await auth_client.get(f"/report/resourceMetrics/{rid}")
    body = resp.json()

    assert body["code"] == 0
    assert body["data"]["step"] == 45
    assert captured
    assert set(captured) == {45}


@pytest.mark.asyncio
async def test_report_resource_metrics_queries_prometheus_metrics_concurrently(
    auth_client: AsyncClient,
    db: AsyncSession,
    monkeypatch,
) -> None:
    db.add(Config(config_key="PROMETHEUS_BASE_URL", config_value="http://prometheus.example"))
    await db.commit()
    rid = await _insert_report(
        db,
        name="resource-metrics-concurrent",
        exec_type=ExecType.EXEC.value,
        grafana_instance="10.10.27.42:9200",
    )
    active = 0
    max_active = 0

    async def fake_query(base_url, query, start, end, step, timeout):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return [{"timestamp": "10:00:00", "timestamp_ms": 1717476000000, "value": 12.5}]

    monkeypatch.setattr(prometheus_service, "_query_prometheus_range", fake_query)

    resp = await auth_client.get(f"/report/resourceMetrics/{rid}?step=30")
    body = resp.json()

    assert body["code"] == 0
    assert set(body["data"]["series"]) == set(report_service.DEFAULT_PROMETHEUS_RESOURCE_METRICS)
    assert max_active > 1


@pytest.mark.asyncio
async def test_report_resource_metrics_reuses_short_ttl_cache_for_same_instance(
    auth_client: AsyncClient,
    db: AsyncSession,
    monkeypatch,
) -> None:
    prometheus_service._RESOURCE_METRICS_CACHE.clear()
    db.add(Config(config_key="PROMETHEUS_BASE_URL", config_value="http://prometheus.example"))
    await db.commit()
    rid = await _insert_report(
        db,
        name="resource-metrics-cache",
        exec_type=ExecType.EXEC.value,
        grafana_instance="10.10.27.42:9200",
    )
    captured: list[str] = []

    async def fake_query(base_url, query, start, end, step, timeout):
        captured.append(query)
        return [{"timestamp": "10:00:00", "timestamp_ms": 1717476000000, "value": 12.5}]

    monkeypatch.setattr(prometheus_service, "_query_prometheus_range", fake_query)

    first = await auth_client.get(f"/report/resourceMetrics/{rid}?step=30")
    second = await auth_client.get(f"/report/resourceMetrics/{rid}?step=30")

    assert first.json()["code"] == 0
    assert second.json()["code"] == 0
    assert len(captured) == len(report_service.DEFAULT_PROMETHEUS_RESOURCE_METRICS)


@pytest.mark.asyncio
async def test_report_resource_metrics_force_refresh_bypasses_short_ttl_cache(
    auth_client: AsyncClient,
    db: AsyncSession,
    monkeypatch,
) -> None:
    prometheus_service._RESOURCE_METRICS_CACHE.clear()
    db.add(Config(config_key="PROMETHEUS_BASE_URL", config_value="http://prometheus.example"))
    await db.commit()
    rid = await _insert_report(
        db,
        name="resource-metrics-cache-refresh",
        exec_type=ExecType.EXEC.value,
        grafana_instance="10.10.27.42:9200",
    )
    captured: list[str] = []

    async def fake_query(base_url, query, start, end, step, timeout):
        captured.append(query)
        return [{"timestamp": "10:00:00", "timestamp_ms": 1717476000000, "value": 12.5}]

    monkeypatch.setattr(prometheus_service, "_query_prometheus_range", fake_query)

    first = await auth_client.get(f"/report/resourceMetrics/{rid}?step=30")
    second = await auth_client.get(f"/report/resourceMetrics/{rid}?step=30&forceRefresh=true")

    assert first.json()["code"] == 0
    assert second.json()["code"] == 0
    assert len(captured) == len(report_service.DEFAULT_PROMETHEUS_RESOURCE_METRICS) * 2


@pytest.mark.asyncio
async def test_report_resource_targets_from_resource_group_map(
    auth_client: AsyncClient,
    db: AsyncSession,
) -> None:
    db.add(
        Config(
            config_key="PROMETHEUS_RESOURCE_GROUP_MAP",
            config_value=json.dumps(
                {
                    "EMM-API": {
                        "name": "EMM-API压测资源组",
                        "targets": [
                            {
                                "name": "EMM-API",
                                "role": "被压服务",
                                "service": "EMM-API",
                                "instance": "10.10.27.42:9200",
                            },
                            {
                                "name": "EMM-CORE",
                                "role": "依赖服务",
                                "service": "EMM-CORE",
                                "instance": "10.10.27.43:9200",
                            },
                            {
                                "name": "MYSQL",
                                "role": "数据库",
                                "service": "MYSQL",
                                "instance": "10.8.83.145:9200",
                            },
                        ],
                    }
                },
                ensure_ascii=False,
            ),
        )
    )
    await db.commit()
    rid = await _insert_report(db, name="emm-api-run", service_name="EMM-API")

    resp = await auth_client.get(f"/report/resourceTargets/{rid}")
    body = resp.json()

    assert body["code"] == 0
    assert body["data"] == [
        {
            "name": "EMM-API",
            "role": "被压服务",
            "service": "EMM-API",
            "instance": "10.10.27.42:9200",
        },
        {
            "name": "EMM-CORE",
            "role": "依赖服务",
            "service": "EMM-CORE",
            "instance": "10.10.27.43:9200",
        },
        {
            "name": "MYSQL",
            "role": "数据库",
            "service": "MYSQL",
            "instance": "10.8.83.145:9200",
        },
    ]


@pytest.mark.asyncio
async def test_report_resource_targets_from_resource_group_map_string_list(
    auth_client: AsyncClient,
    db: AsyncSession,
) -> None:
    db.add(
        Config(
            config_key="PROMETHEUS_RESOURCE_GROUP_MAP",
            config_value=json.dumps(
                {
                    "EMM-API": [
                        "10.10.27.42:9200",
                        "10.10.27.43:9200",
                    ]
                },
                ensure_ascii=False,
            ),
        )
    )
    await db.commit()
    rid = await _insert_report(db, name="emm-api-run", service_name="EMM-API")

    resp = await auth_client.get(f"/report/resourceTargets/{rid}")
    body = resp.json()

    assert body["code"] == 0
    assert body["data"] == [
        {
            "name": "EMM-API-1",
            "role": "相关服务",
            "service": "EMM-API",
            "instance": "10.10.27.42:9200",
        },
        {
            "name": "EMM-API-2",
            "role": "相关服务",
            "service": "EMM-API",
            "instance": "10.10.27.43:9200",
        },
    ]


@pytest.mark.asyncio
async def test_report_resource_targets_invalid_resource_group_falls_back(
    auth_client: AsyncClient,
    db: AsyncSession,
) -> None:
    db.add_all(
        [
            Config(
                config_key="PROMETHEUS_RESOURCE_GROUP_MAP",
                config_value=json.dumps(
                    {
                        "EMM-API": [
                            123,
                            "",
                            {"name": "missing-instance"},
                        ]
                    },
                    ensure_ascii=False,
                ),
            ),
            Config(
                config_key="PROMETHEUS_DEFAULT_INSTANCE",
                config_value="10.10.27.99:9200",
            ),
        ]
    )
    await db.commit()
    rid = await _insert_report(db, name="emm-api-run", service_name="EMM-API")

    resp = await auth_client.get(f"/report/resourceTargets/{rid}")
    body = resp.json()

    assert body["code"] == 0
    assert body["data"] == [
        {
            "name": "EMM-API",
            "role": "被压服务",
            "service": "EMM-API",
            "instance": "10.10.27.99:9200",
        }
    ]


@pytest.mark.asyncio
async def test_report_resource_targets_fallback_to_single_instance(
    auth_client: AsyncClient,
    db: AsyncSession,
) -> None:
    rid = await _insert_report(
        db,
        name="single-target-run",
        service_name="EMM-API",
        grafana_instance="10.10.27.42:9200",
    )

    resp = await auth_client.get(f"/report/resourceTargets/{rid}")
    body = resp.json()

    assert body["code"] == 0
    assert body["data"] == [
        {
            "name": "EMM-API",
            "role": "被压服务",
            "service": "EMM-API",
            "instance": "10.10.27.42:9200",
        }
    ]


@pytest.mark.asyncio
async def test_report_resource_metrics_uses_requested_instance(
    auth_client: AsyncClient,
    db: AsyncSession,
    monkeypatch,
) -> None:
    db.add(Config(config_key="PROMETHEUS_BASE_URL", config_value="http://prometheus.example"))
    await db.commit()
    rid = await _insert_report(
        db,
        name="resource-metrics-requested-instance",
        exec_type=ExecType.EXEC.value,
        grafana_instance="10.10.27.42:9200",
    )
    captured: list[dict] = []

    async def fake_query(base_url, query, start, end, step, timeout):
        captured.append({"query": query, "step": step})
        return [{"timestamp": "10:00:00", "timestamp_ms": 1717476000000, "value": 1.0}]

    monkeypatch.setattr(prometheus_service, "_query_prometheus_range", fake_query)

    resp = await auth_client.get(
        f"/report/resourceMetrics/{rid}",
        params={"instance": "10.10.27.43:9200", "step": 60},
    )
    body = resp.json()

    assert body["code"] == 0
    assert body["data"]["instance"] == "10.10.27.43:9200"
    assert body["data"]["step"] == 60
    assert any('instance="10.10.27.43:9200"' in item["query"] for item in captured)
    assert not any('instance="10.10.27.42:9200"' in item["query"] for item in captured)


@pytest.mark.asyncio
async def test_report_resource_metrics_prefers_prometheus_map_over_grafana_snapshot(
    auth_client: AsyncClient,
    db: AsyncSession,
    monkeypatch,
) -> None:
    db.add(Config(config_key="PROMETHEUS_BASE_URL", config_value="http://prometheus.example"))
    db.add(
        Config(
            config_key="PROMETHEUS_INSTANCE_MAP",
            config_value='{"EMM-API":"10.10.27.43:9200"}',
        )
    )
    await db.commit()
    rid = await _insert_report(
        db,
        name="resource-metrics-prometheus-map",
        exec_type=ExecType.EXEC.value,
        service_name="EMM-API",
        grafana_instance="10.10.27.42:9200",
    )
    captured: list[str] = []

    async def fake_query(base_url, query, start, end, step, timeout):
        captured.append(query)
        return [{"timestamp": "10:00:00", "timestamp_ms": 1717476000000, "value": 1.0}]

    monkeypatch.setattr(prometheus_service, "_query_prometheus_range", fake_query)

    resp = await auth_client.get(f"/report/resourceMetrics/{rid}?step=30")
    body = resp.json()

    assert body["code"] == 0
    assert body["data"]["instance"] == "10.10.27.43:9200"
    assert any('instance="10.10.27.43:9200"' in item for item in captured)
    assert not any('instance="10.10.27.42:9200"' in item for item in captured)


@pytest.mark.asyncio
async def test_report_resource_metrics_uses_execution_run_time_range(
    auth_client: AsyncClient,
    db: AsyncSession,
    monkeypatch,
) -> None:
    db.add(Config(config_key="PROMETHEUS_BASE_URL", config_value="http://prometheus.example"))
    await db.commit()
    rid = await _insert_report(
        db,
        name="resource-metrics-run-time",
        exec_type=ExecType.EXEC.value,
        service_name="EMM-API",
        grafana_instance="10.10.27.42:9200",
    )
    report = await db.get(Report, rid)
    assert report is not None
    report.create_time = datetime(2026, 6, 3, 11, 41, 21)
    report.modify_time = datetime(2026, 6, 3, 11, 41, 21)
    db.add(
        ExecutionRun(
            report_id=rid,
            test_case_id=report.test_case_id,
            status="success",
            started_at=datetime(2026, 6, 3, 11, 41, 21),
            finished_at=datetime(2026, 6, 3, 11, 51, 48),
        )
    )
    await db.commit()
    captured: list[dict] = []

    async def fake_query(base_url, query, start, end, step, timeout):
        captured.append({"start": start, "end": end})
        return [{"timestamp": "10:00:00", "timestamp_ms": 1717476000000, "value": 1.0}]

    monkeypatch.setattr(prometheus_service, "_query_prometheus_range", fake_query)

    resp = await auth_client.get(f"/report/resourceMetrics/{rid}?step=30")
    data = resp.json()["data"]

    expected_from = report_service._to_epoch_ms(datetime(2026, 6, 3, 11, 41, 21) - timedelta(minutes=15))
    expected_to = report_service._to_epoch_ms(datetime(2026, 6, 3, 11, 51, 48) + timedelta(minutes=15))
    assert data["fromMs"] == expected_from
    assert data["toMs"] == expected_to
    assert captured
    assert all(item["start"] == expected_from // 1000 for item in captured)
    assert all(item["end"] == expected_to // 1000 for item in captured)


@pytest.mark.asyncio
async def test_report_resource_metrics_caps_prometheus_query_end_to_stable_now(
    auth_client: AsyncClient,
    db: AsyncSession,
    monkeypatch,
) -> None:
    db.add(Config(config_key="PROMETHEUS_BASE_URL", config_value="http://prometheus.example"))
    db.add(Config(config_key="GRAFANA_FROM_OFFSET_MINUTES", config_value="5"))
    db.add(Config(config_key="GRAFANA_TO_OFFSET_MINUTES", config_value="5"))
    await db.commit()
    rid = await _insert_report(
        db,
        name="resource-metrics-cap-future",
        exec_type=ExecType.EXEC.value,
        service_name="EMM-API",
        grafana_instance="10.10.27.42:9200",
    )
    report = await db.get(Report, rid)
    assert report is not None
    db.add(
        ExecutionRun(
            report_id=rid,
            test_case_id=report.test_case_id,
            status="success",
            started_at=datetime(2026, 6, 4, 15, 47, 34),
            finished_at=datetime(2026, 6, 4, 15, 52, 46),
        )
    )
    await db.commit()
    captured: list[dict] = []

    async def fake_query(base_url, query, start, end, step, timeout):
        captured.append({"start": start, "end": end, "step": step})
        return [{"timestamp": "15:54:00", "timestamp_ms": 1780559640000, "value": 1.0}]

    monkeypatch.setattr(prometheus_service, "_query_prometheus_range", fake_query)
    monkeypatch.setattr(
        prometheus_service.time,
        "time",
        lambda: report_service._to_epoch_ms(datetime(2026, 6, 4, 15, 55, 0)) / 1000,
    )

    resp = await auth_client.get(f"/report/resourceMetrics/{rid}?step=30")
    data = resp.json()["data"]

    expected_to = report_service._to_epoch_ms(datetime(2026, 6, 4, 15, 57, 46))
    stable_query_end = report_service._to_epoch_ms(datetime(2026, 6, 4, 15, 54, 0)) // 1000
    assert data["toMs"] == expected_to
    assert captured
    assert all(item["end"] == stable_query_end for item in captured)


@pytest.mark.asyncio
async def test_report_resource_metrics_requires_prometheus_config(
    auth_client: AsyncClient,
    db: AsyncSession,
) -> None:
    rid = await _insert_report(db, name="resource-no-config", exec_type=ExecType.EXEC.value)

    resp = await auth_client.get(f"/report/resourceMetrics/{rid}")
    body = resp.json()

    assert body["code"] == -1
    assert "PROMETHEUS_BASE_URL" in body["message"]


@pytest.mark.asyncio
async def test_report_transaction_stats_reads_jmeter_statistics_json(
    auth_client: AsyncClient,
    db: AsyncSession,
    tmp_path,
) -> None:
    report_dir = tmp_path / "2026-06-04-10:00:00" / "data"
    report_dir.mkdir(parents=True)
    (report_dir / "statistics.json").write_text(
        json.dumps(
            {
                "Total": {
                    "transaction": "Total",
                    "sampleCount": 100,
                    "errorCount": 3,
                    "meanResTime": 120.5,
                    "minResTime": 20.0,
                    "maxResTime": 900.0,
                    "throughput": 10.25,
                },
                "login": {
                    "transaction": "login",
                    "sampleCount": 40,
                    "errorCount": 1,
                    "meanResTime": 80.0,
                    "minResTime": 15.0,
                    "maxResTime": 300.0,
                    "throughput": 4.0,
                },
                "query": {
                    "transaction": "query",
                    "sampleCount": 60,
                    "errorCount": 2,
                    "meanResTime": 147.5,
                    "minResTime": 30.0,
                    "maxResTime": 900.0,
                    "throughput": 6.25,
                },
            }
        ),
        encoding="utf-8",
    )
    rid = await _insert_report(
        db,
        name="transaction-statistics-json",
        exec_type=ExecType.EXEC.value,
        status=TestCaseStatus.RUN_SUCCESS.value,
        report_dir=str(report_dir),
    )

    resp = await auth_client.get(f"/report/transactionStats/{rid}")
    body = resp.json()

    assert body["code"] == 0
    assert body["data"] == [
        {
            "name": "Total",
            "samples": 100,
            "success": 97,
            "failed": 3,
            "successRate": 97.0,
            "tps": 10.25,
            "avgRt": 120.5,
            "maxRt": 900.0,
            "minRt": 20.0,
            "ratio": 100.0,
        },
        {
            "name": "login",
            "samples": 40,
            "success": 39,
            "failed": 1,
            "successRate": 97.5,
            "tps": 4.0,
            "avgRt": 80.0,
            "maxRt": 300.0,
            "minRt": 15.0,
            "ratio": 40.0,
        },
        {
            "name": "query",
            "samples": 60,
            "success": 58,
            "failed": 2,
            "successRate": 96.67,
            "tps": 6.25,
            "avgRt": 147.5,
            "maxRt": 900.0,
            "minRt": 30.0,
            "ratio": 60.0,
        },
    ]


@pytest.mark.asyncio
async def test_report_transaction_stats_fallbacks_to_jtl_grouped_by_label(
    auth_client: AsyncClient,
    db: AsyncSession,
    tmp_path,
) -> None:
    report_root = tmp_path / "2026-06-04-11:00:00"
    report_dir = report_root / "data"
    jtl_dir = report_root / "jtl"
    report_dir.mkdir(parents=True)
    jtl_dir.mkdir()
    (jtl_dir / "result.jtl").write_text(
        "\n".join(
            [
                "timeStamp,elapsed,label,success,allThreads,grpThreads",
                "1700000000000,100,login,true,1,1",
                "1700000001000,200,login,false,1,1",
                "1700000002000,300,query,true,1,1",
            ]
        ),
        encoding="utf-8",
    )
    rid = await _insert_report(
        db,
        name="transaction-jtl",
        exec_type=ExecType.EXEC.value,
        status=TestCaseStatus.RUN_SUCCESS.value,
        report_dir=str(report_dir),
    )

    resp = await auth_client.get(f"/report/transactionStats/{rid}")
    data = resp.json()["data"]

    assert [item["name"] for item in data] == ["Total", "login", "query"]
    assert data[0]["samples"] == 3
    assert data[0]["success"] == 2
    assert data[0]["failed"] == 1
    assert data[1]["samples"] == 2
    assert data[1]["successRate"] == 50.0
    assert data[1]["avgRt"] == 150.0
    assert data[1]["ratio"] == 66.67
    assert data[2]["samples"] == 1
    assert data[2]["ratio"] == 33.33


@pytest.mark.asyncio
async def test_report_transaction_stats_prefers_jtl_over_inconsistent_statistics_total(
    auth_client: AsyncClient,
    db: AsyncSession,
    tmp_path,
) -> None:
    report_root = tmp_path / "2026-07-15-16:28:38"
    report_dir = report_root / "data"
    jtl_dir = report_root / "jtl"
    report_dir.mkdir(parents=True)
    jtl_dir.mkdir()
    (report_dir / "statistics.json").write_text(
        json.dumps(
            {
                "Total": {"transaction": "Total", "sampleCount": 59, "errorCount": 0},
                "LDAP事务": {
                    "transaction": "LDAP事务",
                    "sampleCount": 3,
                    "errorCount": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    (jtl_dir / "result.jtl").write_text(
        "\n".join(
            [
                "timeStamp,elapsed,label,success,allThreads,grpThreads,responseMessage",
                '1700000000000,30,LDAP事务,true,1,1,"Number of samples in transaction : 2"',
                '1700000000030,40,LDAP事务,true,1,1,"Number of samples in transaction : 2"',
                '1700000000070,50,LDAP事务,true,1,1,"Number of samples in transaction : 2"',
            ]
        ),
        encoding="utf-8",
    )
    rid = await _insert_report(
        db,
        name="transaction-jtl-source-of-truth",
        exec_type=ExecType.EXEC.value,
        status=TestCaseStatus.RUN_SUCCESS.value,
        report_dir=str(report_dir),
    )

    resp = await auth_client.get(f"/report/transactionStats/{rid}")
    data = resp.json()["data"]

    assert [item["name"] for item in data] == ["Total", "LDAP事务"]
    assert data[0]["samples"] == 3
    assert data[0]["success"] == 3
    assert data[0]["ratio"] == 100.0
    assert data[1]["samples"] == 3
    assert data[1]["ratio"] == 100.0


@pytest.mark.asyncio
async def test_generate_transaction_snapshots_removes_stale_labels(
    db: AsyncSession,
    tmp_path,
) -> None:
    report_root = tmp_path / "2026-07-15-17:00:00"
    report_dir = report_root / "data"
    jtl_dir = report_root / "jtl"
    report_dir.mkdir(parents=True)
    jtl_dir.mkdir()
    (jtl_dir / "result.jtl").write_text(
        "\n".join(
            [
                "timeStamp,elapsed,label,success,allThreads,grpThreads",
                "1700000000000,30,父事务,true,1,1",
            ]
        ),
        encoding="utf-8",
    )
    rid = await _insert_report(db, name="transaction-stale-label", report_dir=str(report_dir))
    db.add(
        ReportTransactionSnapshot(
            report_id=rid,
            transaction_name="旧子请求",
            samples=99,
            creator="system",
            creator_id="0",
            modifier="system",
            modifier_id="0",
        )
    )
    await db.commit()

    await report_service.generate_transaction_snapshots_for_report(db, rid)

    rows = (
        await db.execute(
            select(ReportTransactionSnapshot).where(ReportTransactionSnapshot.report_id == rid)
        )
    ).scalars().all()
    assert {row.transaction_name for row in rows} == {"Total", "父事务"}


@pytest.mark.asyncio
async def test_report_transaction_metrics_groups_jtl_by_label_and_time_window(
    auth_client: AsyncClient,
    db: AsyncSession,
    tmp_path,
) -> None:
    report_root = tmp_path / "2026-06-04-12:00:00"
    report_dir = report_root / "data"
    jtl_dir = report_root / "jtl"
    report_dir.mkdir(parents=True)
    jtl_dir.mkdir()
    (jtl_dir / "result.jtl").write_text(
        "\n".join(
            [
                "timeStamp,elapsed,label,success,allThreads,grpThreads,threadName",
                "1700000000000,100,login,true,1,1,t-1",
                "1700000001000,300,login,false,1,1,t-2",
                "1700000060000,200,login,true,1,1,t-1",
                "1700000002000,80,query,true,1,1,t-1",
                "1700000061000,120,query,true,1,1,t-3",
            ]
        ),
        encoding="utf-8",
    )
    rid = await _insert_report(
        db,
        name="transaction-metrics-jtl",
        exec_type=ExecType.EXEC.value,
        status=TestCaseStatus.RUN_SUCCESS.value,
        report_dir=str(report_dir),
    )

    resp = await auth_client.get(f"/report/transactionMetrics/{rid}?window=60")
    body = resp.json()

    assert body["code"] == 0
    data = body["data"]
    assert data["reportId"] == rid
    assert data["window"] == 60
    assert data["transactions"] == ["login", "query"]
    assert data["series"]["login"] == [
        {
            "timestamp": "06:13:00",
            "qps": 0.03,
            "avgRt": 200.0,
            "p95Rt": 290.0,
            "p99Rt": 298.0,
            "errorRate": 50.0,
            "sampleCount": 2,
            "failCount": 1,
            "activeThreads": 2,
        },
        {
            "timestamp": "06:14:00",
            "qps": 0.02,
            "avgRt": 200.0,
            "p95Rt": 200.0,
            "p99Rt": 200.0,
            "errorRate": 0.0,
            "sampleCount": 1,
            "failCount": 0,
            "activeThreads": 1,
        },
    ]
    assert data["series"]["query"][0]["avgRt"] == 80.0
    assert data["series"]["query"][1]["avgRt"] == 120.0

    rows = (
        await db.execute(
            select(ReportTransactionMetricSnapshot).where(
                ReportTransactionMetricSnapshot.report_id == rid
            )
        )
    ).scalars().all()
    assert len(rows) == 4


@pytest.mark.asyncio
async def test_report_transaction_metrics_prefers_group_threads_without_thread_name(
    auth_client: AsyncClient,
    db: AsyncSession,
    tmp_path,
) -> None:
    report_root = tmp_path / "2026-06-04-12:15:00"
    report_dir = report_root / "data"
    jtl_dir = report_root / "jtl"
    report_dir.mkdir(parents=True)
    jtl_dir.mkdir()
    (jtl_dir / "result.jtl").write_text(
        "\n".join(
            [
                "timeStamp,elapsed,label,success,allThreads,grpThreads",
                "1700000000000,100,login,true,8,4",
                "1700000001000,300,login,true,9,4",
                "1700000002000,80,query,true,10,4",
            ]
        ),
        encoding="utf-8",
    )
    rid = await _insert_report(
        db,
        name="transaction-metrics-no-thread-name",
        exec_type=ExecType.EXEC.value,
        status=TestCaseStatus.RUN_SUCCESS.value,
        report_dir=str(report_dir),
    )

    resp = await auth_client.get(f"/report/transactionMetrics/{rid}?window=60")
    body = resp.json()

    assert body["code"] == 0
    data = body["data"]
    assert data["series"]["login"][0]["activeThreads"] == 4
    assert data["series"]["query"][0]["activeThreads"] == 4


@pytest.mark.asyncio
async def test_report_transaction_metrics_fallbacks_to_all_threads_when_group_threads_missing(
    auth_client: AsyncClient,
    db: AsyncSession,
    tmp_path,
) -> None:
    report_root = tmp_path / "2026-06-04-12:18:00"
    report_dir = report_root / "data"
    jtl_dir = report_root / "jtl"
    report_dir.mkdir(parents=True)
    jtl_dir.mkdir()
    (jtl_dir / "result.jtl").write_text(
        "\n".join(
            [
                "timeStamp,elapsed,label,success,allThreads,grpThreads",
                "1700000000000,100,login,true,8,",
                "1700000001000,300,login,true,9,0",
            ]
        ),
        encoding="utf-8",
    )
    rid = await _insert_report(
        db,
        name="transaction-metrics-no-group-threads",
        exec_type=ExecType.EXEC.value,
        status=TestCaseStatus.RUN_SUCCESS.value,
        report_dir=str(report_dir),
    )

    resp = await auth_client.get(f"/report/transactionMetrics/{rid}?window=60")
    body = resp.json()

    assert body["code"] == 0
    assert body["data"]["series"]["login"][0]["activeThreads"] == 9


@pytest.mark.asyncio
async def test_report_transaction_metrics_regenerates_zero_active_thread_snapshots(
    auth_client: AsyncClient,
    db: AsyncSession,
    tmp_path,
) -> None:
    report_root = tmp_path / "2026-06-04-12:20:00"
    report_dir = report_root / "data"
    jtl_dir = report_root / "jtl"
    report_dir.mkdir(parents=True)
    jtl_dir.mkdir()
    (jtl_dir / "result.jtl").write_text(
        "\n".join(
            [
                "timeStamp,elapsed,label,success,allThreads,grpThreads",
                "1700000000000,100,login,true,7,3",
            ]
        ),
        encoding="utf-8",
    )
    rid = await _insert_report(
        db,
        name="transaction-metrics-zero-backfill",
        exec_type=ExecType.EXEC.value,
        status=TestCaseStatus.RUN_SUCCESS.value,
        report_dir=str(report_dir),
    )
    db.add(
        ReportTransactionMetricSnapshot(
            report_id=rid,
            transaction_name="login",
            window_sec=60,
            bucket_start_ms=1699999980000,
            timestamp="06:13:00",
            qps=0.02,
            avg_rt=100,
            sample_count=1,
            active_threads=0,
        )
    )
    await db.commit()

    resp = await auth_client.get(f"/report/transactionMetrics/{rid}?window=60")
    body = resp.json()

    assert body["code"] == 0
    assert body["data"]["series"]["login"][0]["activeThreads"] == 3


@pytest.mark.asyncio
async def test_report_transaction_metrics_regenerates_legacy_all_threads_snapshots(
    auth_client: AsyncClient,
    db: AsyncSession,
    tmp_path,
) -> None:
    report_root = tmp_path / "2026-06-04-12:25:00"
    report_dir = report_root / "data"
    jtl_dir = report_root / "jtl"
    report_dir.mkdir(parents=True)
    jtl_dir.mkdir()
    (jtl_dir / "result.jtl").write_text(
        "\n".join(
            [
                "timeStamp,elapsed,label,success,allThreads,grpThreads",
                "1700000000000,100,login,true,30,5",
            ]
        ),
        encoding="utf-8",
    )
    rid = await _insert_report(
        db,
        name="transaction-metrics-legacy-all-threads",
        exec_type=ExecType.EXEC.value,
        status=TestCaseStatus.RUN_SUCCESS.value,
        report_dir=str(report_dir),
    )
    db.add(
        ReportTransactionMetricSnapshot(
            report_id=rid,
            transaction_name="login",
            window_sec=60,
            bucket_start_ms=1699999980000,
            timestamp="06:13:00",
            qps=0.02,
            avg_rt=100,
            sample_count=1,
            active_threads=30,
            modifier_id="0",
        )
    )
    await db.commit()

    resp = await auth_client.get(f"/report/transactionMetrics/{rid}?window=60")
    body = resp.json()

    assert body["code"] == 0
    assert body["data"]["series"]["login"][0]["activeThreads"] == 5


@pytest.mark.asyncio
async def test_report_transaction_trend_returns_split_chart_data(
    auth_client: AsyncClient,
    db: AsyncSession,
    tmp_path,
) -> None:
    report_root = tmp_path / "2026-06-04-12:30:00"
    report_dir = report_root / "data"
    jtl_dir = report_root / "jtl"
    report_dir.mkdir(parents=True)
    jtl_dir.mkdir()
    (jtl_dir / "result.jtl").write_text(
        "\n".join(
            [
                "timeStamp,elapsed,label,success,allThreads,grpThreads,threadName",
                "1700000000000,100,login,true,1,1,t-1",
                "1700000001000,300,login,false,1,1,t-2",
                "1700000002000,80,query,true,1,1,t-1",
                "1700000060000,200,login,true,1,1,t-1",
                "1700000061000,120,query,true,1,1,t-3",
            ]
        ),
        encoding="utf-8",
    )
    rid = await _insert_report(
        db,
        name="transaction-trend-jtl",
        exec_type=ExecType.EXEC.value,
        status=TestCaseStatus.RUN_SUCCESS.value,
        report_dir=str(report_dir),
    )

    resp = await auth_client.get(f"/report/transactionTrend/{rid}?window=60")
    body = resp.json()

    assert body["code"] == 0
    data = body["data"]
    assert data["reportId"] == rid
    assert data["window"] == 60
    assert data["timestamps"] == ["06:13:00", "06:14:00"]
    assert data["transactions"] == ["login", "query"]
    assert data["totalTps"] == [
        {"timestamp": "06:13:00", "value": 0.05},
        {"timestamp": "06:14:00", "value": 0.03},
    ]
    assert data["transactionTps"]["login"][0] == {"timestamp": "06:13:00", "value": 0.03}
    assert data["avgRt"]["login"][0] == {"timestamp": "06:13:00", "value": 200.0}
    assert data["activeThreads"]["login"][0] == {"timestamp": "06:13:00", "value": 2}
    assert data["activeThreads"]["query"][0] == {"timestamp": "06:13:00", "value": 1}


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
async def test_download_report_sanitizes_task_name_for_zip_path(
    auth_client: AsyncClient,
    db: AsyncSession,
    tmp_path,
) -> None:
    report_dir = str(tmp_path / "2026-05-13-11:30:00" / "data")
    os.makedirs(report_dir, exist_ok=True)
    (tmp_path / "2026-05-13-11:30:00" / "data" / "index.html").write_text("<html>report</html>")

    rid = await _insert_report(db, name="baseline/after", exec_type=ExecType.EXEC.value, report_dir=report_dir)
    resp = await auth_client.get(f"/report/download/{rid}")

    assert resp.status_code == 200
    assert "baseline_after.zip" in resp.headers["content-disposition"]


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


@pytest.mark.asyncio
async def test_grafana_url_defaults_to_15_minute_execution_window_padding(
    auth_client: AsyncClient,
    db: AsyncSession,
) -> None:
    db.add(
        Config(
            config_key="GRAFANA_DASHBOARD_URL",
            config_value="http://10.10.27.210:3000/d/StarsL-TenSunS-node/0d50bf8?orgId=1",
            description="grafana url",
        )
    )
    await db.commit()

    rid = await _insert_report(db, name="rpt", exec_type=ExecType.EXEC.value)
    report = await db.get(Report, rid)
    assert report is not None
    report.create_time = datetime(2026, 5, 27, 10, 0, 0)
    report.modify_time = datetime(2026, 5, 27, 10, 30, 0)
    await db.commit()

    resp = await auth_client.get(f"/report/grafana/{rid}")

    query = parse_qs(urlsplit(resp.json()["data"]).query)
    assert query["from"] == ["1779846300000"]
    assert query["to"] == ["1779849900000"]


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


def test_parse_jtl_metrics_uses_thread_names_when_distributed_all_threads_is_one(tmp_path) -> None:
    jtl = tmp_path / "result.jtl"
    rows = ["timeStamp,elapsed,success,allThreads,grpThreads,threadName"]
    for i in range(1, 51):
        rows.append(f"1700000000000,100,true,1,1,10.0.0.1:1099-query 1-{i}")
        rows.append(f"1700000000000,100,true,1,1,10.0.0.2:1099-query 1-{i}")
    jtl.write_text("\n".join(rows), encoding="utf-8")

    items = _parse_jtl_metrics(
        str(jtl),
        5,
        {"total_threads": 100, "slave_count": 2, "per_slave_threads": 50},
    )

    assert len(items) == 1
    assert items[0]["threads"] == 100


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


@pytest.mark.asyncio
async def test_get_jtl_metrics_persists_snapshot_and_reuses_it(
    db: AsyncSession,
    tmp_path,
    monkeypatch,
) -> None:
    report_root = tmp_path / "report" / "2026-05-31-16:00:00"
    data_dir = report_root / "data"
    jtl_dir = report_root / "jtl"
    data_dir.mkdir(parents=True)
    jtl_dir.mkdir()
    jtl = jtl_dir / "result.jtl"
    jtl.write_text(
        "\n".join(
            [
                "timeStamp,elapsed,success,allThreads,grpThreads",
                "1700000000000,100,true,10,10",
                "1700000001000,200,false,12,12",
            ]
        ),
        encoding="utf-8",
    )
    rid = await _insert_report(db, name="snapshot", report_dir=str(data_dir) + os.sep)

    await report_service.generate_metric_snapshots_for_report(db, rid, (5,))
    first = await report_service.get_jtl_metrics(db, rid, 5)

    snapshots = (
        await db.execute(
            select(ReportMetricSnapshot).where(
                ReportMetricSnapshot.report_id == rid,
                ReportMetricSnapshot.window_sec == 5,
            )
        )
    ).scalars().all()
    assert len(snapshots) == 1
    assert first[0]["qps"] == 0.4
    assert first[0]["avg_rt"] == 150.0
    assert first[0]["p95_rt"] > 0
    assert first[0]["p99_rt"] > 0
    assert first[0]["error_rate"] == 50.0

    def fail_find_jtl(report_dir: str):
        raise AssertionError("should read metrics snapshot instead of parsing JTL again")

    monkeypatch.setattr(report_metrics_service, "_find_jtl_file", fail_find_jtl)
    second = await report_service.get_jtl_metrics(db, rid, 5)

    assert second == first


@pytest.mark.asyncio
async def test_get_jtl_metrics_refreshes_running_report_snapshot(
    db: AsyncSession,
    tmp_path,
    monkeypatch,
) -> None:
    report_root = tmp_path / "report" / "2026-06-01-16:00:00"
    data_dir = report_root / "data"
    data_dir.mkdir(parents=True)
    rid = await _insert_report(
        db,
        name="snapshot_running",
        status=TestCaseStatus.RUN_ING.value,
        report_dir=str(data_dir) + os.sep,
    )
    db.add(
        ReportMetricSnapshot(
            report_id=rid,
            window_sec=5,
            bucket_start_ms=1700000000000,
            timestamp="10:00:00",
            qps=1.0,
        )
    )
    await db.commit()

    scheduled: list[tuple[int, tuple[int, ...]]] = []

    def fake_schedule(report_id: int, windows=(5,)) -> bool:
        scheduled.append((report_id, tuple(windows)))
        return True

    monkeypatch.setattr(report_metrics_service, "schedule_metric_snapshot_generation", fake_schedule)
    monkeypatch.setattr(report_metrics_service, "_metric_snapshot_last_refresh", {}, raising=False)

    items = await report_service.get_jtl_metrics(db, rid, 5)

    assert items
    assert scheduled == [(rid, (5,))]


@pytest.mark.asyncio
async def test_get_jtl_metrics_uses_configured_running_refresh_interval(
    db: AsyncSession,
    tmp_path,
    monkeypatch,
) -> None:
    db.add(Config(config_key="REPORT_RUNNING_METRIC_REFRESH_SECONDS", config_value="30"))
    report_root = tmp_path / "report" / "2026-06-01-16:03:00"
    data_dir = report_root / "data"
    data_dir.mkdir(parents=True)
    rid = await _insert_report(
        db,
        name="snapshot_running_config_interval",
        status=TestCaseStatus.RUN_ING.value,
        report_dir=str(data_dir) + os.sep,
    )
    db.add(
        ReportMetricSnapshot(
            report_id=rid,
            window_sec=5,
            bucket_start_ms=1700000000000,
            timestamp="10:00:00",
            qps=1.0,
        )
    )
    await db.commit()

    scheduled: list[tuple[int, tuple[int, ...]]] = []

    def fake_schedule(report_id: int, windows=(5,)) -> bool:
        scheduled.append((report_id, tuple(windows)))
        return True

    monkeypatch.setattr(report_metrics_service, "schedule_metric_snapshot_generation", fake_schedule)
    monkeypatch.setattr(report_metrics_service, "_metric_snapshot_last_refresh", {}, raising=False)

    report_metrics_service._metric_snapshot_last_refresh[(rid, 5)] = report_metrics_service.time.monotonic() - 11
    await report_service.get_jtl_metrics(db, rid, 5)
    assert scheduled == []

    report_metrics_service._metric_snapshot_last_refresh[(rid, 5)] = report_metrics_service.time.monotonic() - 31
    await report_service.get_jtl_metrics(db, rid, 5)
    assert scheduled == [(rid, (5,))]


@pytest.mark.asyncio
async def test_get_jtl_metrics_does_not_refresh_finished_snapshot(
    db: AsyncSession,
    tmp_path,
    monkeypatch,
) -> None:
    report_root = tmp_path / "report" / "2026-06-01-16:05:00"
    data_dir = report_root / "data"
    data_dir.mkdir(parents=True)
    rid = await _insert_report(
        db,
        name="snapshot_finished",
        status=TestCaseStatus.RUN_SUCCESS.value,
        report_dir=str(data_dir) + os.sep,
    )
    db.add(
        ReportMetricSnapshot(
            report_id=rid,
            window_sec=5,
            bucket_start_ms=1700000000000,
            timestamp="10:00:00",
            qps=1.0,
        )
    )
    await db.commit()

    scheduled: list[tuple[int, tuple[int, ...]]] = []

    def fake_schedule(report_id: int, windows=(5,)) -> bool:
        scheduled.append((report_id, tuple(windows)))
        return True

    monkeypatch.setattr(report_metrics_service, "schedule_metric_snapshot_generation", fake_schedule)
    monkeypatch.setattr(report_metrics_service, "_metric_snapshot_last_refresh", {}, raising=False)

    items = await report_service.get_jtl_metrics(db, rid, 5)

    assert items
    assert scheduled == []


@pytest.mark.asyncio
async def test_get_jtl_metrics_returns_running_jtl_when_snapshot_missing(
    db: AsyncSession,
    tmp_path,
) -> None:
    report_root = tmp_path / "report" / "2026-06-01-17:00:00"
    data_dir = report_root / "data"
    jtl_dir = report_root / "jtl"
    data_dir.mkdir(parents=True)
    jtl_dir.mkdir()
    (jtl_dir / "result.jtl").write_text(
        "\n".join(
            [
                "timeStamp,elapsed,success,allThreads,grpThreads",
                "1700000000000,100,true,10,10",
                "1700000001000,120,true,12,12",
            ]
        ),
        encoding="utf-8",
    )
    rid = await _insert_report(
        db,
        name="running_snapshot_missing",
        status=TestCaseStatus.RUN_ING.value,
        report_dir=str(data_dir) + os.sep,
    )

    items = await report_service.get_jtl_metrics(db, rid, 5)

    assert len(items) == 1
    assert items[0]["qps"] == 0.4
    assert items[0]["threads"] == 12
    snapshots = (
        await db.execute(
            select(ReportMetricSnapshot).where(
                ReportMetricSnapshot.report_id == rid,
                ReportMetricSnapshot.window_sec == 5,
            )
        )
    ).scalars().all()
    assert len(snapshots) == 1


@pytest.mark.asyncio
async def test_get_jtl_metrics_does_not_parse_jtl_on_request_when_snapshot_missing(
    db: AsyncSession,
    tmp_path,
    monkeypatch,
) -> None:
    report_root = tmp_path / "report" / "2026-06-01-15:00:00"
    data_dir = report_root / "data"
    jtl_dir = report_root / "jtl"
    data_dir.mkdir(parents=True)
    jtl_dir.mkdir()
    (jtl_dir / "result.jtl").write_text(
        "\n".join(
            [
                "timeStamp,elapsed,success,allThreads,grpThreads",
                "1700000000000,100,true,10,10",
            ]
        ),
        encoding="utf-8",
    )
    rid = await _insert_report(db, name="snapshot_missing", report_dir=str(data_dir) + os.sep)

    def fail_parse(*args, **kwargs):
        raise AssertionError("request path should not parse JTL")

    scheduled: list[tuple[int, tuple[int, ...]]] = []

    def fake_schedule(report_id: int, windows=(5,)) -> bool:
        scheduled.append((report_id, tuple(windows)))
        return True

    monkeypatch.setattr(report_metrics_service, "_parse_jtl_metrics", fail_parse)
    monkeypatch.setattr(report_metrics_service, "schedule_metric_snapshot_generation", fake_schedule)

    items = await report_service.get_jtl_metrics(db, rid, 5)

    assert items == []
    assert scheduled == [(rid, (5,))]


@pytest.mark.asyncio
async def test_generate_metric_snapshot_parses_jtl_in_worker_thread(
    db: AsyncSession,
    tmp_path,
    monkeypatch,
) -> None:
    report_root = tmp_path / "report" / "2026-06-01-15:10:00"
    data_dir = report_root / "data"
    jtl_dir = report_root / "jtl"
    data_dir.mkdir(parents=True)
    jtl_dir.mkdir()
    (jtl_dir / "result.jtl").write_text(
        "\n".join(
            [
                "timeStamp,elapsed,success,allThreads,grpThreads",
                "1700000000000,80,true,8,8",
            ]
        ),
        encoding="utf-8",
    )
    rid = await _insert_report(db, name="snapshot_thread", report_dir=str(data_dir) + os.sep)
    calls: list[str] = []
    real_to_thread = report_metrics_service.asyncio.to_thread

    async def tracking_to_thread(func, /, *args, **kwargs):
        calls.append(func.__name__)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(report_metrics_service.asyncio, "to_thread", tracking_to_thread)

    count = await report_service.generate_metric_snapshots_for_report(db, rid)

    assert count == 1
    assert "_parse_jtl_metrics" in calls


@pytest.mark.asyncio
async def test_generate_default_metric_snapshot_for_report(
    db: AsyncSession,
    tmp_path,
) -> None:
    report_root = tmp_path / "report" / "2026-05-31-16:10:00"
    data_dir = report_root / "data"
    jtl_dir = report_root / "jtl"
    data_dir.mkdir(parents=True)
    jtl_dir.mkdir()
    (jtl_dir / "result.jtl").write_text(
        "\n".join(
            [
                "timeStamp,elapsed,success,allThreads,grpThreads",
                "1700000000000,80,true,8,8",
                "1700000001000,120,true,9,9",
            ]
        ),
        encoding="utf-8",
    )
    rid = await _insert_report(db, name="snapshot_bg", report_dir=str(data_dir) + os.sep)

    count = await report_service.generate_metric_snapshots_for_report(db, rid)

    assert count == 1
    snapshots = (
        await db.execute(select(ReportMetricSnapshot).where(ReportMetricSnapshot.report_id == rid))
    ).scalars().all()
    assert len(snapshots) == 1


@pytest.mark.asyncio
async def test_metric_snapshot_save_is_idempotent_for_same_bucket(
    db: AsyncSession,
    tmp_path,
) -> None:
    report_root = tmp_path / "report" / "2026-06-01-10:00:00"
    data_dir = report_root / "data"
    jtl_dir = report_root / "jtl"
    data_dir.mkdir(parents=True)
    jtl_dir.mkdir()
    (jtl_dir / "result.jtl").write_text(
        "\n".join(
            [
                "timeStamp,elapsed,success,allThreads,grpThreads",
                "1700000000000,100,true,10,10",
                "1700000001000,200,true,10,10",
            ]
        ),
        encoding="utf-8",
    )
    rid = await _insert_report(db, name="snapshot_idempotent", report_dir=str(data_dir) + os.sep)

    await report_service.generate_metric_snapshots_for_report(db, rid)
    await report_service.generate_metric_snapshots_for_report(db, rid)

    snapshots = (
        await db.execute(
            select(ReportMetricSnapshot).where(
                ReportMetricSnapshot.report_id == rid,
                ReportMetricSnapshot.window_sec == 5,
            )
        )
    ).scalars().all()
    assert len(snapshots) == 1


@pytest.mark.asyncio
async def test_metric_snapshot_has_unique_bucket_constraint(db: AsyncSession) -> None:
    db.add(
        ReportMetricSnapshot(
            report_id=101,
            window_sec=5,
            bucket_start_ms=1700000000000,
            timestamp="10:00:00",
        )
    )
    await db.commit()

    db.add(
        ReportMetricSnapshot(
            report_id=101,
            window_sec=5,
            bucket_start_ms=1700000000000,
            timestamp="10:00:00",
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
