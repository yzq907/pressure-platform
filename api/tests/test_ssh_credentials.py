"""Node passwords must never be persisted as plaintext."""

import base64
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from app.core.config import get_settings


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    path = tmp_path / "ssh-password.key"
    path.write_bytes(base64.b64encode(os.urandom(32)) + b"\n")
    path.chmod(0o600)
    monkeypatch.setattr(get_settings(), "ssh_password_key_file", str(path))
    return path


async def test_node_api_stores_ciphertext(auth_client, db, key_file):
    secret = "db-storage-secret@2026"
    response = await auth_client.post(
        "/node/add",
        json={"name": "encrypted-node", "type": 0, "host": "10.1.2.3",
              "username": "tester", "password": secret, "port": 22},
    )
    assert response.json()["code"] == 0
    node_id = response.json()["data"]
    stored = (await db.execute(
        text("SELECT password FROM mysterious_node WHERE id = :id"), {"id": node_id}
    )).scalar_one()
    assert stored.startswith("sshenc:v1:")
    assert secret not in stored
    view = await auth_client.get(f"/node/getById/{node_id}")
    assert view.json()["data"]["password"] == "******"


async def test_long_password_and_reserved_prefix_are_encrypted_as_input(auth_client, db, key_file):
    from app.core.ssh_credentials import decrypt_password

    secret = "sshenc:v1:" + "密码🔑" * 1000
    response = await auth_client.post(
        "/node/add", json={"name": "long", "type": 0, "host": "10.1.2.4",
                          "username": "tester", "password": secret, "port": 22},
    )
    assert response.json()["code"] == 0
    stored = (await db.execute(text("SELECT password FROM mysterious_node"))).scalar_one()
    assert stored != secret
    assert decrypt_password(stored) == secret
    listed = await auth_client.get("/node/list?page=1&size=10")
    assert listed.json()["data"]["list"][0]["password"] == "******"


async def test_invalid_password_input_is_not_logged_or_reflected(auth_client, caplog):
    secret = "validation-error-secret"
    with caplog.at_level("WARNING"):
        response = await auth_client.post(
            "/node/add", json={"name": "invalid", "type": 0, "host": "10.1.2.5",
                              "username": "tester", "password": [secret], "port": 22},
        )
    assert response.json()["code"] == 1002
    assert secret not in response.text
    assert secret not in caplog.text


def test_encryption_rejects_password_too_large_for_text(key_file):
    from app.core.ssh_credentials import CredentialEncryptionError, encrypt_password

    with pytest.raises(CredentialEncryptionError):
        encrypt_password("秘" * 22000)


@pytest.mark.parametrize("secret", ["", "p@ss with spaces ", "密码🔑" * 1000, "sshenc:v1:input"],
                         ids=["empty", "spaces", "long-unicode", "reserved-prefix"])
def test_envelope_roundtrip_and_randomness(key_file, secret):
    from app.core.ssh_credentials import decrypt_password, encrypt_password

    first = encrypt_password(secret)
    second = encrypt_password(secret)
    assert decrypt_password(first) == secret
    if secret:
        assert first.startswith("sshenc:v1:")
        assert first != second
        assert secret not in first
    else:
        assert first == second == ""


@pytest.mark.parametrize("value", ["plaintext-secret", "sshenc:v2:AAAA", "sshenc:v1:bad!", "sshenc:v1:AAAA"])
def test_decrypt_refuses_plaintext_or_invalid_envelope(key_file, value):
    from app.core.ssh_credentials import CredentialEncryptionError, decrypt_password

    with pytest.raises(CredentialEncryptionError) as error:
        decrypt_password(value)
    assert value not in str(error.value)


def test_wrong_key_and_tampered_envelope_fail_closed(key_file):
    from app.core.ssh_credentials import CredentialEncryptionError, decrypt_password, encrypt_password

    secret = "never-log-this-secret"
    envelope = encrypt_password(secret)
    raw = bytearray(base64.b64decode(envelope.removeprefix("sshenc:v1:")))
    raw[-1] ^= 1
    with pytest.raises(CredentialEncryptionError):
        decrypt_password("sshenc:v1:" + base64.b64encode(raw).decode())
    key_file.write_bytes(base64.b64encode(os.urandom(32)))
    with pytest.raises(CredentialEncryptionError) as error:
        decrypt_password(envelope)
    assert secret not in str(error.value)
    assert envelope not in str(error.value)


