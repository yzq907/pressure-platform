"""Persistent node lease records for JMeter executions."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, AuditMixin, Base


class ExecutionNode(Base, AuditMixin):
    __tablename__ = "mysterious_execution_node"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    test_case_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    execution_run_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    node_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    node_host: Mapped[str] = mapped_column(String(128), default="", server_default="")
    region: Mapped[str] = mapped_column(String(255), default="", server_default="")
    status: Mapped[str] = mapped_column(String(32), default="leased", server_default="leased")
    leased_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    release_message: Mapped[str] = mapped_column(Text, default="", server_default="")

    __table_args__ = (
        Index("idx_execution_node_report_id", "report_id"),
        Index("idx_execution_node_node_id_status", "node_id", "status"),
        Index("idx_execution_node_status", "status"),
    )
