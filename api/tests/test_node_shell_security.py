"""Shell injection regressions. SSH is mocked; no pressure nodes are contacted."""

import shlex
import subprocess
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.context import UserContext
from app.core.exceptions import MysteriousException
from app.core.shell_safety import build_jmeter_server_start, build_remote_file_remove, validate_node_host
from app.core.ssh import SSHClient
from app.models.config import Config
from app.models.node import Node
from app.models.role import Role, RolePermission
from app.models.user import User
from app.schemas.config import ConfigParam
from app.services import config, node, node_heartbeat, testcase

USER = UserContext(id=1, username="test")
BAD_PATHS = (
    "/opt/jmeter;echo injected", "/opt/$(echo injected)", "/opt/`echo injected`",
    "/opt/jmeter\necho injected", "/opt/jmeter|echo injected", "/opt/jmeter&echo injected",
    "/opt/jmeter>output", "/opt/'jmeter'", "/opt/\"jmeter\"", "/opt/jmeter\\bin",
    "/opt/*/bin", "/opt/jmeter/../bin", "/opt/./bin", "relative/bin", "~/jmeter", "/", "",
)


@pytest.mark.parametrize("path", BAD_PATHS)
@pytest.mark.asyncio
async def test_path_config_rejects_shell_syntax_and_unsafe_paths(db, path):
    with pytest.raises(MysteriousException):
        await config.add_config(db, ConfigParam(config_key="SLAVE_JMETER_BIN_HOME", config_value=path,
                                               description="bin"), USER)
    assert (await db.execute(select(Config))).scalars().all() == []


@pytest.mark.asyncio
async def test_partial_config_updates_validate_effective_key_and_value(db):
    item = Config(config_key="SLAVE_JMETER_BIN_HOME", config_value="/opt/jmeter/bin", description="bin")
    other = Config(config_key="CUSTOM", config_value="/opt/jmeter;echo injected", description="custom")
    db.add_all([item, other])
    await db.commit()
    with pytest.raises(MysteriousException):
        await config.update_config(db, item.id, ConfigParam(config_value="/opt/$(echo injected)"), USER)
    assert item.config_value == "/opt/jmeter/bin"
    with pytest.raises(MysteriousException):
        await config.update_config(db, other.id, ConfigParam(config_key="SLAVE_JMETER_LOG_HOME"), USER)
    assert other.config_key == "CUSTOM"


@pytest.mark.parametrize("getter", [config.get_value, config.get_value_or_default])
@pytest.mark.asyncio
async def test_legacy_invalid_path_config_is_rejected_on_read(db, getter):
    db.add(Config(config_key="SLAVE_JMETER_BIN_HOME", config_value="/opt/jmeter;echo injected"))
    await db.commit()
    with pytest.raises(MysteriousException):
        await getter(db, "SLAVE_JMETER_BIN_HOME")


