"""Jmx 相关 Pydantic schemas。"""

from __future__ import annotations

from pydantic import Field

from app.schemas.base import BaseQuery, BaseVO, CamelModel
from app.schemas.jmx_assertion import AssertionVO
from app.schemas.jmx_csv import CsvDataVO
from app.schemas.jmx_http import HttpVO
from app.schemas.jmx_java import JavaVO
from app.schemas.jmx_thread import (
    ConcurrencyThreadGroupVO,
    SteppingThreadGroupVO,
    ThreadGroupVO,
)


class JmxParam(CamelModel):
    """JMX 描述/参数更新使用。上传走 multipart，不走这个"""

    src_name: str | None = None
    dst_name: str | None = None
    description: str | None = None
    jmx_dir: str | None = None
    test_case_id: int | None = None
    jmeter_script_type: int | None = None
    jmeter_threads_type: int | None = None
    jmeter_sample_type: int | None = None


class JmxVO(BaseVO):
    src_name: str = ""
    dst_name: str = ""
    description: str = ""
    jmx_dir: str = ""
    test_case_id: int = 0
    jmeter_script_type: int = 0
    jmeter_threads_type: int = 0
    jmeter_sample_type: int = 0
    # Phase 4: 嵌套子表 VO（在线编辑模式时使用）
    # 显式 alias：Java 端是 threadGroupVO，pydantic to_camel 会给 threadGroupVo
    thread_group_vo: ThreadGroupVO | None = Field(default=None, alias="threadGroupVO")
    stepping_thread_group_vo: SteppingThreadGroupVO | None = Field(
        default=None, alias="steppingThreadGroupVO"
    )
    concurrency_thread_group_vo: ConcurrencyThreadGroupVO | None = Field(
        default=None, alias="concurrencyThreadGroupVO"
    )
    http_vo: HttpVO | None = Field(default=None, alias="httpVO")
    java_vo: JavaVO | None = Field(default=None, alias="javaVO")
    assertion_vo: AssertionVO | None = Field(default=None, alias="assertionVO")
    csv_data_vo: CsvDataVO | None = Field(default=None, alias="csvDataVO")


class JmxQuery(BaseQuery):
    src_name: str | None = None
    test_case_id: int | None = None
