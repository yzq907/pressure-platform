"""mysterious_jmx_concurrency_thread_group ORM 模型（并发线程组）。"""

from __future__ import annotations

from sqlalchemy import BigInteger, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, AuditMixin, Base


class JmxConcurrencyThreadGroup(Base, AuditMixin):
    __tablename__ = "mysterious_jmx_concurrency_thread_group"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    test_case_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    jmx_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    target_concurrency: Mapped[str] = mapped_column(String(32), default="", server_default="")
    ramp_up_time: Mapped[str] = mapped_column(String(32), default="", server_default="")
    ramp_up_steps_count: Mapped[str] = mapped_column(String(32), default="", server_default="")
    hold_target_rate_time: Mapped[str] = mapped_column(String(32), default="", server_default="")

    __table_args__ = (
        Index("idx_test_case_id_jmx_ctg", "test_case_id"),
        Index("idx_jmx_id_jmx_ctg", "jmx_id"),
    )
