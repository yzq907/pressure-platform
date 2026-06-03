"""统一 import 所有 ORM 模型，方便 Base.metadata.create_all 能感知到全部表"""

from app.models.audit_log import AuditLog
from app.models.ai_generation import AiGenerationArtifact, AiGenerationTask
from app.models.config import Config
from app.models.csv import Csv
from app.models.execution_node import ExecutionNode
from app.models.execution_queue import ExecutionQueue
from app.models.execution_run import ExecutionRun
from app.models.jar import Jar
from app.models.jmx import Jmx
from app.models.jmx_assertion import JmxAssertion
from app.models.jmx_concurrency_thread_group import JmxConcurrencyThreadGroup
from app.models.jmx_csv import JmxCsv
from app.models.jmx_http import JmxHttp
from app.models.jmx_http_header import JmxHttpHeader
from app.models.jmx_http_param import JmxHttpParam
from app.models.jmx_java import JmxJava
from app.models.jmx_stepping_thread_group import JmxSteppingThreadGroup
from app.models.jmx_thread_group import JmxThreadGroup
from app.models.node import Node
from app.models.report import Report
from app.models.report_metric_snapshot import ReportMetricSnapshot
from app.models.role import Role, RolePermission
from app.models.scheduled_task import ScheduledTask
from app.models.scheduled_task_log import ScheduledTaskLog
from app.models.testcase import TestCase
from app.models.upload_file import UploadFileResource
from app.models.user import User

__all__ = [
    "AuditLog",
    "AiGenerationArtifact",
    "AiGenerationTask",
    "Config",
    "Csv",
    "ExecutionNode",
    "ExecutionQueue",
    "ExecutionRun",
    "Jar",
    "Jmx",
    "JmxAssertion",
    "JmxConcurrencyThreadGroup",
    "JmxCsv",
    "JmxHttp",
    "JmxHttpHeader",
    "JmxHttpParam",
    "JmxJava",
    "JmxSteppingThreadGroup",
    "JmxThreadGroup",
    "Node",
    "Report",
    "ReportMetricSnapshot",
    "Role",
    "RolePermission",
    "ScheduledTask",
    "ScheduledTaskLog",
    "TestCase",
    "UploadFileResource",
    "User",
]
