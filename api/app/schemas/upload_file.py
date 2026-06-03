"""上传接口文件相关 Pydantic schemas。"""

from __future__ import annotations

from app.schemas.base import BaseQuery, BaseVO


class UploadFileVO(BaseVO):
    src_name: str = ""
    dst_name: str = ""
    description: str = ""
    file_dir: str = ""
    test_case_id: int = 0


class UploadFileQuery(BaseQuery):
    src_name: str | None = None
    test_case_id: int | None = None
