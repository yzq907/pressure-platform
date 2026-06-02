from __future__ import annotations

import json
from datetime import datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import NodeStatus, NodeType, TestCaseStatus
from app.models.execution_queue import ExecutionQueue
from app.models.node import Node
from app.models.report import Report
from app.models.testcase import TestCase
from app.services import execution_queue as execution_queue_service
from app.services import jmeter_runner
from app.db import session as session_module
from tests.test_testcase_run import _create_case_with_jmx


@pytest.mark.asyncio
async def test_run_can_queue_when_region_has_no_slave(
    auth_client: AsyncClient,
    data_home,
    jmeter_bin_home,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
) -> None:
    case_id = await _create_case_with_jmx(auth_client, "q_no_slave", sample_jmx_bytes)

    resp = await auth_client.post(
        f"/testcase/run/{case_id}",
        json={
            "numThreads": "10",
            "rampTime": "0",
            "duration": "60",
            "slaveCount": 1,
            "region": "长沙",
            "queuePolicy": "queue_when_no_slave",
        },
    )

    body = resp.json()
    assert body["code"] == 0
    tc = (await db.execute(select(TestCase).where(TestCase.id == case_id))).scalar_one()
    assert tc.status == TestCaseStatus.RUN_WAITING.value
    queue = (await db.execute(select(ExecutionQueue).where(ExecutionQueue.test_case_id == case_id))).scalar_one()
    assert queue.status == "pending"
    assert queue.region == "长沙"
    assert queue.requested_slave_count == 1
    assert json.loads(queue.run_param)["queuePolicy"] == "queue_when_no_slave"
    reports = (await db.execute(select(Report).where(Report.test_case_id == case_id))).scalars().all()
    assert reports == []


@pytest.mark.asyncio
async def test_cancel_pending_queue_resets_testcase_status(
    auth_client: AsyncClient,
    data_home,
    jmeter_bin_home,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
) -> None:
    case_id = await _create_case_with_jmx(auth_client, "q_cancel", sample_jmx_bytes)
    await auth_client.post(
        f"/testcase/run/{case_id}",
        json={
            "numThreads": "10",
            "rampTime": "0",
            "duration": "60",
            "slaveCount": 1,
            "region": "长沙",
            "queuePolicy": "queue_when_no_slave",
        },
    )
    queue = (await db.execute(select(ExecutionQueue).where(ExecutionQueue.test_case_id == case_id))).scalar_one()

    resp = await auth_client.get(f"/testcase/cancelQueuedExecution/{queue.id}")

    assert resp.json()["code"] == 0
    await db.refresh(queue)
    assert queue.status == "canceled"
    tc = (await db.execute(select(TestCase).where(TestCase.id == case_id))).scalar_one()
    assert tc.status == TestCaseStatus.WAIT_CANCEL.value


@pytest.mark.asyncio
async def test_dispatcher_starts_pending_queue_when_slave_available(
    auth_client: AsyncClient,
    data_home,
    jmeter_bin_home,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
    monkeypatch,
) -> None:
    captured: dict = {}
    real_launch = jmeter_runner.launch_jmeter

    async def spy(cmd, **kw):
        captured["cmd"] = cmd
        return await real_launch(cmd, **kw)

    monkeypatch.setattr(jmeter_runner, "launch_jmeter", spy)
    case_id = await _create_case_with_jmx(auth_client, "q_dispatch", sample_jmx_bytes)
    await auth_client.post(
        f"/testcase/run/{case_id}",
        json={
            "numThreads": "10",
            "rampTime": "0",
            "duration": "60",
            "slaveCount": 1,
            "region": "长沙",
            "queuePolicy": "queue_when_no_slave",
        },
    )
    db.add(
        Node(
            name="q-slave",
            type=NodeType.SLAVE.value,
            host="10.0.8.1",
            username="root",
            password="x",
            port=22,
            status=NodeStatus.ENABLE.value,
            health_status=1,
            region="长沙",
        )
    )
    await db.commit()

    started = await execution_queue_service.dispatch_pending_once()

    assert started == 1
    queue = (await db.execute(select(ExecutionQueue).where(ExecutionQueue.test_case_id == case_id))).scalar_one()
    assert queue.status == "running"
    assert queue.report_id > 0
    assert "-R" in captured["cmd"]
    await jmeter_runner.wait_for_completion(queue.report_id, timeout=10.0)
    await db.refresh(queue)
    assert queue.status == "success"


