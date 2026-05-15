"""User SQLAlchemy ORM 模型。映射 mysterious_user 表。

注意：和其他业务表不同，user 表没有 creator/modifier/create_time/modify_time 审计字段，
所以这里不引入 AuditMixin。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, Base


class User(Base):
    __tablename__ = "mysterious_user"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(128), default="", server_default="")
    password: Mapped[str] = mapped_column(String(128), default="", server_default="")
    real_name: Mapped[str] = mapped_column(String(128), default="", server_default="")
    token: Mapped[str] = mapped_column(String(128), default="", server_default="")
    effect_time: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    expire_time: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=text("CURRENT_TIMESTAMP"),
    )
