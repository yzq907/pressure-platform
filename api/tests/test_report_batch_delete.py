"""批量报告清理：真实测试库、临时文件、权限及逐项失败隔离。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.core.enums import TestCaseStatus as RunStatus
from app.core.exceptions import MysteriousException
from app.models.audit_log import AuditLog
from app.models.report import Report
from app.models.report_metric_snapshot import ReportMetricSnapshot
from app.models.report_transaction_metric_snapshot import ReportTransactionMetricSnapshot
from app.models.report_transaction_snapshot import ReportTransactionSnapshot
from app.models.role import Role
from app.models.user import User
from app.services import report as service


async def add_report(db, tmp_path, name="report", status=2):
    directory = tmp_path / name / "data"
    directory.mkdir(parents=True)
    (directory / "index.html").write_text("report")
    row = Report(name=name, status=status, report_dir=str(directory))
    db.add(row)
    await db.commit()
    return row.id, directory.parent


@pytest.mark.asyncio
async def test_batch_deletes_reports_and_files_and_audits_each(auth_client, db, tmp_path):
    first, first_dir = await add_report(db, tmp_path, "first")
    second, second_dir = await add_report(db, tmp_path, "second", status=3)
    for model in (ReportMetricSnapshot, ReportTransactionMetricSnapshot, ReportTransactionSnapshot):
        db.add(model(report_id=first))
        db.add(model(report_id=second))
    await db.commit()
    result = (await auth_client.post("/report/cleanBatch", json={"ids": [first, second]})).json()
    assert result["code"] == 0
    assert result["data"] == {
        "deletedIds": [first, second], "failures": [], "successCount": 2, "failedCount": 0,
    }
    assert not first_dir.exists()
    assert not second_dir.exists()
    db.expire_all()
    assert await db.get(Report, first) is None
    assert await db.get(Report, second) is None
    for model in (ReportMetricSnapshot, ReportTransactionMetricSnapshot, ReportTransactionSnapshot):
        assert list((await db.scalars(select(model))).all()) == []
    logs = list((await db.scalars(select(AuditLog).where(AuditLog.resource_type == "report"))).all())
    assert {item.resource_id for item in logs} == {first, second}
    assert all(item.action == "DELETE" and item.username == "test_admin" for item in logs)


@pytest.mark.asyncio
async def test_batch_mixed_results_deduplicate_and_keep_active_reports(auth_client, db, tmp_path):
    done, done_dir = await add_report(db, tmp_path, "done")
    running, running_dir = await add_report(db, tmp_path, "running", status=1)
    missing = 99999
    result = (await auth_client.post("/report/cleanBatch", json={"ids": [running, missing, done, done]})).json()
    assert result["code"] == 0
    data = result["data"]
    assert data["deletedIds"] == [done]
    assert data["successCount"] == 1
    assert data["failedCount"] == 2
    assert [failure["id"] for failure in data["failures"]] == [running, missing]
    assert all(failure["message"] for failure in data["failures"])
    assert running_dir.exists()
    assert not done_dir.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [1, 4, 5, 99])
@pytest.mark.parametrize("batch", [True, False])
async def test_active_or_unknown_status_is_not_deletable(auth_client, db, tmp_path, status, batch):
    report_id, directory = await add_report(db, tmp_path, status=status)
    if batch:
        result = (await auth_client.post("/report/cleanBatch", json={"ids": [report_id]})).json()
        assert result["code"] == 0
        assert result["data"]["deletedIds"] == []
        assert result["data"]["failedCount"] == 1
    else:
        result = (await auth_client.get(f"/report/clean/{report_id}")).json()
        assert result["code"] != 0
    assert directory.exists()
    db.expire_all()
    assert await db.get(Report, report_id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {}, {"ids": []}, {"ids": [0]}, {"ids": [-1]}, {"ids": [True]},
    {"ids": ["1"]}, {"ids": [1.1]}, {"ids": list(range(1, 102))},
])
async def test_batch_rejects_invalid_ids(auth_client, payload):
    result = (await auth_client.post("/report/cleanBatch", json=payload)).json()
    assert result["code"] != 0
    assert "参数" in result["message"]


@pytest.mark.asyncio
async def test_batch_requires_auth(client):
    result = (await client.post("/report/cleanBatch", json={"ids": [1]})).json()
    assert result["code"] == 1007


@pytest.mark.asyncio
async def test_batch_requires_report_permission(auth_client, db, tmp_path):
    report_id, directory = await add_report(db, tmp_path)
    user = (await db.scalars(select(User).where(User.username == "test_admin"))).one()
    role = Role(name="无报告权限", code="no_report", builtin=0)
    db.add(role)
    await db.flush()
    user.role_id = role.id
    await db.commit()
    result = (await auth_client.post("/report/cleanBatch", json={"ids": [report_id]})).json()
    assert result["code"] != 0
    assert "无权" in result["message"]
    assert directory.exists()


@pytest.mark.asyncio
async def test_batch_rolls_back_failed_item_and_continues(auth_client, db, tmp_path, monkeypatch):
    failed, failed_dir = await add_report(db, tmp_path, "failed")
    success, success_dir = await add_report(db, tmp_path, "success")
    db.add(ReportMetricSnapshot(report_id=failed, window_sec=5, bucket_start_ms=0))
    await db.commit()
    original = service.clean_report
    rollback = AsyncMock(wraps=db.rollback)
    monkeypatch.setattr(db, "rollback", rollback)

    async def fail_one(session, report_id):
        if report_id == failed:
            raise SQLAlchemyError("private SQL/password must not appear")
        return await original(session, report_id)

    monkeypatch.setattr(service, "clean_report", fail_one)
    # Direct service call lets us verify rollback on the exact session used.
    result = await service.clean_reports(db, [failed, success])
    assert result.deleted_ids == [success]
    assert result.failed_count == 1
    assert "private" not in result.failures[0].message
    assert rollback.await_count >= 1
    assert failed_dir.exists()
    assert not success_dir.exists()


@pytest.mark.asyncio
async def test_real_failed_transaction_is_rolled_back_before_next_item(db, tmp_path, monkeypatch):
    failed, failed_dir = await add_report(db, tmp_path, "failed-transaction")
    success, success_dir = await add_report(db, tmp_path, "next-transaction")
    db.add(ReportMetricSnapshot(report_id=failed, window_sec=5, bucket_start_ms=0))
    await db.commit()
    original = service.clean_report

    async def violate_unique(session, report_id):
        if report_id == failed:
            session.add(ReportMetricSnapshot(report_id=failed, window_sec=5, bucket_start_ms=0))
            await session.flush()
        return await original(session, report_id)

    monkeypatch.setattr(service, "clean_report", violate_unique)
    result = await service.clean_reports(db, [failed, success])
    assert result.deleted_ids == [success]
    assert result.failed_count == 1
    assert failed_dir.exists()
    assert not success_dir.exists()
    assert len(list((await db.scalars(select(ReportMetricSnapshot))).all())) == 1


@pytest.mark.asyncio
async def test_delete_reloads_status_instead_of_using_stale_identity_map(db, tmp_path):
    from sqlalchemy import update

    from tests.conftest import TestSessionLocal

    report_id, directory = await add_report(db, tmp_path)
    stale = await db.get(Report, report_id)
    assert stale.status == 2
    async with TestSessionLocal() as other:
        await other.execute(update(Report).where(Report.id == report_id).values(status=RunStatus.RUN_ING.value))
        await other.commit()
    with pytest.raises(MysteriousException, match="执行|删除"):
        await service.clean_report(db, report_id)
    assert directory.exists()


@pytest.mark.asyncio
async def test_failed_audit_does_not_poison_next_deleted_report_audit(auth_client, db, tmp_path, monkeypatch):
    from app.services import audit_log

    first, _ = await add_report(db, tmp_path, "audit-first")
    second, _ = await add_report(db, tmp_path, "audit-second")
    original = audit_log.add_log
    calls = 0

    async def fail_first_audit(session, param):
        nonlocal calls
        calls += 1
        if calls == 1:
            session.add(Role(name="duplicate", code="admin"))
            await session.flush()
        return await original(session, param)

    monkeypatch.setattr(audit_log, "add_log", fail_first_audit)
    result = (await auth_client.post("/report/cleanBatch", json={"ids": [first, second]})).json()
    assert result["code"] == 0
    assert result["data"]["deletedIds"] == [first, second]
    assert calls == 2
    logs = list((await db.scalars(select(AuditLog))).all())
    assert [item.resource_id for item in logs] == [second]
