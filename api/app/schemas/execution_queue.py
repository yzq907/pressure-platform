"""Execution queue schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import field_serializer

from app.schemas.base import BaseQuery, BaseVO, CamelModel, _fmt_dt


class ExecutionQueueQuery(BaseQuery):
    status: str | None = None
    test_case_id: int | None = None


class ExecutionQueueVO(BaseVO):
    test_case_id: int = 0
    report_id: int = 0
    status: str = ""
    queue_policy: str = ""
    trigger_type: str = ""
    run_param: str = ""
    region: str = ""
    requested_slave_count: int = 0
    available_slave_count: int = 0
    allocated_slave_count: int = 0
    slave_hosts: str = ""
    message: str = ""
    enqueue_time: datetime | None = None
    start_time: datetime | None = None
    finish_time: datetime | None = None

    @field_serializer("enqueue_time", "start_time", "finish_time", when_used="json")
    def _ser_dt(self, v: datetime | None) -> str | None:
        return _fmt_dt(v)


class ExecutionQueueStatsVO(CamelModel):
    pending: int = 0
    running: int = 0
    canceling: int = 0
