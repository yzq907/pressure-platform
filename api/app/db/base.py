"""SQLAlchemy Declarative Base + 通用 Mixin。对齐 Java 端 BaseDO 的审计字段。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# MySQL 用 BIGINT；SQLite (测试) 必须用 INTEGER 才能 AUTOINCREMENT 生效
ID_TYPE = BigInteger().with_variant(Integer(), "sqlite")


class Base(DeclarativeBase):
    """所有 ORM 模型的基类"""


class AuditMixin:
    """对齐 Java BaseDO 的审计字段。

    注意：原 SQL Schema 里 creator_id / modifier_id 是 varchar(32)，
    不是 Java BaseDO 里写的 Long。此处以 SQL 为准。
    """

    creator_id: Mapped[str] = mapped_column(String(32), default="", server_default="")
    creator: Mapped[str] = mapped_column(String(32), default="", server_default="")
    modifier_id: Mapped[str] = mapped_column(String(32), default="", server_default="")
    modifier: Mapped[str] = mapped_column(String(32), default="", server_default="")
    create_time: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    modify_time: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=text("CURRENT_TIMESTAMP"),
    )
