"""Timeout scanner regression tests."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import TestCaseStatus
from app.models.report import Report
from app.models.testcase import TestCase
from app.services import timeout_scanner


@pytest.mark.asyncio
async def test_timeout_scanner_compares_naive_report_time_with_aware_now(
    db: AsyncSession,
    monkeypatch,
) -> None:
    """SQLite/MySQL may return naive datetimes; scanner should not crash on aware now."""
    tc = TestCase(
        name="timeout_future",
        status=TestCaseStatus.RUN_ING.value,
        test_case_dir="/tmp/timeout_future",
        timeout_seconds=7200,
    )
    db.add(tc)
    await db.commit()
    await db.refresh(tc)
    rpt = Report(
        name="timeout_future",
        test_case_id=tc.id,
        status=TestCaseStatus.RUN_ING.value,
    )
    rpt.create_time = datetime(2026, 5, 28, 9, 0, 0)
    db.add(rpt)
    await db.commit()

    monkeypatch.setattr(
        timeout_scanner,
        "_now",
        lambda: datetime(2026, 5, 28, 9, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert await timeout_scanner._scan_and_timeout(db) == 0
