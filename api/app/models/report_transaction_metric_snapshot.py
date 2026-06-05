"""Report transaction metric snapshot ORM model."""

from __future__ import annotations

from sqlalchemy import BigInteger, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, AuditMixin, Base


class ReportTransactionMetricSnapshot(Base, AuditMixin):
    __tablename__ = "mysterious_report_transaction_metric_snapshot"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    transaction_name: Mapped[str] = mapped_column(String(255), default="", server_default="")
    window_sec: Mapped[int] = mapped_column(Integer, default=60, server_default="60")
    bucket_start_ms: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    timestamp: Mapped[str] = mapped_column(String(32), default="", server_default="")
    qps: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    avg_rt: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    p95_rt: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    p99_rt: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    error_rate: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    sample_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    fail_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    active_threads: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    __table_args__ = (
        Index(
            "uk_report_transaction_metric_bucket",
            "report_id",
            "transaction_name",
            "window_sec",
            "bucket_start_ms",
            unique=True,
        ),
    )
