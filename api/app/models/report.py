"""Report ORM 模型。映射 mysterious_report 表。"""

from __future__ import annotations

from sqlalchemy import BigInteger, Index, Integer, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import ID_TYPE, AuditMixin, Base


class Report(Base, AuditMixin):
    __tablename__ = "mysterious_report"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), default="", server_default="")
    description: Mapped[str] = mapped_column(String(255), default="", server_default="")
    test_case_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    report_dir: Mapped[str] = mapped_column(String(255), default="", server_default="")
    # 1-调试, 2-执行（对齐 ExecTypeEnum）
    exec_type: Mapped[int] = mapped_column(SmallInteger, default=1, server_default="1")
    # 0-未执行 1-执行中 2-执行成功 3-执行异常
    status: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    response_data: Mapped[str] = mapped_column(String(512), default="", server_default="")
    jmeter_log_file_path: Mapped[str] = mapped_column(String(255), default="", server_default="")
    region: Mapped[str] = mapped_column(String(255), default="", server_default="")
    service_name: Mapped[str] = mapped_column(String(128), default="", server_default="")
    total_threads: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    slave_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    grafana_instance: Mapped[str] = mapped_column(String(255), default="", server_default="")
    artifact_dir: Mapped[str] = mapped_column(String(255), default="", server_default="")

    __table_args__ = (Index("idx_test_case_id_report", "test_case_id"),)
