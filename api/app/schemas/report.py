"""Report 相关 Pydantic schemas。"""

from __future__ import annotations

from app.schemas.base import BaseQuery, BaseVO, CamelModel


class ReportParam(CamelModel):
    """供内部 debug/run 调用 add_report 用，前端不会传过来"""

    name: str | None = None
    description: str | None = None
    test_case_id: int | None = None
    report_dir: str | None = None
    exec_type: int | None = None
    status: int | None = None
    response_data: str | None = None
    jmeter_log_file_path: str | None = None


class ReportVO(BaseVO):
    name: str = ""
    description: str = ""
    test_case_id: int = 0
    report_dir: str = ""
    exec_type: int = 1
    status: int = 0
    response_data: str = ""
    jmeter_log_file_path: str = ""


class ReportQuery(BaseQuery):
    """对齐 Java ReportQuery：模糊 name"""

    name: str | None = None


class ReportByTestCaseQuery(BaseQuery):
    """对齐 Java ReportByTestCaseQuery：模糊 name + 精确 test_case_id"""

    name: str | None = None
    test_case_id: int | None = None
