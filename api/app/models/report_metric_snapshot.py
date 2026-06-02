"""Report metric snapshot ORM model."""

from __future__ import annotations

from sqlalchemy import BigInteger, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, AuditMixin, Base


class ReportMetricSnapshot(Base, AuditMixin):
    __tablename__ = "mysterious_report_metric_snapshot"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    window_sec: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    bucket_start_ms: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    timestamp: Mapped[str] = mapped_column(String(32), default="", server_default="")
    qps: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    avg_rt: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    p95_rt: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    p99_rt: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    error_rate: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    threads: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    sample_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    fail_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tps_peak: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")

    __table_args__ = (
        Index(
            "uk_report_metric_report_window_bucket",
            "report_id",
            "window_sec",
            "bucket_start_ms",
            unique=True,
        ),
    )
