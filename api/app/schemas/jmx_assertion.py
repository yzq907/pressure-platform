"""断言 VO，对齐 Java AssertionVO。"""

from __future__ import annotations

from app.schemas.base import BaseVO


class AssertionVO(BaseVO):
    test_case_id: int | None = None
    jmx_id: int | None = None
    response_code: str | None = None
    response_message: str | None = None
    json_path: str | None = None
    expected_value: str | None = None
