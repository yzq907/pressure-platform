"""AuditLog ORM 模型。映射 mysterious_audit_log 表。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, Base


class AuditLog(Base):
    __tablename__ = "mysterious_audit_log"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    username: Mapped[str] = mapped_column(String(128), default="", server_default="")
    action: Mapped[str] = mapped_column(String(32), default="", server_default="")
    resource_type: Mapped[str] = mapped_column(String(32), default="", server_default="")
    resource_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    resource_name: Mapped[str] = mapped_column(String(255), default="", server_default="")
    detail: Mapped[str] = mapped_column(Text, default="", server_default="")
    ip: Mapped[str] = mapped_column(String(64), default="", server_default="")
    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
