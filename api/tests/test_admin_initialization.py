"""Initial admin is seeded by database setup, never recreated on startup."""

import logging
import re
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.core.security import hash_password, verify_password
from app.models.user import User
from app.services.user import ensure_admin_user


@pytest.fixture(autouse=True)
def capture_service_logs(caplog, monkeypatch):
    # Application loggers do not propagate to the root pytest capture handler.
    logger = logging.getLogger("app.services.user")
    monkeypatch.setattr(logger, "handlers", [*logger.handlers, caplog.handler])


@pytest.mark.asyncio
async def test_database_seed_creates_admin_without_logging_credentials(db, client, caplog):
    sql = (Path(__file__).resolve().parents[2] / "docker" / "init.sql").read_text()
    seed = re.search(r"INSERT INTO\s+`?mysterious_user`?\s*\([^;]+;", sql, re.IGNORECASE)
    assert seed is not None, "Database initialization must seed the admin account"
    assert "Emm@2025" not in seed.group(), "SQL must store a bcrypt hash, not plaintext"
    await db.execute(text(seed.group()))
    await db.execute(text(seed.group()))
    await db.commit()

    admin = (await db.execute(select(User).where(User.username == "admin"))).scalar_one()
    assert admin.password.startswith("$2")
    assert verify_password("Emm@2025", admin.password)
    with caplog.at_level(logging.INFO, logger="app.services.user"):
        await ensure_admin_user(db)
    assert admin.role_id > 0
    assert "Emm@2025" not in caplog.text
    assert admin.password not in caplog.text
    if admin.token:
        assert admin.token not in caplog.text

    response = await client.post("/user/login", json={"username": "admin", "password": "Emm@2025"})
    assert response.json()["code"] == 0
    current = await client.get("/user/current", headers={"token": response.json()["data"]})
    assert current.json()["data"]["roleCode"] == "admin"


@pytest.mark.asyncio
async def test_startup_does_not_create_admin_or_log_default_password(db, caplog):
    with caplog.at_level(logging.INFO, logger="app.services.user"):
        await ensure_admin_user(db)
    assert (await db.execute(select(User).where(User.username == "admin"))).scalar_one_or_none() is None
    assert "Emm@2025" not in caplog.text


@pytest.mark.asyncio
async def test_startup_preserves_existing_admin_password(db):
    original_hash = hash_password("ChangedPassword!2026")
    admin = User(username="admin", password=original_hash, role_id=0)
    db.add(admin)
    await db.commit()
    sql = (Path(__file__).resolve().parents[2] / "docker" / "init.sql").read_text()
    seed = re.search(r"INSERT INTO\s+`?mysterious_user`?\s*\([^;]+;", sql, re.IGNORECASE)
    assert seed is not None
    await db.execute(text(seed.group()))
    await db.commit()
    await ensure_admin_user(db)
    await ensure_admin_user(db)
    assert admin.password == original_hash
    assert admin.role_id > 0
    assert len((await db.execute(select(User).where(User.username == "admin"))).scalars().all()) == 1