@pytest.mark.parametrize("offset", [0, 12, 60, 72])
def test_every_envelope_component_is_authenticated(key_file, offset):
    from app.core.ssh_credentials import CredentialEncryptionError, decrypt_password, encrypt_password

    envelope = encrypt_password("sample")
    raw = bytearray(base64.b64decode(envelope.removeprefix("sshenc:v1:")))
    raw[offset] ^= 1
    with pytest.raises(CredentialEncryptionError):
        decrypt_password("sshenc:v1:" + base64.b64encode(raw).decode())


@pytest.mark.parametrize("problem", ["unset", "missing", "permissions", "invalid", "symlink", "directory"])
def test_key_validation(key_file, monkeypatch, tmp_path, problem):
    from app.core.ssh_credentials import CredentialEncryptionError, load_master_key

    if problem == "unset":
        monkeypatch.setattr(get_settings(), "ssh_password_key_file", "")
    elif problem == "missing":
        key_file.unlink()
    elif problem == "permissions":
        key_file.chmod(0o644)
    elif problem == "invalid":
        key_file.write_text("invalid-key-content")
    elif problem == "symlink":
        link = tmp_path / "link.key"
        link.symlink_to(key_file)
        monkeypatch.setattr(get_settings(), "ssh_password_key_file", str(link))
    else:
        monkeypatch.setattr(get_settings(), "ssh_password_key_file", str(tmp_path))
    with pytest.raises(CredentialEncryptionError) as error:
        load_master_key()
    assert "invalid-key-content" not in str(error.value)


def test_key_creation_is_private_and_never_overwrites(tmp_path, monkeypatch):
    from app.core.ssh_credentials import CredentialEncryptionError, initialize_key_file, load_master_key

    path = tmp_path / "new.key"
    initialize_key_file(path)
    original = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(get_settings(), "ssh_password_key_file", str(path))
    assert len(load_master_key()) == 32
    with pytest.raises(CredentialEncryptionError):
        initialize_key_file(path)
    assert path.read_bytes() == original


def test_key_creation_refuses_symlinks_and_missing_parent(tmp_path):
    from app.core.ssh_credentials import CredentialEncryptionError, initialize_key_file

    target = tmp_path / "target"
    target.write_text("unchanged")
    link = tmp_path / "link.key"
    link.symlink_to(target)
    for path in [link, tmp_path / "missing" / "key"]:
        with pytest.raises(CredentialEncryptionError):
            initialize_key_file(path)
    assert target.read_text() == "unchanged"


@pytest.mark.real_ssh
def test_ssh_decrypts_only_at_connect_boundary(key_file, monkeypatch):
    from app.core import ssh
    from app.core.ssh_credentials import encrypt_password

    secret = "connection-only-secret"
    encrypted = encrypt_password(secret)
    transport = MagicMock()
    monkeypatch.setattr(ssh.paramiko, "SSHClient", lambda: transport)
    client = ssh.SSHClient("10.1.2.3", 22, "tester", encrypted)
    assert client.password == encrypted
    assert client._connect() is transport
    assert transport.connect.call_args.kwargs["password"] == secret
    assert client.password == encrypted


@pytest.mark.real_ssh
def test_invalid_password_does_not_open_ssh_connection(key_file, monkeypatch):
    from app.core import ssh
    from app.core.ssh_credentials import CredentialEncryptionError

    factory = MagicMock()
    monkeypatch.setattr(ssh.paramiko, "SSHClient", factory)
    with pytest.raises(CredentialEncryptionError):
        ssh.SSHClient("10.1.2.3", 22, "tester", "legacy-plain")._connect()
    factory.assert_not_called()


