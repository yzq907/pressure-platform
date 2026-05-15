"""业务枚举集中管理。对齐 Java service.enums.*。"""

from __future__ import annotations

from enum import IntEnum


class NodeStatus(IntEnum):
    """对齐 NodeStatusEnum"""

    DISABLED = 0
    ENABLE = 1
    FAILED = 2


class NodeType(IntEnum):
    """对齐 NodeTypeEnum"""

    SLAVE = 0
    MASTER = 1


class TestCaseStatus(IntEnum):
    """对齐 TestCaseStatus 枚举"""

    NOT_RUN = 0
    RUN_ING = 1
    RUN_SUCCESS = 2
    RUN_FAILED = 3
    RUN_WAITING = 4
    WAIT_CANCEL = 5


class JMeterScript(IntEnum):
    """JMX 脚本生成方式，对齐 JMeterScriptEnum"""

    UPLOAD_JMX = 0
    ONLINE_JMX = 1


class JMeterThreads(IntEnum):
    """JMX 线程组类型，对齐 JMeterThreadsEnum"""

    THREAD_GROUP = 0
    STEPPING_THREAD_GROUP = 1
    CONCURRENCY_THREAD_GROUP = 2


class JMeterSample(IntEnum):
    """JMX Sample 类型，对齐 JMeterSampleEnum"""

    HTTP_REQUEST = 0
    JAVA_REQUEST = 1
    DUBBO_SAMPLE = 2


class ExecType(IntEnum):
    """报告执行类型，对齐 ExecTypeEnum / ReportTypeEnum"""

    DEBUG = 1
    EXEC = 2