@pytest.mark.asyncio
async def test_path_defaults_and_environment_override_are_validated(db, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", "")
    with pytest.raises(MysteriousException):
        await config.get_value_or_default(db, "MASTER_DATA_HOME", "/opt/$(echo injected)")
    monkeypatch.setattr(get_settings(), "data_dir", "/opt/$(echo injected)")
    with pytest.raises(MysteriousException):
        await config.get_value(None, "MASTER_DATA_HOME")


@pytest.mark.parametrize("key", ["MASTER_JMETER_HOME", "MASTER_JMETER_BIN_HOME", "MASTER_DATA_HOME",
                                "MASTER_BASE_JMX_FILES_PATH", "AI_GENERATION_WORK_DIR"])
@pytest.mark.asyncio
async def test_all_catalog_path_configs_are_validated(db, key):
    with pytest.raises(MysteriousException):
        await config.add_config(db, ConfigParam(config_key=key, config_value="/opt/jmeter;echo injected",
                                               description="path"), USER)


@pytest.mark.asyncio
async def test_safe_path_with_spaces_is_preserved(db):
    key = "SLAVE_JMETER_BIN_HOME"
    path = "/opt/Apache JMeter-5.6.3/bin/"
    await config.add_config(db, ConfigParam(config_key=key, config_value=path, description="bin"), USER)
    assert await config.get_value(db, key) == path


@pytest.mark.asyncio
async def test_enable_node_quotes_paths_with_spaces(db, monkeypatch):
    db.add_all([Config(config_key="SLAVE_JMETER_BIN_HOME", config_value="/opt/Apache JMeter/bin/"),
                Config(config_key="SLAVE_JMETER_LOG_HOME", config_value="/opt/JMeter logs/")])
    obj = Node(host="loadgen-01.internal", username="u", port=22, type=0, status=0)
    db.add(obj)
    await db.commit()
    commands = []
    original = SSHClient.exec_command

    async def capture(self, command):
        commands.append(command)
        return await original(self, command)

    monkeypatch.setattr(SSHClient, "exec_command", capture)
    assert await node.enable_node(db, obj.id, USER)
    md5 = next(cmd for cmd in commands if cmd.startswith("md5sum"))
    assert shlex.split(md5)[:3] == ["md5sum", "--", "/opt/Apache JMeter/bin/jmeter-server"]
    start = next(cmd for cmd in commands if "nohup" in cmd)
    tokens = shlex.split(start)
    assert tokens[tokens.index("nohup") + 1] == "/opt/Apache JMeter/bin/jmeter-server"
    assert tokens[tokens.index("cd") + 1:tokens.index("cd") + 3] == ["--", "/opt/JMeter logs"]


@pytest.mark.parametrize("entry", ["enable", "heartbeat", "stop"])
@pytest.mark.parametrize("field", ["host", "bin", "log"])
@pytest.mark.asyncio
async def test_all_start_paths_reject_legacy_malicious_input_before_ssh(db, monkeypatch, entry, field):
    host = "127.0.0.1;echo injected" if field == "host" else "127.0.0.1"
    slave_bin = "/opt/jmeter;echo injected" if field == "bin" else "/opt/jmeter/bin"
    slave_log = "/opt/log;echo injected" if field == "log" else "/opt/log"
    obj = Node(host=host, username="u", port=22, type=0, status=0)
    db.add(obj)
    db.add_all([Config(config_key="SLAVE_JMETER_BIN_HOME", config_value=slave_bin),
                Config(config_key="SLAVE_JMETER_LOG_HOME", config_value=slave_log)])
    await db.commit()
    commands = []

    async def capture(self, command):
        commands.append(command)
        return "null"

    monkeypatch.setattr(SSHClient, "exec_command", capture)
    with pytest.raises(MysteriousException):
        if entry == "enable":
            await node.enable_node(db, obj.id, USER)
        elif entry == "heartbeat":
            await node_heartbeat._ensure_jmeter_server_running(SSHClient(obj.host, 22, "u", ""), obj,
                                                             slave_bin=slave_bin, slave_log=slave_log)
        else:
            await testcase._stop_remote_engine(obj, slave_bin, slave_log)
    assert commands == []


@pytest.mark.parametrize("path", ["/", "//", "/opt/../", "relative.jar", "-rf"])
@pytest.mark.asyncio
async def test_remote_delete_rejects_dangerous_targets(db, monkeypatch, path):
    with pytest.raises(MysteriousException):
        await node.rm_on_enabled_slaves(db, path)


@pytest.mark.asyncio
async def test_remote_delete_quotes_filename_as_one_operand(db, monkeypatch):
    obj = Node(host="10.0.0.1", username="u", port=22, type=0, status=1)
    db.add(obj)
    await db.commit()
    commands = []

    async def capture(self, command):
        commands.append(command)
        return "null"

    monkeypatch.setattr(SSHClient, "exec_command", capture)
    path = "/opt/jmeter/lib/ext/test;echo injected.jar"
    await node.rm_on_enabled_slaves(db, path)
    assert len(commands) == 1
    assert shlex.split(commands[0]) == ["rm", "-f", "--", path]


@pytest.mark.parametrize("host", ["host;echo injected", "$(echo injected)", "host\nnext", "-host", "host:1099"])
@pytest.mark.asyncio
async def test_node_create_and_partial_update_reject_invalid_host(auth_client, host):
    response = await auth_client.post("/node/add", json={"host": host, "username": "u", "port": 22, "type": 0})
    assert response.json()["code"] != 0
    valid = await auth_client.post("/node/add", json={"host": "10.0.0.1", "username": "u", "port": 22, "type": 0})
    assert valid.json()["code"] == 0
    response = await auth_client.post(f"/node/update/{valid.json()['data']}", json={"host": host})
    assert response.json()["code"] != 0


@pytest.mark.parametrize("host", ["10.10.27.97", "loadgen-01.internal", "localhost", "2001:db8::1"])
def test_valid_ip_and_dns_hosts_are_preserved(host):
    assert validate_node_host(host) == host


@pytest.mark.parametrize("name", ["test;printf INJECTED.jar", "test$(printf INJECTED).jar",
                                 "test`printf INJECTED`.jar", "test'quoted.jar", "test*.jar"])
def test_remote_file_remove_does_not_execute_filename_in_real_shell(name):
    path = "/opt/jmeter/lib/ext/" + name
    # Replace rm with a harmless shell function. No actual deletion is performed.
    command = 'rm() { printf "<%s>\\n" "$@"; }; ' + build_remote_file_remove(path)
    result = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, check=True, timeout=5)
    assert result.stdout.splitlines() == ["<-f>", "<-->", f"<{path}>"]


def test_start_command_preserves_chinese_spaces_and_parentheses_in_real_shell(tmp_path):
    command = build_jmeter_server_start("/opt/压测工具 (JMeter)/bin/", "/opt/JMeter logs/", "loadgen-01")
    # Stub every filesystem/process operation; output redirection is confined to pytest tmp_path.
    prefix = 'mkdir() { :; }; cd() { :; }; nohup() { printf "<%s>\\n" "$@"; }; '
    subprocess.run(["/bin/sh", "-c", prefix + command], cwd=tmp_path,
                   capture_output=True, check=True, timeout=5)
    assert (tmp_path / "jmeter-server.log").read_text().splitlines() == [
        "</opt/压测工具 (JMeter)/bin/jmeter-server>", "<-Djava.rmi.server.hostname=loadgen-01>",
    ]


@pytest.mark.asyncio
async def test_config_only_user_cannot_save_shell_expression(db):
    from app.main import app

    role = Role(name="config-only", code="config-only")
    db.add(role)
    await db.flush()
    db.add(RolePermission(role_id=role.id, permission_code="config"))
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    db.add(User(username="config-only", password="unused", role_id=role.id,
                token="config-only-token", expire_time=now + timedelta(hours=1)))
    await db.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"token": "config-only-token"}) as client:
        assert (await client.get("/node/list")).json()["code"] != 0
        body = {"configKey": "SLAVE_JMETER_BIN_HOME", "configValue": "/opt/jmeter;echo injected",
                "description": "bin"}
        assert (await client.post("/config/add", json=body)).json()["code"] != 0
        body["configValue"] = "/opt/jmeter/bin"
        assert (await client.post("/config/add", json=body)).json()["code"] == 0
