"""审计日志批量删除：真实隔离数据库、权限与原子审计。"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, insert, select
from sqlalchemy.exc import SQLAlchemyError

from app.models.audit_log import AuditLog
from app.models.role import Role, RolePermission
from app.models.user import User


async def add_logs(db, count=3):
    rows = [AuditLog(username="original", action="CREATE", resource_type="testcase") for _ in range(count)]
    db.add_all(rows)
    await db.commit()
    return [row.id for row in rows]


async def set_permissions(db, permissions):
    role = Role(name="审计操作员", code="audit_operator", builtin=0)
    db.add(role)
    await db.flush()
    for permission in permissions:
        db.add(RolePermission(role_id=role.id, permission_code=permission))
    user = (await db.scalars(select(User).where(User.username == "test_admin"))).one()
    user.role_id = role.id
    await db.commit()


@pytest.mark.asyncio
async def test_batch_deletes_only_selected_and_records_summary(auth_client, db):
    first, second, keep = await add_logs(db)
    result = (await auth_client.post("/audit/cleanBatch", json={"ids": [second, first]})).json()
    assert result["code"] == 0
    assert result["data"] == {
        "deletedIds": [second, first], "failures": [], "successCount": 2, "failedCount": 0,
    }
    db.expire_all()
    rows = list((await db.scalars(select(AuditLog))).all())
    assert len(rows) == 2
    assert any(row.id == keep for row in rows)
    audit = next(row for row in rows if row.resource_type == "audit")
    assert audit.action == "DELETE"
    assert audit.username == "test_admin"
    assert audit.user_id > 0
    assert audit.ip == "127.0.0.1"
    assert str(first) in audit.detail and str(second) in audit.detail
    assert "2" in audit.detail


@pytest.mark.asyncio
async def test_non_admin_with_audit_permission_can_delete(auth_client, db):
    ids = await add_logs(db, 1)
    await set_permissions(db, ["audit"])
    result = (await auth_client.post("/audit/cleanBatch", json={"ids": ids})).json()
    assert result["code"] == 0
    assert result["data"]["deletedIds"] == ids


@pytest.mark.asyncio
async def test_no_audit_permission_cannot_delete(auth_client, db):
    ids = await add_logs(db, 1)
    await set_permissions(db, ["report"])
    result = (await auth_client.post("/audit/cleanBatch", json={"ids": ids})).json()
    assert result["code"] != 0
    assert "无权" in result["message"]
    db.expire_all()
    assert await db.get(AuditLog, ids[0]) is not None


@pytest.mark.asyncio
async def test_unauthenticated_cannot_delete(client, db):
    ids = await add_logs(db, 1)
    result = (await client.post("/audit/cleanBatch", json={"ids": ids})).json()
    assert result["code"] == 1007
    db.expire_all()
    assert await db.get(AuditLog, ids[0]) is not None


@pytest.mark.asyncio
async def test_deduplicates_ids_and_reports_missing(auth_client, db):
    first, second, keep = await add_logs(db)
    result = (await auth_client.post("/audit/cleanBatch", json={"ids": [second, 99999, first, second]})).json()
    data = result["data"]
    assert result["code"] == 0
    assert data["deletedIds"] == [second, first]
    assert data["successCount"] == 2
    assert data["failedCount"] == 1
    assert data["failures"][0]["id"] == 99999
    assert data["failures"][0]["message"]
    db.expire_all()
    assert await db.get(AuditLog, keep) is not None


@pytest.mark.asyncio
async def test_all_missing_does_not_create_deletion_audit(auth_client, db):
    result = (await auth_client.post("/audit/cleanBatch", json={"ids": [88888, 99999]})).json()
    assert result["code"] == 0
    assert result["data"]["deletedIds"] == []
    assert result["data"]["failedCount"] == 2
    assert list((await db.scalars(select(AuditLog))).all()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {}, {"ids": []}, {"ids": [0]}, {"ids": [-1]}, {"ids": [True]}, {"ids": ["1"]},
    {"ids": [1.1]}, {"ids": None}, {"ids": list(range(1, 102))},
])
async def test_invalid_ids_do_not_delete(auth_client, db, payload):
    ids = await add_logs(db, 1)
    result = (await auth_client.post("/audit/cleanBatch", json=payload)).json()
    assert result["code"] != 0
    assert "参数" in result["message"]
    db.expire_all()
    assert await db.get(AuditLog, ids[0]) is not None


@pytest.mark.asyncio
async def test_audit_write_failure_rolls_back_deletions(db):
    from app.core.context import UserContext
    from app.core.exceptions import MysteriousException
    from app.services import audit_log as service

    db.add(Role(name="existing", code="audit_failure"))
    ids = await add_logs(db, 2)

    def fail_audit_insert(mapper, connection, target):
        if target.resource_type == "audit":
            # Real UNIQUE violation while inserting the audit poisons the transaction.
            connection.execute(insert(Role).values(name="private SQL or credentials", code="audit_failure"))

    event.listen(AuditLog, "before_insert", fail_audit_insert)
    try:
        with pytest.raises(MysteriousException) as error:
            await service.clean_logs(db, ids, UserContext(id=1, username="operator"), "127.0.0.1")
    finally:
        event.remove(AuditLog, "before_insert", fail_audit_insert)
    assert "private" not in str(error.value)
    db.expire_all()
    assert sorted((await db.scalars(select(AuditLog.id))).all()) == sorted(ids)


@pytest.mark.asyncio
async def test_commit_failure_rolls_back_deletions_and_summary(db, monkeypatch):
    from app.core.context import UserContext
    from app.core.exceptions import MysteriousException
    from app.services import audit_log as service

    ids = await add_logs(db, 2)
    monkeypatch.setattr(db, "commit", AsyncMock(side_effect=SQLAlchemyError("private error")))
    with pytest.raises(MysteriousException):
        await service.clean_logs(db, ids, UserContext(id=1, username="operator"), "127.0.0.1")
    db.expire_all()
    assert sorted((await db.scalars(select(AuditLog.id))).all()) == sorted(ids)


@pytest.mark.asyncio
async def test_list_can_filter_new_audit_resource(auth_client, db):
    ids = await add_logs(db, 1)
    await auth_client.post("/audit/cleanBatch", json={"ids": ids})
    result = (await auth_client.get("/audit/list", params={"resourceType": "audit", "action": "DELETE"})).json()
    assert result["code"] == 0
    assert result["data"]["total"] == 1
    assert result["data"]["list"][0]["resourceType"] == "audit"
