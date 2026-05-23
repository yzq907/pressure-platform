"""Small compatibility migrations for deployments without Alembic."""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text

from app.db.session import async_engine

log = logging.getLogger(__name__)


_REPORT_COLUMNS = {
    "region": {
        "mysql": "varchar(255) NOT NULL DEFAULT '' COMMENT '执行区域快照'",
        "default": "VARCHAR(255) NOT NULL DEFAULT ''",
    },
    "service_name": {
        "mysql": "varchar(128) NOT NULL DEFAULT '' COMMENT '执行时服务名快照'",
        "default": "VARCHAR(128) NOT NULL DEFAULT ''",
    },
    "total_threads": {
        "mysql": "int NOT NULL DEFAULT 0 COMMENT '执行时总线程数快照'",
        "default": "INTEGER NOT NULL DEFAULT 0",
    },
    "slave_count": {
        "mysql": "int NOT NULL DEFAULT 0 COMMENT '执行时压力机数快照'",
        "default": "INTEGER NOT NULL DEFAULT 0",
    },
    "grafana_instance": {
        "mysql": "varchar(255) NOT NULL DEFAULT '' COMMENT '执行时Grafana instance快照'",
        "default": "VARCHAR(255) NOT NULL DEFAULT ''",
    },
    "artifact_dir": {
        "mysql": "varchar(255) NOT NULL DEFAULT '' COMMENT '执行产物目录快照'",
        "default": "VARCHAR(255) NOT NULL DEFAULT ''",
    },
}


async def ensure_report_snapshot_columns() -> None:
    """Add report snapshot columns for existing databases.

    The project currently uses docker/init.sql instead of Alembic. This keeps
    upgraded deployments from failing when the ORM starts selecting new columns.
    """
    async with async_engine.begin() as conn:
        dialect = conn.dialect.name
        columns = await conn.run_sync(
            lambda sync_conn: {
                col["name"] for col in inspect(sync_conn).get_columns("mysterious_report")
            }
        )
        for name, definitions in _REPORT_COLUMNS.items():
            if name in columns:
                continue
            ddl = definitions["mysql"] if dialect == "mysql" else definitions["default"]
            await conn.execute(text(f"ALTER TABLE mysterious_report ADD COLUMN {name} {ddl}"))
            log.info("已补齐 mysterious_report.%s 字段", name)
