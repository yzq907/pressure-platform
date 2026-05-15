"""mysterious_jmx_java ORM 模型（Java Sampler）。

Java quirk: 一条 Java 请求 = 多行 java 表记录，每行存一对 (param_key, param_value)。
java_request_class_path 在每行都重复（非规范化）。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, AuditMixin, Base


class JmxJava(Base, AuditMixin):
    __tablename__ = "mysterious_jmx_java"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    test_case_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    jmx_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    java_request_class_path: Mapped[str] = mapped_column(String(32), default="", server_default="")
    param_key: Mapped[str] = mapped_column(String(255), default="", server_default="")
    param_value: Mapped[str] = mapped_column(String(255), default="", server_default="")

    __table_args__ = (
        Index("idx_test_case_id_jmx_java", "test_case_id"),
        Index("idx_jmx_id_jmx_java", "jmx_id"),
    )
