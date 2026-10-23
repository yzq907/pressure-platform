"""Validation and quoting for POSIX commands sent to JMeter pressure nodes."""

import ipaddress
import re
import shlex

from app.core.codes import Codes
from app.core.exceptions import MysteriousException

_DIRECTORY_RE = re.compile(r"/[\w .()/\-]+")
_HOST_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def _absolute_path_parts(value: str, label: str) -> list[str]:
    if (not value or not value.startswith("/") or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise MysteriousException(Codes.PARAM_WRONG, message=f"{label} 必须是非空 Linux 绝对路径，不能包含控制字符")
    parts = value.rstrip("/").split("/")[1:]
    if not parts or any(part in ("", ".", "..") or not part.strip() for part in parts):
        raise MysteriousException(Codes.PARAM_WRONG, message=f"{label} 不能是根目录或包含空目录、.、.. 路径段")
    return parts


def validate_directory(value: str, label: str = "目录配置") -> str:
    """Allow literal directory names, not shell expressions or path traversal."""
    _absolute_path_parts(value, label)
    if not _DIRECTORY_RE.fullmatch(value):
        raise MysteriousException(
            Codes.PARAM_WRONG,
            message=f"{label} 仅允许字母、数字、中文、空格、下划线、连字符、点、括号和目录分隔符，不能包含 Shell 表达式",
        )
    return value


def validate_node_host(host: str) -> str:
    """Accept a bare IP address or DNS hostname, never URLs, ports or options."""
    try:
        ipaddress.ip_address(host)
        # Scoped IPv6 zone IDs are not supported as JMeter RMI hostnames.
        if "%" not in host:
            return host
    except ValueError:
        pass
    labels = host.removesuffix(".").split(".")
    if (host and len(host) <= 253 and not re.fullmatch(r"[0-9.]+", host)
            and all(_HOST_LABEL_RE.fullmatch(label) for label in labels)):
        return host
    raise MysteriousException(Codes.PARAM_WRONG, message="节点地址必须是有效的 IP 或主机名，不能包含端口、空格或 Shell 表达式")


def jmeter_server_path(slave_bin: str) -> str:
    return validate_directory(slave_bin, "SLAVE_JMETER_BIN_HOME").rstrip("/") + "/jmeter-server"


def build_jmeter_server_start(slave_bin: str, slave_log: str, host: str) -> str:
    """Shared by manual enable, heartbeat and remote-engine restart."""
    server = jmeter_server_path(slave_bin)
    log_dir = validate_directory(slave_log, "SLAVE_JMETER_LOG_HOME").rstrip("/")
    validate_node_host(host)
    return (
        f"mkdir -p -- {shlex.quote(log_dir)} && cd -- {shlex.quote(log_dir)} && "
        f"nohup {shlex.quote(server)} {shlex.quote(f'-Djava.rmi.server.hostname={host}')} "
        "> jmeter-server.log 2>&1 & echo $!"
    )


def build_remote_file_remove(remote_path: str) -> str:
    """Remove one literal file; never recurse or interpret options/globs."""
    _absolute_path_parts(remote_path, "远程删除文件路径")
    return f"rm -f -- {shlex.quote(remote_path)}"
