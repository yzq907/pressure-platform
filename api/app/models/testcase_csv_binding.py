"""用例与公共 CSV 参数化文件的绑定关系。"""

from __future__ import annotations

from sqlalchemy import BigInteger, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, AuditMixin, Base


class TestcaseCsvBinding(Base, AuditMixin):
    __tablename__ = "mysterious_testcase_csv_binding"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    test_case_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    filename: Mapped[str] = mapped_column(String(255), default="", server_default="")
    description: Mapped[str] = mapped_column(String(255), default="", server_default="")
    distribution_strategy: Mapped[str] = mapped_column(String(32), default="shared", server_default="shared")

    __table_args__ = (
        Index("uk_testcase_csv_binding_case_filename", "test_case_id", "filename", unique=True),
        Index("idx_testcase_csv_binding_filename", "filename"),
    )
