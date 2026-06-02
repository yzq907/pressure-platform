"""Scheduled task execution log tests."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import NodeStatus, NodeType
from app.models.execution_node import ExecutionNode
from app.models.node import Node
from app.models.scheduled_task import ScheduledTask
from app.models.scheduled_task_log import ScheduledTaskLog
from app.models.testcase import TestCase
from app.services.scheduled_task import _execute_scheduled


SHANGHAI = ZoneInfo("Asia/Shanghai")


async def _create_task(
    db: AsyncSession,
    *,
    test_case_id: int,
    region: str = "长沙",
    slave_count: int = 1,
) -> ScheduledTask:
    task = ScheduledTask(
        test_case_id=test_case_id,
        schedule_type="daily",
        schedule_data=json.dumps({"time": "20:00"}),
        run_param=json.dumps(
            {
                "numThreads": "30",
                "rampTime": "0",
                "duration": "60",
                "slaveCount": slave_count,
                "region": region,
            }
        ),
        enabled=1,
        next_run_at=datetime.now(SHANGHAI).replace(tzinfo=None),
        creator_id="1",
        creator="tester",
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


@pytest.mark.asyncio
async def test_scheduled_task_skip_without_region_slaves_records_log(
    db: AsyncSession,
) -> None:
    testcase = TestCase(name="scheduled_no_slave", status=0, test_case_dir="/tmp")
    db.add(testcase)
    await db.commit()
    await db.refresh(testcase)
    task = await _create_task(db, test_case_id=testcase.id, region="长沙", slave_count=1)

    await _execute_scheduled(db, task)

    log = (
        await db.execute(
            select(ScheduledTaskLog).where(ScheduledTaskLog.scheduled_task_id == task.id)
        )
    ).scalar_one()
    assert log.trigger_type == "auto"
    assert log.status == "skipped"
    assert log.test_case_id == testcase.id
    assert log.region == "长沙"
    assert log.requested_slave_count == 1
    assert log.available_slave_count == 0
    assert log.allocated_slave_count == 0
    assert "暂无可用压力机" in log.reason
    assert log.next_run_at is not None


@pytest.mark.asyncio
async def test_scheduled_task_triggered_records_slave_allocation(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    testcase = TestCase(name="scheduled_triggered", status=0, test_case_dir="/tmp")
    db.add(testcase)
    for host in ("10.0.0.1", "10.0.0.2"):
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
                region="华南",
            )
        )
    await db.commit()
    await db.refresh(testcase)
    task = await _create_task(db, test_case_id=testcase.id, region="华南", slave_count=1)

    from app.services import testcase as testcase_service

    async def fake_run_testcase(*args, **kwargs) -> bool:
        return True

    monkeypatch.setattr(testcase_service, "run_testcase", fake_run_testcase)

    await _execute_scheduled(db, task)

    log = (
        await db.execute(
            select(ScheduledTaskLog).where(ScheduledTaskLog.scheduled_task_id == task.id)
        )
    ).scalar_one()
    assert log.trigger_type == "auto"
    assert log.status == "triggered"
    assert log.region == "华南"
    assert log.requested_slave_count == 1
    assert log.available_slave_count == 2
    assert log.allocated_slave_count == 1
    assert json.loads(log.slave_hosts) == ["10.0.0.1"]
    assert log.next_run_at is not None


@pytest.mark.asyncio
async def test_scheduled_task_slave_allocation_excludes_leased_nodes(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    testcase = TestCase(name="scheduled_excludes_leased", status=0, test_case_dir="/tmp")
    db.add(testcase)
    busy = Node(
        name="busy",
        type=NodeType.SLAVE.value,
        host="10.0.1.1",
        username="root",
        password="x",
        port=22,
        status=NodeStatus.ENABLE.value,
        health_status=1,
        region="华北",
    )
    idle = Node(
        name="idle",
        type=NodeType.SLAVE.value,
        host="10.0.1.2",
        username="root",
        password="x",
        port=22,
        status=NodeStatus.ENABLE.value,
        health_status=1,
        region="华北",
    )
    db.add_all([busy, idle])
    await db.commit()
    await db.refresh(testcase)
    await db.refresh(busy)
    db.add(
        ExecutionNode(
            report_id=1101,
            test_case_id=2201,
            node_id=busy.id,
            node_host=busy.host,
            region="华北",
            status="leased",
        )
    )
    await db.commit()
    task = await _create_task(db, test_case_id=testcase.id, region="华北", slave_count=1)

    from app.services import testcase as testcase_service

    async def fake_run_testcase(*args, **kwargs) -> bool:
        return True

    monkeypatch.setattr(testcase_service, "run_testcase", fake_run_testcase)

    await _execute_scheduled(db, task)

    log = (
        await db.execute(
            select(ScheduledTaskLog).where(ScheduledTaskLog.scheduled_task_id == task.id)
        )
    ).scalar_one()
    assert log.status == "triggered"
    assert log.available_slave_count == 1
    assert log.allocated_slave_count == 1
    assert json.loads(log.slave_hosts) == ["10.0.1.2"]


@pytest.mark.asyncio
async def test_scheduled_task_log_list_api(
    auth_client: AsyncClient,
    db: AsyncSession,
) -> None:
    testcase = TestCase(name="scheduled_log_api", status=0, test_case_dir="/tmp")
    db.add(testcase)
    await db.commit()
    await db.refresh(testcase)
    task = await _create_task(db, test_case_id=testcase.id, region="华南", slave_count=2)
    db.add(
        ScheduledTaskLog(
            scheduled_task_id=task.id,
            test_case_id=testcase.id,
            trigger_type="auto",
            status="skipped",
            reason="区域「华南」暂无可用压力机，跳过执行",
            message="区域「华南」暂无可用压力机，跳过执行",
            region="华南",
            requested_slave_count=2,
            available_slave_count=0,
            allocated_slave_count=0,
            slave_hosts="[]",
            run_param=task.run_param,
            trigger_time=datetime.now(SHANGHAI).replace(tzinfo=None),
            next_run_at=task.next_run_at,
        )
    )
    await db.commit()

    resp = await auth_client.get(f"/scheduledTask/logs?scheduledTaskId={task.id}")
    body = resp.json()

    assert body["code"] == 0
    assert body["data"]["total"] == 1
    item = body["data"]["list"][0]
    assert item["scheduledTaskId"] == task.id
    assert item["testCaseId"] == testcase.id
    assert item["triggerType"] == "auto"
    assert item["status"] == "skipped"
    assert item["region"] == "华南"
    assert item["requestedSlaveCount"] == 2
    assert item["availableSlaveCount"] == 0
    assert item["allocatedSlaveCount"] == 0
