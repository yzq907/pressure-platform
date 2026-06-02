"""Execution queue ORM model."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, AuditMixin, Base


class ExecutionQueue(Base, AuditMixin):
    __tablename__ = "mysterious_execution_queue"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    test_case_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    report_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(32), default="pending", server_default="pending")
    queue_policy: Mapped[str] = mapped_column(String(64), default="", server_default="")
    trigger_type: Mapped[str] = mapped_column(String(32), default="manual", server_default="manual")
    run_param: Mapped[str] = mapped_column(Text, default="", server_default="")
    region: Mapped[str] = mapped_column(String(255), default="", server_default="")
    requested_slave_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    available_slave_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    allocated_slave_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    slave_hosts: Mapped[str] = mapped_column(Text, default="", server_default="")
    message: Mapped[str] = mapped_column(Text, default="", server_default="")
    enqueue_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    start_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finish_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
