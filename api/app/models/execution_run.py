"""Persistent JMeter execution run state."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, AuditMixin, Base


class ExecutionRun(Base, AuditMixin):
    __tablename__ = "mysterious_execution_run"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    test_case_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    region: Mapped[str] = mapped_column(String(255), default="", server_default="")
    status: Mapped[str] = mapped_column(String(32), default="preparing", server_default="preparing")
    worker_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    pid: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    pgid: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cmd: Mapped[str] = mapped_column(Text, default="", server_default="")
    jtl_path: Mapped[str] = mapped_column(String(512), default="", server_default="")
    log_path: Mapped[str] = mapped_column(String(512), default="", server_default="")
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stop_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    exit_code: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    message: Mapped[str] = mapped_column(Text, default="", server_default="")

    __table_args__ = (
        Index("uk_execution_run_report_id", "report_id", unique=True),
        Index("idx_execution_run_status", "status"),
    )
