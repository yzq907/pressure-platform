"""Csv 相关 Pydantic schemas。"""

from __future__ import annotations

from app.schemas.base import BaseQuery, BaseVO, CamelModel


class CsvStrategyParam(CamelModel):
    distribution_strategy: str


class CsvResourceVO(BaseVO):
    filename: str = ""
    file_dir: str = ""
    file_type: str = ""
    description: str = ""
    file_size: int = 0
    checksum: str = ""
    reference_count: int = 0
    csv_reference_count: int = 0
    upload_file_reference_count: int = 0
    exists: bool = True


class CsvBindingParam(CamelModel):
    test_case_id: int
    filename: str
    distribution_strategy: str = "shared"
    description: str | None = None


class CsvBindingVO(BaseVO):
    test_case_id: int = 0
    filename: str = ""
    description: str = ""
    distribution_strategy: str = "shared"
    exists: bool = False


class CsvResourceQuery(BaseQuery):
    filename: str | None = None
    file_type: str | None = None
