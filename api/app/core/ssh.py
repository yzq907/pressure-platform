"""SSH 工具封装，对齐 Java SSHUtils（基于 ch.ethz.ssh2，Phase 5 改用 paramiko）。

设计要点：
- 类的接口对齐 Java：`telnet` / `exec_command` / `scp_file`
- 三个公开方法均为 async，内部用 `asyncio.to_thread` 把同步的 paramiko 调用 offload 到线程池
- `exec_command` 只取 stdout 第一行（Java 行为）；空输出时返回字面量 `"null"`，
  对齐 Java `BufferedReader.readLine() + StringBuilder.append(null)` 的最终结果，让上游可以
  按 `result == "null"` 判断"没有进程"等场景
- `scp_file` 内部错误**仅日志，不抛**，对齐 Java SCPClient 的实现
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket

import paramiko

from app.core.codes import Codes
from app.core.exceptions import MysteriousException

log = logging.getLogger(__name__)


class SSHClient:
    """密码认证的 SSH 客户端。每次调用方法都重新建立连接，对齐 Java SSHUtils 的用法。"""

    def __init__(self, host: str, port: int, username: str, password: str):
        self.host = host
        self.port = port
        self.username = username
        self.password = password

    async def telnet(self, timeout_ms: int = 200) -> bool:
        """TCP 连通性检查。失败抛 CANNOT_CONNECT。"""
        return await asyncio.to_thread(self._telnet_sync, timeout_ms)

    async def exec_command(self, command: str) -> str:
        """SSH 执行命令，只取 stdout 第一行；空输出返回 'null'（对齐 Java）。"""
        return await asyncio.to_thread(self._exec_sync, command)

    async def scp_file(self, local_path: str, remote_dir: str) -> None:
        """SCP 文件到远端目录（先 mkdir -p）。失败仅日志，不抛（对齐 Java）。"""
        await asyncio.to_thread(self._scp_sync, local_path, remote_dir)

    # --- 同步实现 ---

    def _telnet_sync(self, timeout_ms: int) -> bool:
        sock = socket.socket()
        sock.settimeout(timeout_ms / 1000)
        try:
            sock.connect((self.host, self.port))
            return True
        except OSError as e:
            log.info("telnet 失败 host=%s port=%s: %s", self.host, self.port, e)
            raise MysteriousException(Codes.CANNOT_CONNECT) from e
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _connect(self) -> paramiko.SSHClient:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            timeout=10,
            allow_agent=False,
            look_for_keys=False,
        )
        return c

    def _exec_sync(self, command: str) -> str:
        client: paramiko.SSHClient | None = None
        try:
            client = self._connect()
            _, stdout, _ = client.exec_command(command)
            line = stdout.readline().strip()
            # Java BufferedReader.readLine() 返回 null，append 后字面量 "null"
            return line if line else "null"
        except Exception as e:
            log.info("SSH exec 失败 host=%s cmd=%s: %s", self.host, command, e)
            raise MysteriousException(Codes.SSH_EXEC_ERROR) from e
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def _scp_sync(self, local_path: str, remote_dir: str) -> None:
        # 先确保远端目录存在（Java 行为）
        try:
            self._exec_sync(f"mkdir -p {remote_dir}")
        except Exception:
            log.info("mkdir 远端目录失败: %s", remote_dir)
            return

        client: paramiko.SSHClient | None = None
        try:
            client = self._connect()
            sftp = client.open_sftp()
            remote_path = remote_dir.rstrip("/") + "/" + os.path.basename(local_path)
            sftp.put(local_path, remote_path)
            sftp.close()
        except Exception:
            # 对齐 Java：scp 异常仅日志，不抛
            log.info("SCP 失败 host=%s %s -> %s", self.host, local_path, remote_dir)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
