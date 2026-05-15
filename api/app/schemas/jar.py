"""Jar 相关 Pydantic schemas。"""

from __future__ import annotations

from app.schemas.base import BaseQuery, BaseVO, CamelModel


class JarParam(CamelModel):
    src_name: str | None = None
    dst_name: str | None = None
    description: str | None = None
    jar_dir: str | None = None
    test_case_id: int | None = None


class JarVO(BaseVO):
    src_name: str = ""
    dst_name: str = ""
    description: str = ""
    jar_dir: str = ""
    test_case_id: int = 0


class JarQuery(BaseQuery):
    src_name: str | None = None
    test_case_id: int | None = None
