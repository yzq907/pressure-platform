"""响应码常量，1:1 复刻 Java 端 ResponseCodeEnum。

Java 原始版本在 1052/1053 上有重复值（同名/同值复制粘贴的痕迹），
此处忠实保留以避免和 Java 数据/前端预期发生偏差。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Code:
    """一个响应码 = (数字码, 提示文案, success 标志)"""

    code: int
    message: str
    success: bool = False


class Codes:
    """所有响应码定义。命名与 Java ResponseCodeEnum 保持一致。"""

    SUCCESS = Code(0, "操作成功", True)
    FAIL = Code(-1, "业务异常", False)
    USER_SESSION_LOSS = Code(1000, "用户sesssion丢失", False)
    PARAMS_EMPTY = Code(1001, "参数不能为空", False)
    PARAM_WRONG = Code(1002, "参数不正确", False)
    PARAM_MISSING = Code(1003, "参数缺失", False)
    USER_EXIST = Code(1004, "用户已存在", False)
    USER_NOT_EXIST = Code(1005, "用户不存在", False)
    USER_PASSWORD_ERROR = Code(1006, "用户密码错误", False)
    USER_NOT_LOGIN = Code(1007, "用户未登录", False)
    USER_TOKEN_EXPIRE = Code(1008, "用户凭证失效", False)
    NODE_EXIST = Code(1009, "节点已存在", False)
    ID_IS_EMPTY = Code(1010, "主键为空", False)
    CONFIG_EXIST = Code(1011, "配置已存在", False)
    CONFIG_NOT_EXIST = Code(1012, "配置不存在", False)
    NODE_NOT_EXIST = Code(1013, "节点不存在", False)
    JMX_NOT_EXIST = Code(1014, "JMX脚本不存在", False)
    CSV_NAME_ERROR = Code(1015, "CSV文件名称异常", False)
    JMX_ERROR = Code(1016, "JMX脚本异常", False)
    JMX_CSV_NAME_ERROR = Code(1017, "CSV Data Set Config控件的Name没有设置", False)
    CSV_IS_EXIST = Code(1018, "CSV文件已存在", False)
    MKDIR_ERROR = Code(1019, "mkDir失败", False)
    RMDIR_ERROR = Code(1020, "rmDir失败", False)
    RMFILE_ERROR = Code(1021, "rmFileE失败", False)
    MKDIR_PARENT_ERROR = Code(1022, "mkDirParent失败", False)
    UPLOAD_FILE_ERROR = Code(1023, "uploadFile失败", False)
    COPY_FILE_ERROR = Code(1024, "copyFile失败", False)
    DOWNLOAD_FILE_ERROR = Code(1025, "downloadFileFromURL失败", False)
    FILE_NOT_EXIST = Code(1026, "文件不存在", False)
    CANNOT_CONNECT = Code(1027, "无法连通", False)
    CLOSE_CONNECT_ERROR = Code(1028, "关闭连接异常", False)
    SSH_EXEC_ERROR = Code(1029, "SSH执行命令异常", False)
    CSV_NOT_EXIST = Code(1030, "CSV文件不存在", False)
    JAR_NAME_ERROR = Code(1031, "JAR名称异常", False)
    JAR_IS_EXIST = Code(1032, "JAR文件已存在", False)
    JAR_NOT_EXIST = Code(1033, "JAR文件不存在", False)
    TESTCASE_HAS_JMX = Code(1034, "用例已经关联了JMX", False)
    JMX_NAME_ERROR = Code(1035, "JMX名称异常", False)
    JMX_HAS_JAR = Code(1036, "JMX关联了JAR", False)
    JMX_HAS_CSV = Code(1037, "JMX关联了CSV", False)
    DOWNLOAD_ERROR = Code(1038, "下载文件失败", False)
    TESTCASE_NAME_ERROR = Code(1039, "用例名称异常", False)
    TESTCASE_IS_EXIST = Code(1040, "用例已经存在", False)
    TESTCASE_NOT_EXIST = Code(1041, "用例不存在", False)
    NODE_IS_ENABLE = Code(1042, "节点启用中, 无法操作", False)
    STRESS_LOG_TOO_LARGE = Code(1043, "压测日志过大", False)
    REPORT_NOT_EXIST = Code(1044, "报告不存在", False)
    DEBUG_REPORT_NOT_DOWNLOAD = Code(1045, "调试报告不下载", False)
    REPORT_DIR_NOT_EXIST = Code(1046, "报告目录不存在", False)
    REPORT_DIR_IS_EMPTY = Code(1047, "报告目录为空", False)
    DEBUG_REPORT_NOT_VIEW = Code(1048, "调试报告直接查看结果", False)
    NODE_TYPE_ERROR = Code(1049, "节点不存在或者类型不对", False)
    NODE_CANNOT_CONNECT = Code(1050, "节点无法连通", False)
    SSH_AND_LOGIN_ERROR = Code(1051, "请确认SSH连接以及登录验证是否正确", False)
    JMETER_SERVER_NOT_FOUND = Code(1052, "找不到jmeter-server文件", False)
    JMETER_SERVER_IS_ENABLE = Code(1053, "jmeter-server已经启动", False)
    # Java 端 1052/1053 有重复，此处忠实复刻（同 code 不同 name + 文案小差异）：
    JMETER_SERVER_ENABLE_ERROR = Code(1052, "找不到jmeter-server文件", False)
    JMETER_SERVER_IS_NOT_ENABLE = Code(1052, "找不到jmeter-server文件", False)
    ONLY_SLAVE_CAN_DISABLE = Code(1053, "只能禁用slave节点", False)
    XML_ERROR = Code(1054, "xml文件异常", False)
    RUN_JMX_ERROR = Code(1055, "脚本执行异常", False)
    SCRIPT_NOT_RUNNING = Code(1056, "脚本并未执行,无法停止", False)
    DEBUG_BEFORE_RUN = Code(1057, "用例执行之前请先保证调试成功", False)
    TESTCASE_IS_RUNNING = Code(1058, "测试用例正在执行中", False)
    TESTCASE_IS_NOT_RUNNING = Code(1059, "用例此刻并不在执行, 无法停止", False)
    SCRIPT_STOP_ERROR = Code(1060, "停止脚本异常", False)
    SCRIPT_DEBUG_ERROR = Code(1061, "调试脚本异常", False)
    SCRIPT_TYPE_ERROR = Code(1062, "调试类型异常", False)
    STRESS_RESULT = Code(1063, "请根据[日志]和[预览]了解压测详情", False)

    SYSTEM_ERROR = Code(9999, "系统异常", False)
