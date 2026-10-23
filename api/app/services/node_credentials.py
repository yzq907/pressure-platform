"""Transactional one-time migration of legacy node credentials."""

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ssh_credentials import (
    RESERVED_PREFIX,
    decrypt_password,
    encrypt_password,
    load_master_key,
)
from app.models.node import Node


async def migrate_node_passwords(db: AsyncSession) -> int:
    """Own the transaction; validate everything before modifying any password."""
    key = load_master_key()
    async with db.begin():
        # Only depend on the two legacy columns, not the latest complete ORM schema.
        nodes = (await db.execute(select(Node.id, Node.password).order_by(Node.id).with_for_update())).all()
        legacy = []
        for node in nodes:
            if node.password.startswith(RESERVED_PREFIX):
                decrypt_password(node.password, master_key=key)
            elif node.password:
                legacy.append({"node_id": node.id, "encrypted_password": encrypt_password(node.password, master_key=key)})
        if legacy:
            await db.execute(
                text("UPDATE mysterious_node SET password = :encrypted_password WHERE id = :node_id"), legacy
            )
    return len(legacy)