@pytest.mark.asyncio
async def test_dispatcher_binds_queue_before_launching_jmeter(
    auth_client: AsyncClient,
    data_home,
    jmeter_bin_home,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
    monkeypatch,
) -> None:
    observed: dict = {}
    real_launch = jmeter_runner.launch_jmeter
    case_id = await _create_case_with_jmx(auth_client, "q_bind_before_launch", sample_jmx_bytes)
    await auth_client.post(
        f"/testcase/run/{case_id}",
        json={
            "numThreads": "10",
            "rampTime": "0",
            "duration": "60",
            "slaveCount": 1,
            "region": "长沙",
            "queuePolicy": "queue_when_no_slave",
        },
    )
    db.add(
        Node(
            name="q-bind-slave",
            type=NodeType.SLAVE.value,
            host="10.0.8.2",
            username="root",
            password="x",
            port=22,
            status=NodeStatus.ENABLE.value,
            health_status=1,
            region="长沙",
        )
    )
    await db.commit()

    async def spy(cmd, **kw):
        async with session_module.AsyncSessionLocal() as session:
            queue = (
                await session.execute(select(ExecutionQueue).where(ExecutionQueue.test_case_id == case_id))
            ).scalar_one()
            observed["report_id"] = queue.report_id
            observed["status"] = queue.status
            observed["expected_report_id"] = kw["report_id"]
        return await real_launch(cmd, **kw)

    monkeypatch.setattr(jmeter_runner, "launch_jmeter", spy)

    started = await execution_queue_service.dispatch_pending_once()

    assert started == 1
    assert observed["report_id"] == observed["expected_report_id"]
    assert observed["status"] == "running"


@pytest.mark.asyncio
async def test_dispatcher_does_not_start_queue_canceled_after_scan(
    auth_client: AsyncClient,
    data_home,
    jmeter_bin_home,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
    monkeypatch,
) -> None:
    from app.services import testcase as testcase_service

    case_id = await _create_case_with_jmx(auth_client, "q_cancel_race", sample_jmx_bytes)
    await auth_client.post(
        f"/testcase/run/{case_id}",
        json={
            "numThreads": "10",
            "rampTime": "0",
            "duration": "60",
            "slaveCount": 1,
            "region": "长沙",
            "queuePolicy": "queue_when_no_slave",
        },
    )
    queue = (await db.execute(select(ExecutionQueue).where(ExecutionQueue.test_case_id == case_id))).scalar_one()
    called = {"run": False}

    async def fake_should_queue(db_arg, testcase_id, param, *, allow_waiting_queue_id=None):
        current = await db_arg.get(ExecutionQueue, allow_waiting_queue_id)
        current.status = "canceled"
        current.finish_time = datetime.now()
        testcase = await db_arg.get(TestCase, testcase_id)
        testcase.status = TestCaseStatus.WAIT_CANCEL.value
        await db_arg.commit()
        return False, "", 1

    async def fake_run(*args, **kwargs):
        called["run"] = True
        return 999

    monkeypatch.setattr(testcase_service, "_should_queue_run", fake_should_queue)
    monkeypatch.setattr(testcase_service, "_run_testcase_now", fake_run)

    started = await execution_queue_service.dispatch_pending_once()

    await db.refresh(queue)
    assert started == 0
    assert called["run"] is False
    assert queue.status == "canceled"


@pytest.mark.asyncio
async def test_can_queue_same_testcase_in_different_regions(
    auth_client: AsyncClient,
    data_home,
    jmeter_bin_home,
    sample_jmx_bytes: bytes,
    db: AsyncSession,
) -> None:
    case_id = await _create_case_with_jmx(auth_client, "q_regions", sample_jmx_bytes)
    for region in ("长沙", "北京"):
        resp = await auth_client.post(
            f"/testcase/run/{case_id}",
            json={
                "numThreads": "10",
                "rampTime": "0",
                "duration": "60",
                "slaveCount": 1,
                "region": region,
                "queuePolicy": "queue_when_no_slave",
            },
        )
        assert resp.json()["code"] == 0

    queues = (
        await db.execute(
            select(ExecutionQueue).where(ExecutionQueue.test_case_id == case_id).order_by(ExecutionQueue.id.asc())
        )
    ).scalars().all()
    assert [queue.region for queue in queues] == ["长沙", "北京"]
