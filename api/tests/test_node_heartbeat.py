"""Concurrent SSH must never share a database session or hold a DB transaction."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.enums import NodeStatus, NodeType
from app.db import session as session_module
from app.db.base import Base
from app.models.config import Config
from app.models.node import Node
from app.services import node_heartbeat as heartbeat


@pytest.fixture(autouse=True)
def capture_heartbeat_logs(caplog):
    # App logger deliberately does not propagate to root in deployed logging config.
    heartbeat.log.addHandler(caplog.handler)
    yield
    heartbeat.log.removeHandler(caplog.handler)


@pytest.fixture
async def heartbeat_db(monkeypatch):
    """Dedicated engine, so deliberately failing transactions cannot poison other tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    state = {"open_sessions": 0, "write_commits": 0}

    class TrackedSession(AsyncSession):
        async def commit(self):
            state["write_commits"] += 1
            await super().commit()

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=TrackedSession)

    @asynccontextmanager
    async def tracked_factory():
        async with factory() as db:
            state["open_sessions"] += 1
            try:
                yield db
            finally:
                state["open_sessions"] -= 1

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(session_module, "AsyncSessionLocal", tracked_factory)
    monkeypatch.setattr(heartbeat, "_JMETER_SERVER_START_WAIT_SECONDS", 0)
    async with factory() as db:
        db.add_all(
            [
                Config(config_key="SLAVE_JMETER_BIN_HOME", config_value="/opt/jmeter/bin"),
                Config(config_key="SLAVE_JMETER_LOG_HOME", config_value="/opt/jmeter/log"),
            ]
        )
        await db.commit()
    state["write_commits"] = 0
    yield factory, state
    await engine.dispose()


async def seed_nodes(factory, count=1):
    async with factory() as db:
        nodes = [
            Node(
                name=f"slave-{i}",
                host=f"10.30.0.{i + 1}",
                type=NodeType.SLAVE.value,
                port=22,
                username="tester",
                password="",
                status=NodeStatus.ENABLE.value,
                health_status=0,
                cpu_usage=1,
                mem_usage=2,
                load_avg=3,
            )
            for i in range(count)
        ]
        db.add_all(nodes)
        await db.commit()
        return [node.id for node in nodes]


def fake_metrics(monkeypatch):
    async def exec_command(self, command):
        if "loadavg" in command:
            return "0.25"
        if command.startswith("free "):
            return "20.0"
        if command.startswith("top "):
            return "90.0"
        if "ApacheJMeter.jar" in command:
            return "running"
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(heartbeat.SSHClient, "exec_command", exec_command)


async def test_parallel_probes_release_read_session_and_commit_once(heartbeat_db, monkeypatch):
    factory, state = heartbeat_db
    await seed_nodes(factory, 8)
    state["write_commits"] = 0
    entered, release = asyncio.Event(), asyncio.Event()
    active = maximum = 0
    open_sessions_during_probe = []

    async def telnet(self, timeout_ms=3000):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        open_sessions_during_probe.append(state["open_sessions"])
        if active == 5:
            entered.set()
        try:
            await release.wait()
        finally:
            active -= 1
        return True

    fake_metrics(monkeypatch)
    monkeypatch.setattr(heartbeat.SSHClient, "telnet", telnet)
    task = asyncio.create_task(heartbeat._heartbeat_round())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert maximum == 5
        assert state["write_commits"] == 0
    finally:
        release.set()
        await asyncio.wait_for(task, 3)
    assert open_sessions_during_probe == [0] * 8
    assert state["write_commits"] == 1
    async with factory() as db:
        nodes = (await db.execute(select(Node))).scalars().all()
        assert len(nodes) == 8
        assert all(node.health_status == 1 and node.last_heartbeat is not None for node in nodes)
        assert all((node.cpu_usage, node.mem_usage, node.load_avg) == (10, 20, 0.25) for node in nodes)


async def test_failed_ssh_does_not_affect_other_nodes(heartbeat_db, monkeypatch):
    factory, _ = heartbeat_db
    ids = await seed_nodes(factory, 3)

    async def telnet(self, timeout_ms=3000):
        if self.host.endswith(".2"):
            raise OSError("unreachable")
        return True

    fake_metrics(monkeypatch)
    monkeypatch.setattr(heartbeat.SSHClient, "telnet", telnet)
    await heartbeat._heartbeat_round()
    async with factory() as db:
        nodes = (await db.execute(select(Node).order_by(Node.id))).scalars().all()
        assert [node.id for node in nodes] == ids
        assert [node.health_status for node in nodes] == [1, 0, 1]
        assert all(node.last_heartbeat is not None for node in nodes)
        assert (nodes[1].cpu_usage, nodes[1].mem_usage, nodes[1].load_avg) == (1, 2, 3)


