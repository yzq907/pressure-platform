"""TestCase ORM 模型。映射 mysterious_testcase 表。"""

from __future__ import annotations

from sqlalchemy import SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, AuditMixin, Base


class TestCase(Base, AuditMixin):
    __tablename__ = "mysterious_testcase"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), default="", server_default="")
    description: Mapped[str] = mapped_column(String(255), default="", server_default="")
    biz: Mapped[str] = mapped_column(String(128), default="", server_default="")
    service: Mapped[str] = mapped_column(String(128), default="", server_default="")
    version: Mapped[str] = mapped_column(String(128), default="", server_default="")
    status: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    num_threads: Mapped[str] = mapped_column(String(32), default="", server_default="")
    ramp_time: Mapped[str] = mapped_column(String(32), default="", server_default="")
    duration: Mapped[str] = mapped_column(String(32), default="", server_default="")
    timeout_seconds: Mapped[int] = mapped_column(SmallInteger, default=7200, server_default="7200")
    test_case_dir: Mapped[str] = mapped_column(String(255), default="", server_default="")
