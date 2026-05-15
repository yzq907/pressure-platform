"""app.core.ssh 单元测试。

策略：用 unittest.mock 替换 paramiko.SSHClient / socket.socket，不需要真实 SSH 服务。
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock

import pytest

from app.core import ssh as ssh_mod
from app.core.codes import Codes
from app.core.exceptions import MysteriousException

# 关键：本文件测试的就是 SSHClient 自己，不能被 conftest 里的 autouse mock_ssh 影响
pytestmark = pytest.mark.real_ssh


def _fake_stdout(text: str):
    """模拟 paramiko exec_command 返回的 stdout 对象（实现了 readline）。"""
    return io.StringIO(text)


@pytest.mark.asyncio
async def test_telnet_success(monkeypatch):
    """telnet 成功"""
    sock_mock = MagicMock()
    sock_mock.connect.return_value = None
    monkeypatch.setattr(ssh_mod.socket, "socket", lambda: sock_mock)

    client = ssh_mod.SSHClient("1.2.3.4", 22, "root", "x")
    ok = await client.telnet(100)
    assert ok is True
    sock_mock.connect.assert_called_once_with(("1.2.3.4", 22))


@pytest.mark.asyncio
async def test_telnet_failure_raises_cannot_connect(monkeypatch):
    """telnet 连不上 → CANNOT_CONNECT"""
    sock_mock = MagicMock()
    sock_mock.connect.side_effect = OSError("nope")
    monkeypatch.setattr(ssh_mod.socket, "socket", lambda: sock_mock)

    client = ssh_mod.SSHClient("1.2.3.4", 22, "root", "x")
    with pytest.raises(MysteriousException) as exc:
        await client.telnet(100)
    assert exc.value.code == Codes.CANNOT_CONNECT


@pytest.mark.asyncio
async def test_exec_command_returns_first_line(monkeypatch):
    """exec_command 只取 stdout 第一行"""
    paramiko_client = MagicMock()
    paramiko_client.exec_command.return_value = (None, _fake_stdout("foo\nbar\n"), None)
    monkeypatch.setattr(ssh_mod.SSHClient, "_connect", lambda self: paramiko_client)

    client = ssh_mod.SSHClient("h", 22, "u", "p")
    out = await client.exec_command("echo hi")
    assert out == "foo"
    paramiko_client.exec_command.assert_called_once_with("echo hi")
    paramiko_client.close.assert_called_once()


@pytest.mark.asyncio
async def test_exec_command_empty_returns_null_literal(monkeypatch):
    """对齐 Java：空 stdout → 字面量 'null'"""
    paramiko_client = MagicMock()
    paramiko_client.exec_command.return_value = (None, _fake_stdout(""), None)
    monkeypatch.setattr(ssh_mod.SSHClient, "_connect", lambda self: paramiko_client)

    client = ssh_mod.SSHClient("h", 22, "u", "p")
    out = await client.exec_command("ps aux | grep jmeter")
    assert out == "null"


@pytest.mark.asyncio
async def test_exec_command_paramiko_error_raises(monkeypatch):
    """paramiko 报错 → SSH_EXEC_ERROR"""
    def boom(self):
        raise RuntimeError("conn refused")
    monkeypatch.setattr(ssh_mod.SSHClient, "_connect", boom)

    client = ssh_mod.SSHClient("h", 22, "u", "p")
    with pytest.raises(MysteriousException) as exc:
        await client.exec_command("x")
    assert exc.value.code == Codes.SSH_EXEC_ERROR


@pytest.mark.asyncio
async def test_scp_calls_mkdir_then_sftp_put(monkeypatch, tmp_path):
    """scp 先 mkdir -p remote_dir，再 sftp.put"""
    local_file = tmp_path / "data.csv"
    local_file.write_bytes(b"abc")

    # mock paramiko: 一次给 exec_command 用，一次给 sftp 用，因为代码两次 _connect
    sftp_mock = MagicMock()
    paramiko_client = MagicMock()
    paramiko_client.exec_command.return_value = (None, _fake_stdout(""), None)
    paramiko_client.open_sftp.return_value = sftp_mock
    monkeypatch.setattr(ssh_mod.SSHClient, "_connect", lambda self: paramiko_client)

    client = ssh_mod.SSHClient("h", 22, "u", "p")
    await client.scp_file(str(local_file), "/remote/dir")

    # exec_command 被调过一次（mkdir -p）
    assert paramiko_client.exec_command.call_count == 1
    cmd_arg = paramiko_client.exec_command.call_args[0][0]
    assert cmd_arg == "mkdir -p /remote/dir"

    sftp_mock.put.assert_called_once_with(str(local_file), "/remote/dir/data.csv")


@pytest.mark.asyncio
async def test_scp_swallow_paramiko_error(monkeypatch, tmp_path):
    """scp 内部 paramiko 报错只打日志，不抛"""
    local_file = tmp_path / "x.csv"
    local_file.write_bytes(b"a")

    paramiko_client = MagicMock()
    paramiko_client.exec_command.return_value = (None, _fake_stdout(""), None)
    paramiko_client.open_sftp.side_effect = RuntimeError("ssh broke")
    monkeypatch.setattr(ssh_mod.SSHClient, "_connect", lambda self: paramiko_client)

    client = ssh_mod.SSHClient("h", 22, "u", "p")
    # 不应抛异常
    await client.scp_file(str(local_file), "/remote/dir")
