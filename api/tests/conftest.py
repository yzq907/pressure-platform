"""pytest fixtures.

策略：
- 用 SQLite 内存库 (aiosqlite + StaticPool) 替换真实 MySQL，让 ORM 测试可以端到端跑
- 用一个简单的 fake 替换 Redis（健康检查会读 redis.ping）
- 测试开始前 create_all 建表，结束后 drop_all 清理
- `auth_client` 直接绕开 bcrypt 登录流程，插入一行 user + token，速度比 /user/login 快几个数量级
- `data_home` 给 testcase 测试用：pytest tmp_path + 预插入 config 表
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# 触发 ORM 模型注册到 Base.metadata
import app.models  # noqa: F401
from app.db.base import Base
from app.db.redis import get_redis
from app.db.session import get_db
from app.main import app
from app.models.config import Config
from app.models.user import User

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


# StaticPool + check_same_thread=False 是 SQLite 内存库共享连接的标准做法
test_engine = create_async_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)


async def _override_get_db() -> AsyncIterator[AsyncSession]:
    async with TestSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


class _FakeRedis:
    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


async def _override_get_redis() -> AsyncIterator[_FakeRedis]:
    yield _FakeRedis()


@pytest.fixture(scope="session", autouse=True)
async def _setup_db() -> AsyncIterator[None]:
    """整个测试会话开始时建表，结束后清表"""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await test_engine.dispose()


@pytest.fixture(autouse=True)
def _override_deps() -> Any:
    """每个测试用例都自动 mock get_db / get_redis 依赖，
    并把 app.db.session.AsyncSessionLocal 替换成 TestSessionLocal，
    让 jmeter_runner 之类直接用 session 工厂的代码也走测试库。"""
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_redis] = _override_get_redis

    from app.db import session as session_module

    original_session_local = session_module.AsyncSessionLocal
    session_module.AsyncSessionLocal = TestSessionLocal  # type: ignore[attr-defined]
    yield
    session_module.AsyncSessionLocal = original_session_local  # type: ignore[attr-defined]
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
async def _clean_tables() -> AsyncIterator[None]:
    """每个测试用例之间清空所有表，保证用例之间互不影响"""
    yield
    async with test_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """直接拿到 session，用于测试里手动构造测试数据 / 断言数据库状态"""
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def auth_client() -> AsyncIterator[AsyncClient]:
    """提供已登录态的测试客户端。

    直接在 DB 插入一行 user + token，跳过 /user/add 的 bcrypt 哈希（~250ms）。
    所有请求自动带 `token` header。
    """
    token = str(uuid.uuid4())
    # 用 Shanghai 本地时间，对齐 deps/auth.py 里的时间比较
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    async with TestSessionLocal() as session:
        user = User(
            username="test_admin",
            password="bypass-bcrypt-in-fixture",
            real_name="测试管理员",
            token=token,
            effect_time=now,
            expire_time=now + timedelta(hours=12),
        )
        session.add(user)
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"token": token},
    ) as c:
        yield c


@pytest.fixture
async def data_home(tmp_path: Path) -> AsyncIterator[Path]:
    """提供一个 tmp 目录作为 MASTER_DATA_HOME，并把它写入 config 表"""
    async with TestSessionLocal() as session:
        cfg = Config(
            config_key="MASTER_DATA_HOME",
            config_value=str(tmp_path),
            description="测试用 master 数据目录",
        )
        session.add(cfg)
        await session.commit()
    yield tmp_path


@pytest.fixture
async def jmeter_home(tmp_path: Path) -> AsyncIterator[Path]:
    """提供一个 tmp 目录作为 MASTER_JMETER_HOME，含 lib/ext 子目录用于 plugin JAR 测试"""
    jh = tmp_path / "jmeter"
    (jh / "lib" / "ext").mkdir(parents=True, exist_ok=True)
    async with TestSessionLocal() as session:
        cfg = Config(
            config_key="MASTER_JMETER_HOME",
            config_value=str(jh),
            description="测试用 master jmeter 目录",
        )
        session.add(cfg)
        await session.commit()
    yield jh


SAMPLE_JMX_PATH = Path(__file__).parent / "fixtures" / "sample.jmx"


@pytest.fixture
def sample_jmx_bytes() -> bytes:
    """读取 fixtures/sample.jmx 的字节内容，方便上传测试用"""
    return SAMPLE_JMX_PATH.read_bytes()


@pytest.fixture
async def slave_paths() -> AsyncIterator[None]:
    """给 Node enable/disable 测试用：插入 SLAVE_JMETER_BIN_HOME / SLAVE_JMETER_LOG_HOME 配置"""
    async with TestSessionLocal() as s:
        s.add(Config(config_key="SLAVE_JMETER_BIN_HOME", config_value="/opt/jmeter/bin"))
        s.add(Config(config_key="SLAVE_JMETER_LOG_HOME", config_value="/opt/jmeter/log"))
        await s.commit()
    yield


@pytest.fixture
async def jmeter_bin_home(tmp_path: Path) -> AsyncIterator[Path]:
    """指向 tests/fakes/fake_jmeter.sh，并写入 MASTER_JMETER_BIN_HOME config。"""
    import shutil

    bin_dir = tmp_path / "jmeter-bin"
    bin_dir.mkdir()
    fake_src = Path(__file__).parent / "fakes" / "fake_jmeter.sh"
    shutil.copy(fake_src, bin_dir / "jmeter")
    (bin_dir / "jmeter").chmod(0o755)
    # shutdown.sh：测试里只跑 exit 0
    (bin_dir / "shutdown.sh").write_text("#!/bin/bash\nexit 0\n")
    (bin_dir / "shutdown.sh").chmod(0o755)

    async with TestSessionLocal() as s:
        s.add(Config(config_key="MASTER_JMETER_BIN_HOME", config_value=str(bin_dir)))
        await s.commit()
    yield bin_dir


@pytest.fixture(autouse=True)
def mock_ssh(request, monkeypatch) -> Any:
    """全局 mock SSHClient。让所有走 SSH 的服务在测试里都不真的连远端。

    覆盖默认行为：所有方法返回成功。
    需要测真实 SSHClient 的用例（test_ssh.py）通过 `pytestmark = pytest.mark.real_ssh` 跳过此 mock。
    特定测试要模拟失败时，直接再次 monkeypatch 即可（后写覆盖先写）。
    """
    if request.node.get_closest_marker("real_ssh"):
        return None

    from app.core import ssh as ssh_mod

    # 状态机：跟踪 jmeter-server 是否已经启动；让默认 mock 能跑通 Java 的 enable 流程
    # （enable 流程：md5 通过 → ps 不存在 → 启动 → ps 存在）
    state = {"jmeter_started": False}

    async def fake_telnet(self, timeout_ms: int = 200) -> bool:
        return True

    async def fake_exec(self, command: str) -> str:
        # 启动 jmeter-server
        if "jmeter-server" in command and "rmi.server.hostname" in command:
            state["jmeter_started"] = True
            return "Using local port: 1099"
        # ps 检查
        if "ps aux" in command and "grep jmeter-server" in command:
            if "kill" in command:
                return ""
            return "root  12345 ... jmeter-server" if state["jmeter_started"] else "null"
        # md5sum
        if "md5sum" in command:
            return "0" * 32
        # rm/mkdir 等
        return ""

    async def fake_scp(self, local_path: str, remote_dir: str) -> None:
        return None

    monkeypatch.setattr(ssh_mod.SSHClient, "telnet", fake_telnet)
    monkeypatch.setattr(ssh_mod.SSHClient, "exec_command", fake_exec)
    monkeypatch.setattr(ssh_mod.SSHClient, "scp_file", fake_scp)
    return None
