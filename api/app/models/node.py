"""Node ORM 模型。映射 mysterious_node 表。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, AuditMixin, Base


class Node(Base, AuditMixin):
    __tablename__ = "mysterious_node"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), default="", server_default="")
    description: Mapped[str] = mapped_column(String(255), default="", server_default="")
    # SQL 里是 tinyint(4)；用 SmallInteger 兼容 MySQL/SQLite
    type: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    host: Mapped[str] = mapped_column(String(128), default="", server_default="")
    username: Mapped[str] = mapped_column(String(128), default="", server_default="")
    password: Mapped[str] = mapped_column(String(128), default="", server_default="")
    port: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    status: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    region: Mapped[str] = mapped_column(String(255), default="", server_default="")
    # 健康检查字段
    health_status: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cpu_usage: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    mem_usage: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    load_avg: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
