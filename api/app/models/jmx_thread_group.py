"""mysterious_jmx_thread_group ORM 模型（默认线程组）。"""

from __future__ import annotations

from sqlalchemy import BigInteger, Index, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, AuditMixin, Base


class JmxThreadGroup(Base, AuditMixin):
    __tablename__ = "mysterious_jmx_thread_group"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    test_case_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    jmx_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    num_threads: Mapped[str] = mapped_column(String(32), default="", server_default="")
    ramp_time: Mapped[str] = mapped_column(String(32), default="", server_default="")
    loops: Mapped[str] = mapped_column(String(32), default="", server_default="")
    same_user_on_next_iteration: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    delayed_start: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    scheduler: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    duration: Mapped[str] = mapped_column(String(32), default="", server_default="")
    delay: Mapped[str] = mapped_column(String(32), default="", server_default="")

    __table_args__ = (
        Index("idx_test_case_id_jmx_tg", "test_case_id"),
        Index("idx_jmx_id_jmx_tg", "jmx_id"),
    )