async def test_repeated_rounds_update_existing_heartbeat(heartbeat_db, monkeypatch):
    factory, state = heartbeat_db
    node_id = (await seed_nodes(factory))[0]
    fake_metrics(monkeypatch)
    clock = iter([datetime(2026, 10, 10, 12, 0, 0, 123456), datetime(2026, 10, 10, 12, 1, 0, 987654)])
    monkeypatch.setattr(heartbeat, "_now", lambda: next(clock))
    state["write_commits"] = 0
    await heartbeat._heartbeat_round()
    await heartbeat._heartbeat_round()
    assert state["write_commits"] == 2
    async with factory() as db:
        node = await db.get(Node, node_id)
        assert node.health_status == 1
        assert node.last_heartbeat == datetime(2026, 10, 10, 12, 1, 0, 987654)


@pytest.mark.parametrize(
    "change", ["disable", "delete", "host", "port", "username", "password", "modified", "new-heartbeat"]
)
async def test_obsolete_results_are_not_written(heartbeat_db, monkeypatch, change):
    factory, state = heartbeat_db
    node_id = (await seed_nodes(factory))[0]
    entered, release = asyncio.Event(), asyncio.Event()

    async def telnet(self, timeout_ms=3000):
        entered.set()
        await release.wait()
        return True

    fake_metrics(monkeypatch)
    monkeypatch.setattr(heartbeat.SSHClient, "telnet", telnet)
    task = asyncio.create_task(heartbeat._heartbeat_round())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with factory() as db:
            node = await db.get(Node, node_id)
            if change == "delete":
                await db.delete(node)
            elif change == "disable":
                node.status = NodeStatus.DISABLED.value
            elif change == "modified":
                node.modify_time = datetime(2040, 1, 1)
            elif change == "new-heartbeat":
                node.last_heartbeat = datetime(2040, 1, 1)
                node.health_status = 0
            else:
                setattr(
                    node,
                    change,
                    {
                        "host": "10.40.0.1",
                        "port": 2222,
                        "username": "new-user",
                        "password": "changed-ciphertext",
                    }[change],
                )
            await db.commit()
    finally:
        release.set()
        await asyncio.wait_for(task, 3)
    async with factory() as db:
        node = await db.get(Node, node_id)
        if change == "delete":
            assert node is None
        else:
            assert node.health_status == 0
            assert node.last_heartbeat == (datetime(2040, 1, 1) if change == "new-heartbeat" else None)
            assert (node.cpu_usage, node.mem_usage, node.load_avg) == (1, 2, 3)


async def test_database_failure_rolls_back_entire_round(heartbeat_db, monkeypatch, caplog):
    factory, _ = heartbeat_db
    await seed_nodes(factory, 2)
    async with factory() as db:
        await db.execute(
            text("""CREATE TRIGGER fail_second BEFORE UPDATE OF health_status ON mysterious_node
            WHEN OLD.host = '10.30.0.2' BEGIN SELECT RAISE(ABORT, 'simulated database failure'); END""")
        )
        await db.commit()
    fake_metrics(monkeypatch)
    with caplog.at_level("ERROR"), pytest.raises(RuntimeError, match="心跳数据库写入失败"):
        await heartbeat._heartbeat_round()
    assert "rolled back" in caplog.text
    async with factory() as db:
        nodes = (await db.execute(select(Node))).scalars().all()
        assert all(node.health_status == 0 and node.last_heartbeat is None for node in nodes)


async def test_commit_failure_rolls_back_without_logging_sql_credentials(heartbeat_db, monkeypatch, caplog):
    from sqlalchemy.exc import StatementError

    factory, _ = heartbeat_db
    await seed_nodes(factory, 2)
    fake_metrics(monkeypatch)
    secret = "database-parameter-credential"

    async def fail_commit(db):
        raise StatementError("failed", "UPDATE mysterious_node", {"password": secret}, RuntimeError(secret))

    monkeypatch.setattr(factory.class_, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="心跳数据库写入失败") as error:
        await heartbeat._heartbeat_round()
    assert secret not in str(error.value)
    assert secret not in caplog.text
    assert "rolled back" in caplog.text
    async with factory() as db:
        nodes = (await db.execute(select(Node))).scalars().all()
        assert all(node.health_status == 0 and node.last_heartbeat is None for node in nodes)


async def test_cancellation_does_not_commit_partial_results(heartbeat_db, monkeypatch):
    factory, state = heartbeat_db
    await seed_nodes(factory, 2)
    state["write_commits"] = 0
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def telnet(self, timeout_ms=3000):
        if self.host.endswith(".2"):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return True

    fake_metrics(monkeypatch)
    monkeypatch.setattr(heartbeat.SSHClient, "telnet", telnet)
    task = asyncio.create_task(heartbeat._heartbeat_round())
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert state["write_commits"] == 0
    async with factory() as db:
        nodes = (await db.execute(select(Node))).scalars().all()
        assert all(node.health_status == 0 and node.last_heartbeat is None for node in nodes)


