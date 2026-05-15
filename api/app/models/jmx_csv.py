"""mysterious_jmx_csv ORM 模型（脚本内 CSVDataSet 控件配置）。

注意：这是"在线编辑"模式里脚本自带的 CSV 控件配置，
和 Phase 3 的 models/csv.py（用户上传的 CSV 文件元数据）是两张完全不同的表。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Index, String, SmallInteger
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, AuditMixin, Base


class JmxCsv(Base, AuditMixin):
    __tablename__ = "mysterious_jmx_csv"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    test_case_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    jmx_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    filename: Mapped[str] = mapped_column(String(255), default="", server_default="")
    variable_names: Mapped[str] = mapped_column(String(255), default="", server_default="")
    delimiter: Mapped[str] = mapped_column(String(10), default=",", server_default=",")
    file_encoding: Mapped[str] = mapped_column(String(32), default="UTF-8", server_default="UTF-8")
    ignore_first_line: Mapped[int] = mapped_column(SmallInteger, default=1, server_default="1")
    allow_quoted_data: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    recycle_on_eof: Mapped[int] = mapped_column(SmallInteger, default=1, server_default="1")
    stop_thread_on_eof: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    sharing_mode: Mapped[str] = mapped_column(String(32), default="Current thread group", server_default="Current thread group")

    __table_args__ = (
        Index("idx_test_case_id_jmx_csv", "test_case_id"),
        Index("idx_jmx_id_jmx_csv", "jmx_id"),
    )
