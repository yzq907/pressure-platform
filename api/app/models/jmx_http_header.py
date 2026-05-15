"""mysterious_jmx_http_header ORM 模型（HTTP Header 列表）。"""

from __future__ import annotations

from sqlalchemy import BigInteger, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, AuditMixin, Base


class JmxHttpHeader(Base, AuditMixin):
    __tablename__ = "mysterious_jmx_http_header"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    test_case_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    jmx_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    http_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    header_key: Mapped[str] = mapped_column(String(255), default="", server_default="")
    header_value: Mapped[str] = mapped_column(String(255), default="", server_default="")

    __table_args__ = (
        Index("idx_test_case_id_jmx_hdr", "test_case_id"),
        Index("idx_jmx_id_jmx_hdr", "jmx_id"),
        Index("idx_http_id_jmx_hdr", "http_id"),
    )
