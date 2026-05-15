"""mysterious_jmx_http ORM 模型（HTTP 请求采样器）。"""

from __future__ import annotations

from sqlalchemy import BigInteger, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, AuditMixin, Base


class JmxHttp(Base, AuditMixin):
    __tablename__ = "mysterious_jmx_http"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    test_case_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    jmx_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    method: Mapped[str] = mapped_column(String(32), default="", server_default="")
    protocol: Mapped[str] = mapped_column(String(32), default="", server_default="")
    domain: Mapped[str] = mapped_column(String(255), default="", server_default="")
    port: Mapped[str] = mapped_column(String(32), default="", server_default="")
    path: Mapped[str] = mapped_column(String(255), default="", server_default="")
    content_encoding: Mapped[str] = mapped_column(String(32), default="", server_default="")
    body: Mapped[str] = mapped_column(String(4096), default="", server_default="")

    __table_args__ = (
        Index("idx_test_case_id_jmx_http", "test_case_id"),
        Index("idx_jmx_id_jmx_http", "jmx_id"),
    )
