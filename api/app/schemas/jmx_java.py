"""Java Sample VO，对齐 Java JavaVO + JavaParamVO。"""

from __future__ import annotations

from pydantic import Field

from app.schemas.base import BaseVO, CamelModel


class JavaParamVO(BaseVO):
    test_case_id: int | None = None
    jmx_id: int | None = None
    param_key: str | None = None
    param_value: str | None = None


class JavaVO(BaseVO):
    test_case_id: int | None = None
    jmx_id: int | None = None
    java_request_class_path: str | None = None
    java_param_vo_list: list[JavaParamVO] = Field(default_factory=list, alias="javaParamVOList")
