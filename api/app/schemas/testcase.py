"""TestCase 相关 Pydantic schemas，对齐 Java TestCaseParam / TestCaseVO / TestCaseQuery。"""

from __future__ import annotations

from pydantic import Field

from app.schemas.base import BaseQuery, BaseVO, CamelModel
from app.schemas.csv import CsvVO
from app.schemas.jar import JarVO
from app.schemas.jmx import JmxVO


class TestCaseParam(CamelModel):
    name: str | None = None
    description: str | None = None
    biz: str | None = None
    service: str | None = None
    version: str | None = None
    num_threads: str | None = None
    ramp_time: str | None = None
    duration: str | None = None
    timeout_seconds: int | None = 7200


class TestCaseVO(BaseVO):
    name: str = ""
    description: str = ""
    biz: str = ""
    service: str = ""
    version: str = ""
    status: int = 0
    num_threads: str = ""
    ramp_time: str = ""
    duration: str = ""
    timeout_seconds: int = 7200
    test_case_dir: str = ""


class TestCaseQuery(BaseQuery):
    id: int | None = None
    name: str | None = None
    description: str | None = None
    biz: str | None = None
    service: str | None = None


class TestCaseStatsVO(CamelModel):
    """用例状态聚合统计。"""

    total: int = 0
    idle: int = 0
    running: int = 0
    success: int = 0
    failed: int = 0
    waiting: int = 0
    canceled: int = 0


class BatchDeleteParam(CamelModel):
    """对齐 Java testcase/batchDelete 接口体 {"ids": [1,2,3]}"""

    ids: list[int] = []


class TestCaseFullVO(TestCaseVO):
    """对齐 Java TestCaseFullVO：用例 + 关联的 JMX/CSV/JAR。

    显式指定 alias 让 JSON key 是 `jmxVO/csvVOList/jarVOList`，对齐 Java（默认 to_camel 会得到 jmxVo/csvVoList）。
    """

    jmx_vo: JmxVO | None = Field(default=None, alias="jmxVO")
    csv_vo_list: list[CsvVO] = Field(default_factory=list, alias="csvVOList")
    jar_vo_list: list[JarVO] = Field(default_factory=list, alias="jarVOList")


class RunParam(CamelModel):
    """执行用例时的压测参数"""

    num_threads: str = "10"
    ramp_time: str = "0"
    duration: str = "60"
    slave_count: int = 1  # 期望使用的 slave 数量，默认 1
    region: str = ""  # 目标区域，为空则不限区域
    queue_policy: str = "fail_fast"  # fail_fast / queue_when_no_slave
    thread_group_overrides: list["ThreadGroupRunOverride"] = Field(default_factory=list)


class ThreadGroupRunOverride(CamelModel):
    """单次执行的线程组参数覆盖。mode=global 跟随全局，custom 自定义，fixed 保留 JMX 原值。"""

    key: str | None = None
    name: str
    enabled: bool | None = None
    mode: str = "global"
    num_threads: str | None = None
    ramp_time: str | None = None
    duration: str | None = None


class ThreadGroupRunVO(CamelModel):
    """JMX 中可配置的启用线程组。"""

    key: str = ""
    name: str = ""
    type: str = ""
    enabled: bool = True


class JMeterResultVO(CamelModel):
    """对齐 Java JMeterResultVO：实时压测指标的一条记录"""

    current_time: str = Field(default="", alias="currentTime")
    throughput: float = 0.0
    avg_response_time: float = 0.0
