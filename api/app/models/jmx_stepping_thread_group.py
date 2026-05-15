"""mysterious_jmx_stepping_thread_group ORM 模型（梯度线程组）。"""

from __future__ import annotations

from sqlalchemy import BigInteger, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, AuditMixin, Base


class JmxSteppingThreadGroup(Base, AuditMixin):
    __tablename__ = "mysterious_jmx_stepping_thread_group"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    test_case_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    jmx_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    num_threads: Mapped[str] = mapped_column(String(32), default="", server_default="")
    first_wait_for_seconds: Mapped[str] = mapped_column(String(32), default="", server_default="")
    then_start_threads: Mapped[str] = mapped_column(String(32), default="", server_default="")
    next_add_threads: Mapped[str] = mapped_column(String(32), default="", server_default="")
    next_add_threads_every_seconds: Mapped[str] = mapped_column(String(32), default="", server_default="")
    using_ramp_up_seconds: Mapped[str] = mapped_column(String(32), default="", server_default="")
    then_hold_load_for_seconds: Mapped[str] = mapped_column(String(32), default="", server_default="")
    finally_stop_threads: Mapped[str] = mapped_column(String(32), default="", server_default="")
    finally_stop_threads_every_seconds: Mapped[str] = mapped_column(String(32), default="", server_default="")

    __table_args__ = (
        Index("idx_test_case_id_jmx_stg", "test_case_id"),
        Index("idx_jmx_id_jmx_stg", "jmx_id"),
    )
