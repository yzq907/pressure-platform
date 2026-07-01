"""上传接口文件相关 Pydantic schemas。"""

from __future__ import annotations

from app.schemas.base import BaseVO, CamelModel


class UploadFileBindingParam(CamelModel):
    test_case_id: int
    filename: str
    description: str | None = None


class UploadFileBindingVO(BaseVO):
    test_case_id: int = 0
    filename: str = ""
    description: str = ""
    exists: bool = False
