"""公共 CSV 参数化文件资源模型。"""

from __future__ import annotations

from sqlalchemy import BigInteger, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, AuditMixin, Base


class CsvResource(Base, AuditMixin):
    __tablename__ = "mysterious_csv_resource"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(255), default="", server_default="")
    file_dir: Mapped[str] = mapped_column(String(255), default="", server_default="")
    file_type: Mapped[str] = mapped_column(String(32), default="", server_default="")
    description: Mapped[str] = mapped_column(String(255), default="", server_default="")
    file_size: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    checksum: Mapped[str] = mapped_column(String(64), default="", server_default="")

    __table_args__ = (
        Index("uk_mysterious_csv_resource_filename", "filename", unique=True),
    )
