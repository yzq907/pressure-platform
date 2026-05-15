"""mysterious_jmx_assertion ORM 模型（断言）。"""

from __future__ import annotations

from sqlalchemy import BigInteger, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, AuditMixin, Base


class JmxAssertion(Base, AuditMixin):
    __tablename__ = "mysterious_jmx_assertion"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    test_case_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    jmx_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    response_code: Mapped[str] = mapped_column(String(32), default="", server_default="")
    response_message: Mapped[str] = mapped_column(String(255), default="", server_default="")
    json_path: Mapped[str] = mapped_column(String(32), default="", server_default="")
    expected_value: Mapped[str] = mapped_column(String(255), default="", server_default="")

    __table_args__ = (
        Index("idx_test_case_id_jmx_assert", "test_case_id"),
        Index("idx_jmx_id_jmx_assert", "jmx_id"),
    )
