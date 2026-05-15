"""HTTP Sample VO，对齐 Java HttpVO + HttpHeaderVO + HttpParamVO。"""

from __future__ import annotations

from pydantic import Field

from app.schemas.base import BaseVO, CamelModel


class HttpHeaderVO(BaseVO):
    test_case_id: int | None = None
    jmx_id: int | None = None
    http_id: int | None = None
    header_key: str | None = None
    header_value: str | None = None


class HttpParamVO(BaseVO):
    test_case_id: int | None = None
    jmx_id: int | None = None
    http_id: int | None = None
    param_key: str | None = None
    param_value: str | None = None


class HttpVO(BaseVO):
    test_case_id: int | None = None
    jmx_id: int | None = None
    method: str | None = None
    protocol: str | None = None
    domain: str | None = None
    port: str | None = None
    path: str | None = None
    content_encoding: str | None = None
    body: str | None = None
    http_header_vo_list: list[HttpHeaderVO] = Field(default_factory=list, alias="httpHeaderVOList")
    http_param_vo_list: list[HttpParamVO] = Field(default_factory=list, alias="httpParamVOList")
