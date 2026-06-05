"""Report transaction statistics snapshot ORM model."""

from __future__ import annotations

from sqlalchemy import BigInteger, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, AuditMixin, Base


class ReportTransactionSnapshot(Base, AuditMixin):
    __tablename__ = "mysterious_report_transaction_snapshot"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    transaction_name: Mapped[str] = mapped_column(String(255), default="", server_default="")
    samples: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    success_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    fail_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    success_rate: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    tps: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    avg_rt: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    max_rt: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    min_rt: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    ratio: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")

    __table_args__ = (
        Index(
            "uk_report_transaction_report_name",
            "report_id",
            "transaction_name",
            unique=True,
        ),
    )
