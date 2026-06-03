"""上传接口文件 ORM 模型。映射 mysterious_upload_file 表。"""

from __future__ import annotations

from sqlalchemy import BigInteger, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, AuditMixin, Base


class UploadFileResource(Base, AuditMixin):
    __tablename__ = "mysterious_upload_file"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    src_name: Mapped[str] = mapped_column(String(255), default="", server_default="")
    dst_name: Mapped[str] = mapped_column(String(255), default="", server_default="")
    description: Mapped[str] = mapped_column(String(255), default="", server_default="")
    file_dir: Mapped[str] = mapped_column(String(255), default="", server_default="")
    test_case_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")

    __table_args__ = (Index("idx_test_case_id_upload_file", "test_case_id"),)
