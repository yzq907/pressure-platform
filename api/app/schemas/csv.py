"""Csv 相关 Pydantic schemas。"""

from __future__ import annotations

from app.schemas.base import BaseQuery, BaseVO, CamelModel


class CsvParam(CamelModel):
    src_name: str | None = None
    dst_name: str | None = None
    description: str | None = None
    csv_dir: str | None = None
    test_case_id: int | None = None


class CsvVO(BaseVO):
    src_name: str = ""
    dst_name: str = ""
    description: str = ""
    csv_dir: str = ""
    test_case_id: int = 0


class CsvQuery(BaseQuery):
    src_name: str | None = None
    test_case_id: int | None = None
