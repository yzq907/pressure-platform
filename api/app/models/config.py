"""Config ORM 模型。映射 mysterious_config 表。"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, AuditMixin, Base


class Config(Base, AuditMixin):
    __tablename__ = "mysterious_config"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    config_key: Mapped[str] = mapped_column(String(255), default="", server_default="")
    config_value: Mapped[str] = mapped_column(String(255), default="", server_default="")
    description: Mapped[str] = mapped_column(String(255), default="", server_default="")
