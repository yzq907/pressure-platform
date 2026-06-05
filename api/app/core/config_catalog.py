"""Config category catalog.

The config table stores only key/value/description for compatibility. This
catalog provides display metadata and derived categories by config key.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConfigCategory:
    key: str
    name: str
    sort: int


@dataclass(frozen=True)
class ConfigMeta:
    category: str
    display_name: str
    value_type: str = "text"
    sort: int = 100


CATEGORIES: tuple[ConfigCategory, ...] = (
    ConfigCategory("business", "业务选项", 10),
    ConfigCategory("jmeter", "JMeter", 20),
    ConfigCategory("grafana", "Grafana", 30),
    ConfigCategory("prometheus", "Prometheus", 35),
    ConfigCategory("report", "报告", 40),
    ConfigCategory("schedule", "调度", 50),
    ConfigCategory("init_data", "初始化数据", 60),
    ConfigCategory("ai_generation", "AI生成", 70),
    ConfigCategory("retention", "系统保留策略", 80),
    ConfigCategory("other", "其他", 999),
)

CATEGORY_NAME_MAP = {item.key: item.name for item in CATEGORIES}
CATEGORY_SORT_MAP = {item.key: item.sort for item in CATEGORIES}

CONFIG_META: dict[str, ConfigMeta] = {
    "BIZ_OPTIONS": ConfigMeta("business", "产品线选项", "list", 10),
    "SERVICE_OPTIONS": ConfigMeta("business", "服务选项", "list", 20),
    "VERSION_OPTIONS": ConfigMeta("business", "版本选项", "list", 30),
    "REGION_OPTIONS": ConfigMeta("business", "区域选项", "list", 40),
    "MASTER_JMETER_HOME": ConfigMeta("jmeter", "Master JMeter目录", "path", 10),
    "MASTER_JMETER_BIN_HOME": ConfigMeta("jmeter", "Master JMeter执行目录", "path", 20),
    "SLAVE_JMETER_BIN_HOME": ConfigMeta("jmeter", "Slave JMeter执行目录", "path", 30),
    "SLAVE_JMETER_LOG_HOME": ConfigMeta("jmeter", "Slave JMeter日志目录", "path", 40),
    "MASTER_BASE_JMX_FILES_PATH": ConfigMeta("jmeter", "在线JMX基础脚本目录", "path", 50),
    "GRAFANA_DASHBOARD_URL": ConfigMeta("grafana", "Grafana完整面板地址", "url", 10),
    "GRAFANA_BASE_URL": ConfigMeta("grafana", "Grafana服务地址", "url", 20),
    "GRAFANA_DASHBOARD_PATH": ConfigMeta("grafana", "Grafana面板路径", "text", 30),
    "GRAFANA_ORG_ID": ConfigMeta("grafana", "Grafana组织ID", "number", 40),
    "GRAFANA_INSTANCE_VAR": ConfigMeta("grafana", "Grafana实例变量名", "text", 50),
    "GRAFANA_DEFAULT_INSTANCE": ConfigMeta("grafana", "Grafana默认实例", "text", 60),
    "GRAFANA_INSTANCE_MAP": ConfigMeta("grafana", "服务Grafana实例映射", "json", 70),
    "GRAFANA_FROM_OFFSET_MINUTES": ConfigMeta("grafana", "Grafana开始时间偏移分钟", "number", 80),
    "GRAFANA_TO_OFFSET_MINUTES": ConfigMeta("grafana", "Grafana结束时间偏移分钟", "number", 90),
    "PROMETHEUS_BASE_URL": ConfigMeta("prometheus", "Prometheus服务地址", "url", 10),
    "PROMETHEUS_TIMEOUT_SECONDS": ConfigMeta("prometheus", "Prometheus查询超时时间秒", "number", 20),
    "PROMETHEUS_STEP_SECONDS": ConfigMeta("prometheus", "Prometheus默认查询步长秒", "number", 30),
    "PROMETHEUS_INSTANCE_LABEL": ConfigMeta("prometheus", "Prometheus实例标签名", "text", 40),
    "PROMETHEUS_DEFAULT_INSTANCE": ConfigMeta("prometheus", "Prometheus默认实例", "text", 50),
    "PROMETHEUS_INSTANCE_MAP": ConfigMeta("prometheus", "服务Prometheus实例映射", "json", 60),
    "PROMETHEUS_RESOURCE_GROUP_MAP": ConfigMeta("prometheus", "Prometheus资源组映射", "json", 70),
    "PROMETHEUS_FROM_OFFSET_MINUTES": ConfigMeta("prometheus", "Prometheus开始时间偏移分钟", "number", 80),
    "PROMETHEUS_TO_OFFSET_MINUTES": ConfigMeta("prometheus", "Prometheus结束时间偏移分钟", "number", 90),
    "MASTER_DATA_HOME": ConfigMeta("report", "Master数据和报告目录", "path", 10),
    "MASTER_HOST_PORT": ConfigMeta("report", "报告预览Host", "text", 20),
    "INIT_ARTIFACT_TESTCASE_IDS": ConfigMeta("init_data", "初始化产物用例ID", "text", 10),
    "AI_GENERATION_WORK_DIR": ConfigMeta("ai_generation", "AI生成工作目录", "path", 10),
    "AI_GENERATION_TIMEOUT_SECONDS": ConfigMeta("ai_generation", "AI生成超时时间秒", "number", 20),
    "AI_CODEX_BIN": ConfigMeta("ai_generation", "Codex命令路径", "text", 30),
    "AI_JMX_SKILL_NAME": ConfigMeta("ai_generation", "JMX生成Skill名称", "text", 40),
    "REPORT_RETENTION_DAYS": ConfigMeta("retention", "测试报告保留天数", "number", 10),
    "AUDIT_RETENTION_DAYS": ConfigMeta("retention", "审计日志保留天数", "number", 20),
}

DEFAULT_CONFIG_VALUES: dict[str, str] = {
    "PROMETHEUS_STEP_SECONDS": "30",
}


def get_config_meta(config_key: str) -> ConfigMeta:
    return CONFIG_META.get(config_key, ConfigMeta("other", config_key or "未命名配置", "text", 999))


def get_category_name(category: str) -> str:
    return CATEGORY_NAME_MAP.get(category, CATEGORY_NAME_MAP["other"])


def get_category_sort(category: str) -> int:
    return CATEGORY_SORT_MAP.get(category, CATEGORY_SORT_MAP["other"])