@pytest.mark.real_ssh
def test_ssh_error_logging_does_not_disclose_credentials(key_file, caplog):
    from app.core.exceptions import MysteriousException
    from app.core.ssh import SSHClient
    from app.core.ssh_credentials import encrypt_password

    secret = "log-safety-secret"
    encrypted = encrypt_password(secret)
    key_file.write_bytes(base64.b64encode(os.urandom(32)))
    with caplog.at_level("INFO"), pytest.raises(MysteriousException):
        SSHClient("10.1.2.3", 22, "tester", encrypted)._exec_sync("true")
    assert secret not in caplog.text
    assert encrypted not in caplog.text
    assert key_file.read_text().strip() not in caplog.text


def test_key_cli_never_starts_app_or_overwrites(tmp_path):
    path = tmp_path / "cli.key"
    env = {**os.environ, "MYSTERIOUS_DATA_DIR": "", "MYSTERIOUS_CONFIG_FILE": "/nonexistent"}
    command = [sys.executable, "launcher.py", "--init-ssh-key", str(path)]
    first = subprocess.run(command, cwd=Path(__file__).parents[1], env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    original = path.read_text()
    assert original.strip() not in first.stdout + first.stderr
    second = subprocess.run(command, cwd=Path(__file__).parents[1], env=env, capture_output=True, text=True)
    assert second.returncode != 0
    assert path.read_text() == original
    assert original.strip() not in second.stdout + second.stderr


async def test_migration_encrypts_plaintext_and_is_idempotent(db, key_file):
    from app.core.ssh_credentials import decrypt_password, encrypt_password
    from app.models.node import Node
    from app.services.node_credentials import migrate_node_passwords

    encrypted = encrypt_password("already-encrypted")
    rows = [Node(host="10.2.0.1", password="legacy-secret"),
            Node(host="10.2.0.2", password=encrypted), Node(host="10.2.0.3", password="")]
    db.add_all(rows)
    await db.commit()
    assert await migrate_node_passwords(db) == 1
    values = (await db.execute(text("SELECT password FROM mysterious_node ORDER BY id"))).scalars().all()
    assert [decrypt_password(value) for value in values] == ["legacy-secret", "already-encrypted", ""]
    assert values[1] == encrypted
    await db.rollback()
    assert await migrate_node_passwords(db) == 0
    assert (await db.execute(text("SELECT password FROM mysterious_node ORDER BY id"))).scalars().all() == values


@pytest.mark.parametrize("failure", ["tampered", "wrong-key"])
async def test_migration_is_all_or_nothing(db, key_file, failure):
    from app.core.ssh_credentials import CredentialEncryptionError, encrypt_password
    from app.models.node import Node
    from app.services.node_credentials import migrate_node_passwords

    encrypted = encrypt_password("encrypted-secret")
    if failure == "tampered":
        encrypted = "sshenc:v1:broken"
    else:
        key_file.write_bytes(base64.b64encode(os.urandom(32)))
    db.add_all([Node(host="10.3.0.1", password="legacy-secret"),
                Node(host="10.3.0.2", password=encrypted)])
    await db.commit()
    with pytest.raises(CredentialEncryptionError):
        await migrate_node_passwords(db)
    values = (await db.execute(text("SELECT password FROM mysterious_node ORDER BY id"))).scalars().all()
    assert values == ["legacy-secret", encrypted]


async def test_startup_missing_key_stops_before_schema_or_recovery(monkeypatch):
    from app import main
    from app.core.ssh_credentials import CredentialEncryptionError
    from app.db import schema

    monkeypatch.setattr(get_settings(), "ssh_password_key_file", "")
    unexpected = MagicMock(side_effect=AssertionError("must not reach database/recovery"))
    monkeypatch.setattr(schema, "ensure_config_value_text_column", unexpected)
    monkeypatch.setattr(main, "_recover_stuck_tasks", unexpected)
    with pytest.raises(CredentialEncryptionError):
        async with main.lifespan(main.app):
            pytest.fail("startup must fail")
    unexpected.assert_not_called()


async def test_startup_corrupt_ciphertext_stops_before_recovery(db, key_file, monkeypatch):
    from app import main
    from app.core.ssh_credentials import CredentialEncryptionError
    from app.db import schema
    from app.models.node import Node

    db.add(Node(host="10.5.0.1", password="sshenc:v1:bad"))
    await db.commit()

    async def schema_ready():
        pass

    monkeypatch.setattr(schema, "ensure_node_password_text_column", schema_ready)
    unexpected = MagicMock(side_effect=AssertionError("no recovery after failed migration"))
    monkeypatch.setattr(main, "_recover_stuck_tasks", unexpected)
    monkeypatch.setattr(schema, "ensure_config_value_text_column", unexpected)
    with pytest.raises(CredentialEncryptionError):
        async with main.lifespan(main.app):
            pytest.fail("startup must fail")
    unexpected.assert_not_called()


def test_password_column_has_enough_room_for_envelopes():
    from sqlalchemy import Text

    from app.models.node import Node

    assert isinstance(Node.__table__.c.password.type, Text)
    # MySQL does not accept a literal DEFAULT '' on TEXT across supported versions.
    assert Node.__table__.c.password.server_default is None


def test_bootstrap_node_schema_matches_model_columns():
    import re

    from app.models.node import Node

    sql = (Path(__file__).parents[2] / "docker" / "init.sql").read_text()
    table = sql.split("CREATE TABLE `mysterious_node` (", 1)[1].split(") ENGINE=", 1)[0]
    columns = set(re.findall(r"^\s*`(\w+)`", table, flags=re.MULTILINE))
    assert set(Node.__table__.c.keys()) <= columns


@pytest.mark.parametrize("column_type,expected_changes", [("VARCHAR(128)", 1), ("TEXT", 0)])
async def test_mysql_password_schema_upgrade_is_idempotent(monkeypatch, column_type, expected_changes):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.db import schema

    conn = SimpleNamespace(dialect=SimpleNamespace(name="mysql"),
                           run_sync=AsyncMock(return_value={"password": {"type": column_type}}),
                           execute=AsyncMock())
    context = MagicMock()
    context.__aenter__.return_value = conn
    monkeypatch.setattr(schema, "async_engine", SimpleNamespace(begin=lambda: context))
    await schema.ensure_node_password_text_column()
    assert conn.execute.await_count == expected_changes
    if expected_changes:
        assert "MODIFY COLUMN password TEXT NOT NULL" in str(conn.execute.call_args.args[0])


async def test_migration_requires_only_legacy_id_and_password_columns(key_file):
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.core.ssh_credentials import decrypt_password
    from app.services.node_credentials import migrate_node_passwords

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE mysterious_node (id INTEGER PRIMARY KEY, password TEXT NOT NULL)"))
            await conn.execute(text("INSERT INTO mysterious_node VALUES (1, 'legacy-secret')"))
        async with AsyncSession(engine) as session:
            assert await migrate_node_passwords(session) == 1
            stored = (await session.execute(text("SELECT password FROM mysterious_node"))).scalar_one()
            assert decrypt_password(stored) == "legacy-secret"
    finally:
        await engine.dispose()


async def test_database_write_failure_rolls_back_all_passwords(db, key_file):
    from sqlalchemy.exc import IntegrityError

    from app.models.node import Node
    from app.services.node_credentials import migrate_node_passwords

    db.add_all([Node(host="10.8.0.1", password="first-legacy"), Node(host="10.8.0.2", password="second-legacy")])
    await db.commit()
    await db.execute(text("""CREATE TRIGGER reject_second_password BEFORE UPDATE OF password ON mysterious_node
        WHEN OLD.password = 'second-legacy' BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END"""))
    await db.commit()
    try:
        with pytest.raises(IntegrityError):
            await migrate_node_passwords(db)
        stored = (await db.execute(text("SELECT password FROM mysterious_node ORDER BY id"))).scalars().all()
        assert stored == ["first-legacy", "second-legacy"]
    finally:
        await db.execute(text("DROP TRIGGER reject_second_password"))
        await db.commit()
