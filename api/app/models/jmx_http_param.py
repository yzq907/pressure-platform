"""mysterious_jmx_http_param ORM 模型（HTTP Query/Form 参数列表）。"""

from __future__ import annotations

from sqlalchemy import BigInteger, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, AuditMixin, Base


class JmxHttpParam(Base, AuditMixin):
    __tablename__ = "mysterious_jmx_http_param"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    test_case_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    jmx_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    http_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    param_key: Mapped[str] = mapped_column(String(255), default="", server_default="")
    param_value: Mapped[str] = mapped_column(String(255), default="", server_default="")

    __table_args__ = (
        Index("idx_test_case_id_jmx_param", "test_case_id"),
        Index("idx_jmx_id_jmx_param", "jmx_id"),
        Index("idx_http_id_jmx_param", "http_id"),
    )
