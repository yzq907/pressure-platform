"""Report 相关 Pydantic schemas。"""

from __future__ import annotations

from datetime import datetime

from pydantic import field_serializer

from app.schemas.base import _fmt_dt
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
    region: str | None = None
    service_name: str | None = None
    total_threads: int | None = None
    slave_count: int | None = None
    grafana_instance: str | None = None
    artifact_dir: str | None = None


class ReportVO(BaseVO):
    name: str = ""
    description: str = ""
    test_case_id: int = 0
    report_dir: str = ""
    exec_type: int = 1
    status: int = 0
    response_data: str = ""
    jmeter_log_file_path: str = ""
    region: str = ""
    service_name: str = ""
    total_threads: int = 0
    slave_count: int = 0
    grafana_instance: str = ""
    artifact_dir: str = ""
    occupied_node_hosts: list[str] = []


class ReportStatsVO(CamelModel):
    """历史报告状态聚合统计。"""

    total: int = 0
    running: int = 0
    success: int = 0
    failed: int = 0
    idle: int = 0
    success_rate: float = 100.0


class ReportQuery(BaseQuery):
    """对齐 Java ReportQuery：模糊 name"""

    name: str | None = None
    region: str | None = None
    exec_type: int | None = None


class ReportByTestCaseQuery(BaseQuery):
    """对齐 Java ReportByTestCaseQuery：模糊 name + 精确 test_case_id"""

    name: str | None = None
    test_case_id: int | None = None
    region: str | None = None
    exec_type: int | None = None


class MetricsVO(CamelModel):
    """JMeter 执行监控指标"""

    timestamp: str = ""
    qps: float = 0.0
    avg_rt: float = 0.0
    p95_rt: float = 0.0
    p99_rt: float = 0.0
    error_rate: float = 0.0
    threads: int = 0
    tps_peak: float = 0.0


class TransactionStatsVO(CamelModel):
    """JMeter 事务维度统计。"""

    name: str = ""
    samples: int = 0
    success: int = 0
    failed: int = 0
    success_rate: float = 0.0
    tps: float = 0.0
    avg_rt: float = 0.0
    max_rt: float = 0.0
    min_rt: float = 0.0
    ratio: float = 0.0


class TransactionMetricPointVO(CamelModel):
    """JMeter 事务维度曲线单点。"""

    timestamp: str = ""
    qps: float = 0.0
    avg_rt: float = 0.0
    p95_rt: float = 0.0
    p99_rt: float = 0.0
    error_rate: float = 0.0
    sample_count: int = 0
    fail_count: int = 0
    active_threads: int = 0


class TransactionMetricsVO(CamelModel):
    """JMeter 事务维度曲线。"""

    report_id: int = 0
    window: int = 60
    transactions: list[str] = []
    series: dict[str, list[TransactionMetricPointVO]] = {}


class TrendPointVO(CamelModel):
    """通用趋势点。"""

    timestamp: str = ""
    value: float = 0.0


class TransactionTrendVO(CamelModel):
    """报告交易性能趋势图。"""

    report_id: int = 0
    window: int = 60
    timestamps: list[str] = []
    transactions: list[str] = []
    total_tps: list[TrendPointVO] = []
    transaction_tps: dict[str, list[TrendPointVO]] = {}
    avg_rt: dict[str, list[TrendPointVO]] = {}
    active_threads: dict[str, list[TrendPointVO]] = {}


class ResourceMetricPointVO(CamelModel):
    """Prometheus 资源监控单点数据。"""

    timestamp: str = ""
    timestamp_ms: int = 0
    value: float | None = None


class ResourceMetricsVO(CamelModel):
    """报告关联的 Prometheus 资源监控曲线。"""

    report_id: int = 0
    instance: str = ""
    from_ms: int = 0
    to_ms: int = 0
    step: int = 30
    series: dict[str, list[ResourceMetricPointVO]] = {}


class ResourceTargetVO(CamelModel):
    """报告可查看的 Prometheus 资源目标。"""

    name: str = ""
    role: str = ""
    service: str = ""
    instance: str = ""


class ArtifactVO(CamelModel):
    """报告产物文件信息。"""

    name: str = ""
    size: int = 0
    modify_time: datetime | None = None

    @field_serializer("modify_time", when_used="json")
    def _ser_modify_time(self, v: datetime | None) -> str | None:
        return _fmt_dt(v)


class CompareVO(CamelModel):
    """报告对比数据"""

    base_name: str = ""
    target_name: str = ""
    base: list[MetricsVO] = []
    target: list[MetricsVO] = []
