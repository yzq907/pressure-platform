"""脚本内 CSVDataSet 控件 VO，对齐 Java CsvDataVO + CsvFileVO。

注意：这是"在线编辑"脚本里的 CSV 控件配置，不是上传的 CSV 文件元数据（那在 schemas/csv.py）。
"""

from __future__ import annotations

from pydantic import Field

from app.schemas.base import BaseVO, CamelModel


class CsvFileVO(CamelModel):
    """单个 CSV 文件"""
    filename: str | None = None
    variable_names: str | None = None


class CsvDataVO(BaseVO):
    test_case_id: int | None = None
    jmx_id: int | None = None
    file_encoding: str | None = "UTF-8"
    delimiter: str | None = ","
    ignore_first_line: int | None = 1
    allow_quoted_data: int | None = 0
    recycle_on_eof: int | None = 1
    stop_thread_on_eof: int | None = 0
    sharing_mode: str | None = "Current thread group"
    csv_file_vo_list: list[CsvFileVO] = Field(default_factory=list, alias="csvFileVOList")