async def test_only_enabled_slaves_are_probed(heartbeat_db, monkeypatch):
    factory, _ = heartbeat_db
    ids = await seed_nodes(factory, 3)
    async with factory() as db:
        await db.execute(update(Node).where(Node.id == ids[1]).values(status=NodeStatus.DISABLED.value))
        await db.execute(update(Node).where(Node.id == ids[2]).values(type=NodeType.MASTER.value))
        await db.commit()
    hosts = []

    async def telnet(self, timeout_ms=3000):
        hosts.append(self.host)
        return True

    fake_metrics(monkeypatch)
    monkeypatch.setattr(heartbeat.SSHClient, "telnet", telnet)
    await heartbeat._heartbeat_round()
    assert hosts == ["10.30.0.1"]


async def test_empty_round_does_not_probe_or_commit(heartbeat_db, monkeypatch):
    factory, state = heartbeat_db

    async def unexpected_probe(*args, **kwargs):
        pytest.fail("empty round must not probe")

    monkeypatch.setattr(heartbeat, "_check_single_node", unexpected_probe)
    await heartbeat._heartbeat_round()
    assert state["open_sessions"] == 0
    assert state["write_commits"] == 0


async def test_unexpected_probe_error_is_logged_and_other_results_are_saved(
    heartbeat_db, monkeypatch, caplog
):
    factory, _ = heartbeat_db
    await seed_nodes(factory, 2)
    real_probe = heartbeat._check_single_node

    async def probe(node, **kwargs):
        if node.host.endswith(".2"):
            raise RuntimeError("unexpected probe failure")
        return await real_probe(node, **kwargs)

    fake_metrics(monkeypatch)
    monkeypatch.setattr(heartbeat, "_check_single_node", probe)
    await heartbeat._heartbeat_round()
    assert "Heartbeat probe failed" in caplog.text
    async with factory() as db:
        nodes = (await db.execute(select(Node).order_by(Node.id))).scalars().all()
        assert nodes[0].health_status == 1
        assert nodes[1].health_status == 0 and nodes[1].last_heartbeat is None


async def test_cancelled_probe_aborts_all_results(heartbeat_db, monkeypatch):
    factory, state = heartbeat_db
    await seed_nodes(factory, 2)
    state["write_commits"] = 0
    real_probe = heartbeat._check_single_node

    async def probe(node, **kwargs):
        if node.host.endswith(".2"):
            raise asyncio.CancelledError
        return await real_probe(node, **kwargs)

    fake_metrics(monkeypatch)
    monkeypatch.setattr(heartbeat, "_check_single_node", probe)
    with pytest.raises(asyncio.CancelledError):
        await heartbeat._heartbeat_round()
    assert state["write_commits"] == 0
    async with factory() as db:
        nodes = (await db.execute(select(Node))).scalars().all()
        assert all(node.last_heartbeat is None for node in nodes)


async def test_cancellation_during_writes_rolls_back(heartbeat_db, monkeypatch):
    factory, state = heartbeat_db
    await seed_nodes(factory, 2)
    state["write_commits"] = 0
    real_execute = factory.class_.execute
    write_count = 0

    async def execute(db, statement, *args, **kwargs):
        nonlocal write_count
        if statement.is_update:
            write_count += 1
            if write_count == 2:
                raise asyncio.CancelledError
        return await real_execute(db, statement, *args, **kwargs)

    fake_metrics(monkeypatch)
    monkeypatch.setattr(factory.class_, "execute", execute)
    with pytest.raises(asyncio.CancelledError):
        await heartbeat._heartbeat_round()
    assert write_count == 2
    assert state["write_commits"] == 0
    async with factory() as db:
        nodes = (await db.execute(select(Node))).scalars().all()
        assert all(node.health_status == 0 and node.last_heartbeat is None for node in nodes)


async def test_loop_retries_after_failed_round(monkeypatch, caplog):
    attempts = sleeps = 0

    async def sleep(seconds):
        nonlocal sleeps
        assert seconds == 60
        sleeps += 1
        if sleeps == 3:
            raise asyncio.CancelledError

    async def round_check():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated heartbeat database failure")

    monkeypatch.setattr(heartbeat.asyncio, "sleep", sleep)
    monkeypatch.setattr(heartbeat, "_heartbeat_round", round_check)
    await heartbeat._heartbeat_loop()
    assert attempts == 2
    assert "Heartbeat loop error, will retry" in caplog.text


def test_snapshots_are_immutable_and_do_not_reveal_credentials():
    from dataclasses import FrozenInstanceError

    node = heartbeat.NodeSnapshot(1, "node", "10.0.0.1", 22, "tester", "encrypted-secret", "revision", None)
    assert "encrypted-secret" not in repr(node)
    with pytest.raises(FrozenInstanceError):
        node.host = "10.0.0.2"
