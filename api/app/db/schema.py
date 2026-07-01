"""Small compatibility migrations for deployments without Alembic."""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text

from app.db.session import async_engine

log = logging.getLogger(__name__)


async def ensure_config_value_text_column() -> None:
    """Allow JSON-style configuration values longer than 255 chars."""
    async with async_engine.begin() as conn:
        dialect = conn.dialect.name
        if dialect != "mysql":
            return
        columns = await conn.run_sync(
            lambda sync_conn: {
                col["name"]: col for col in inspect(sync_conn).get_columns("mysterious_config")
            }
        )
        column = columns.get("config_value")
        if column is None:
            return
        if "text" in str(column.get("type", "")).lower():
            return
        await conn.execute(text("ALTER TABLE mysterious_config MODIFY COLUMN config_value text NOT NULL"))
        log.info("已升级 mysterious_config.config_value 为 text")


async def ensure_ai_generation_tables() -> None:
    """Create AI generation task/artifact tables for upgraded deployments."""
    async with async_engine.begin() as conn:
        dialect = conn.dialect.name
        tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
        if "mysterious_ai_generation_task" in tables and "mysterious_ai_generation_artifact" in tables:
            return

        id_type = "bigint(20) NOT NULL AUTO_INCREMENT" if dialect == "mysql" else "INTEGER NOT NULL"
        dt_default = "datetime NOT NULL DEFAULT CURRENT_TIMESTAMP" if dialect == "mysql" else "DATETIME NOT NULL"

        if "mysterious_ai_generation_task" not in tables:
            task_ddl = f"""
            CREATE TABLE mysterious_ai_generation_task (
                id {id_type},
                task_name varchar(255) NOT NULL DEFAULT '',
                generation_type varchar(64) NOT NULL DEFAULT '',
                input_type varchar(32) NOT NULL DEFAULT '',
                input_filename varchar(255) NOT NULL DEFAULT '',
                input_path varchar(512) NOT NULL DEFAULT '',
                output_filename varchar(255) NOT NULL DEFAULT '',
                output_path varchar(512) NOT NULL DEFAULT '',
                work_dir varchar(512) NOT NULL DEFAULT '',
                status varchar(32) NOT NULL DEFAULT 'pending',
                params_json text NOT NULL,
                error_message text NOT NULL,
                generation_log text NOT NULL,
                creator_id varchar(32) NOT NULL DEFAULT '',
                creator varchar(32) NOT NULL DEFAULT '',
                modifier_id varchar(32) NOT NULL DEFAULT '',
                modifier varchar(32) NOT NULL DEFAULT '',
                create_time {dt_default},
                modify_time {dt_default},
                PRIMARY KEY (id)
            )
            """
            await conn.execute(text(task_ddl))
            if dialect == "mysql":
                await conn.execute(text("CREATE INDEX idx_ai_generation_task_status ON mysterious_ai_generation_task (status)"))
                await conn.execute(text("CREATE INDEX idx_ai_generation_task_type ON mysterious_ai_generation_task (generation_type)"))
            log.info("已创建 mysterious_ai_generation_task 表")

        if "mysterious_ai_generation_artifact" not in tables:
            artifact_ddl = f"""
            CREATE TABLE mysterious_ai_generation_artifact (
                id {id_type},
                task_id bigint NOT NULL DEFAULT 0,
                artifact_type varchar(32) NOT NULL DEFAULT '',
                filename varchar(255) NOT NULL DEFAULT '',
                file_path varchar(512) NOT NULL DEFAULT '',
                file_size int NOT NULL DEFAULT 0,
                creator_id varchar(32) NOT NULL DEFAULT '',
                creator varchar(32) NOT NULL DEFAULT '',
                modifier_id varchar(32) NOT NULL DEFAULT '',
                modifier varchar(32) NOT NULL DEFAULT '',
                create_time {dt_default},
                modify_time {dt_default},
                PRIMARY KEY (id)
            )
            """
            await conn.execute(text(artifact_ddl))
            if dialect == "mysql":
                await conn.execute(text("CREATE INDEX idx_ai_generation_artifact_task_id ON mysterious_ai_generation_artifact (task_id)"))
            log.info("已创建 mysterious_ai_generation_artifact 表")


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


async def ensure_user_session_table() -> None:
    """Create user session token table for multi-client login."""
    async with async_engine.begin() as conn:
        dialect = conn.dialect.name
        tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
        if "mysterious_user_session" in tables:
            return

        id_type = "bigint(20) NOT NULL AUTO_INCREMENT" if dialect == "mysql" else "INTEGER NOT NULL"
        dt_default = "datetime NOT NULL DEFAULT CURRENT_TIMESTAMP" if dialect == "mysql" else "DATETIME NOT NULL"
        ddl = f"""
        CREATE TABLE mysterious_user_session (
            id {id_type},
            user_id bigint NOT NULL DEFAULT 0,
            token varchar(128) NOT NULL DEFAULT '',
            effect_time {dt_default},
            expire_time {dt_default},
            PRIMARY KEY (id)
        )
        """
        await conn.execute(text(ddl))
        if dialect == "mysql":
            await conn.execute(text("CREATE UNIQUE INDEX uk_mysterious_user_session_token ON mysterious_user_session (token)"))
            await conn.execute(text("CREATE INDEX idx_mysterious_user_session_user_id ON mysterious_user_session (user_id)"))
        log.info("已创建 mysterious_user_session 表")

_USER_ROLE_COLUMNS = {
    "role_id": {
        "mysql": "bigint NOT NULL DEFAULT 0 COMMENT '用户角色ID'",
        "default": "INTEGER NOT NULL DEFAULT 0",
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


async def ensure_csv_resource_tables() -> None:
    """Create public CSV resource and testcase binding tables for upgraded deployments."""
    async with async_engine.begin() as conn:
        dialect = conn.dialect.name
        tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
        id_type = "bigint(20) NOT NULL AUTO_INCREMENT" if dialect == "mysql" else "INTEGER NOT NULL"
        bigint_type = "bigint NOT NULL DEFAULT 0" if dialect == "mysql" else "INTEGER NOT NULL DEFAULT 0"
        dt_default = "datetime NOT NULL DEFAULT CURRENT_TIMESTAMP" if dialect == "mysql" else "DATETIME NOT NULL"

        if "mysterious_csv_resource" not in tables:
            await conn.execute(text(f"""
            CREATE TABLE mysterious_csv_resource (
                id {id_type},
                filename varchar(255) NOT NULL DEFAULT '',
                file_dir varchar(255) NOT NULL DEFAULT '',
                file_type varchar(32) NOT NULL DEFAULT '',
                description varchar(255) NOT NULL DEFAULT '',
                file_size {bigint_type},
                checksum varchar(64) NOT NULL DEFAULT '',
                creator_id varchar(32) NOT NULL DEFAULT '',
                creator varchar(32) NOT NULL DEFAULT '',
                modifier_id varchar(32) NOT NULL DEFAULT '',
                modifier varchar(32) NOT NULL DEFAULT '',
                create_time {dt_default},
                modify_time {dt_default},
                PRIMARY KEY (id)
            )
            """))
            if dialect == "mysql":
                await conn.execute(text("CREATE UNIQUE INDEX uk_mysterious_csv_resource_filename ON mysterious_csv_resource (filename)"))
            log.info("已创建 mysterious_csv_resource 表")
        else:
            resource_columns = await conn.run_sync(
                lambda sync_conn: {
                    col["name"] for col in inspect(sync_conn).get_columns("mysterious_csv_resource")
                }
            )
            if "file_type" not in resource_columns:
                await conn.execute(text("ALTER TABLE mysterious_csv_resource ADD COLUMN file_type varchar(32) NOT NULL DEFAULT ''"))
                log.info("已补齐 mysterious_csv_resource.file_type 字段")

        if "mysterious_testcase_csv_binding" not in tables:
            await conn.execute(text(f"""
            CREATE TABLE mysterious_testcase_csv_binding (
                id {id_type},
                test_case_id {bigint_type},
                filename varchar(255) NOT NULL DEFAULT '',
                description varchar(255) NOT NULL DEFAULT '',
                distribution_strategy varchar(32) NOT NULL DEFAULT 'shared',
                creator_id varchar(32) NOT NULL DEFAULT '',
                creator varchar(32) NOT NULL DEFAULT '',
                modifier_id varchar(32) NOT NULL DEFAULT '',
                modifier varchar(32) NOT NULL DEFAULT '',
                create_time {dt_default},
                modify_time {dt_default},
                PRIMARY KEY (id)
            )
            """))
            if dialect == "mysql":
                await conn.execute(text(
                    "CREATE UNIQUE INDEX uk_testcase_csv_binding_case_filename "
                    "ON mysterious_testcase_csv_binding (test_case_id, filename)"
                ))
                await conn.execute(text(
                    "CREATE INDEX idx_testcase_csv_binding_filename "
                    "ON mysterious_testcase_csv_binding (filename)"
                ))
            log.info("已创建 mysterious_testcase_csv_binding 表")

        if "mysterious_testcase_upload_file_binding" not in tables:
            await conn.execute(text(f"""
            CREATE TABLE mysterious_testcase_upload_file_binding (
                id {id_type},
                test_case_id {bigint_type},
                filename varchar(255) NOT NULL DEFAULT '',
                description varchar(255) NOT NULL DEFAULT '',
                creator_id varchar(32) NOT NULL DEFAULT '',
                creator varchar(32) NOT NULL DEFAULT '',
                modifier_id varchar(32) NOT NULL DEFAULT '',
                modifier varchar(32) NOT NULL DEFAULT '',
                create_time {dt_default},
                modify_time {dt_default},
                PRIMARY KEY (id)
            )
            """))
            if dialect == "mysql":
                await conn.execute(text(
                    "CREATE UNIQUE INDEX uk_testcase_upload_file_binding_case_filename "
                    "ON mysterious_testcase_upload_file_binding (test_case_id, filename)"
                ))
                await conn.execute(text(
                    "CREATE INDEX idx_testcase_upload_file_binding_filename "
                    "ON mysterious_testcase_upload_file_binding (filename)"
                ))
            log.info("已创建 mysterious_testcase_upload_file_binding 表")


async def ensure_rbac_schema() -> None:
    """Create RBAC role tables and add user.role_id for upgraded deployments."""
    async with async_engine.begin() as conn:
        dialect = conn.dialect.name
        tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
        id_type = "bigint(20) NOT NULL AUTO_INCREMENT" if dialect == "mysql" else "INTEGER NOT NULL"
        dt_default = "datetime NOT NULL DEFAULT CURRENT_TIMESTAMP" if dialect == "mysql" else "DATETIME NOT NULL"

        if "mysterious_role" not in tables:
            await conn.execute(text(f"""
            CREATE TABLE mysterious_role (
                id {id_type},
                name varchar(128) NOT NULL DEFAULT '',
                code varchar(64) NOT NULL DEFAULT '',
                description varchar(255) NOT NULL DEFAULT '',
                builtin int NOT NULL DEFAULT 0,
                creator_id varchar(32) NOT NULL DEFAULT '',
                creator varchar(32) NOT NULL DEFAULT '',
                modifier_id varchar(32) NOT NULL DEFAULT '',
                modifier varchar(32) NOT NULL DEFAULT '',
                create_time {dt_default},
                modify_time {dt_default},
                PRIMARY KEY (id)
            )
            """))
            if dialect == "mysql":
                await conn.execute(text("CREATE UNIQUE INDEX uk_mysterious_role_code ON mysterious_role (code)"))
            log.info("已创建 mysterious_role 表")

        if "mysterious_role_permission" not in tables:
            await conn.execute(text(f"""
            CREATE TABLE mysterious_role_permission (
                id {id_type},
                role_id bigint NOT NULL DEFAULT 0,
                permission_code varchar(64) NOT NULL DEFAULT '',
                PRIMARY KEY (id)
            )
            """))
            if dialect == "mysql":
                await conn.execute(text("CREATE INDEX idx_role_permission_role_id ON mysterious_role_permission (role_id)"))
            log.info("已创建 mysterious_role_permission 表")

        user_columns = await conn.run_sync(
            lambda sync_conn: {
                col["name"] for col in inspect(sync_conn).get_columns("mysterious_user")
            }
        )
        for name, definitions in _USER_ROLE_COLUMNS.items():
            if name in user_columns:
                continue
            ddl = definitions["mysql"] if dialect == "mysql" else definitions["default"]
            await conn.execute(text(f"ALTER TABLE mysterious_user ADD COLUMN {name} {ddl}"))
            log.info("已补齐 mysterious_user.%s 字段", name)


async def ensure_scheduled_task_log_table() -> None:
    """Create scheduled task execution log table for upgraded deployments."""
    async with async_engine.begin() as conn:
        dialect = conn.dialect.name
        tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
        if "mysterious_scheduled_task_log" in tables:
            return
        id_type = "bigint(20) NOT NULL AUTO_INCREMENT" if dialect == "mysql" else "INTEGER NOT NULL"
        dt_default = "datetime NOT NULL DEFAULT CURRENT_TIMESTAMP" if dialect == "mysql" else "DATETIME NOT NULL"
        ddl = f"""
        CREATE TABLE mysterious_scheduled_task_log (
            id {id_type},
            scheduled_task_id bigint NOT NULL DEFAULT 0,
            test_case_id bigint NOT NULL DEFAULT 0,
            trigger_type varchar(16) NOT NULL DEFAULT '',
            status varchar(16) NOT NULL DEFAULT '',
            reason text NOT NULL,
            message text NOT NULL,
            region varchar(255) NOT NULL DEFAULT '',
            requested_slave_count int NOT NULL DEFAULT 0,
            available_slave_count int NOT NULL DEFAULT 0,
            allocated_slave_count int NOT NULL DEFAULT 0,
            slave_hosts text NOT NULL,
            run_param text NOT NULL,
            trigger_time datetime NOT NULL,
            next_run_at datetime NULL,
            create_time {dt_default},
            PRIMARY KEY (id)
        )
        """
        await conn.execute(text(ddl))
        if dialect == "mysql":
            await conn.execute(text("CREATE INDEX idx_scheduled_task_log_task_id ON mysterious_scheduled_task_log (scheduled_task_id)"))
            await conn.execute(text("CREATE INDEX idx_scheduled_task_log_test_case_id ON mysterious_scheduled_task_log (test_case_id)"))
        log.info("已创建 mysterious_scheduled_task_log 表")


async def ensure_execution_queue_table() -> None:
    """Create manual execution queue table for upgraded deployments."""
    async with async_engine.begin() as conn:
        dialect = conn.dialect.name
        tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
        if "mysterious_execution_queue" in tables:
            return
        id_type = "bigint(20) NOT NULL AUTO_INCREMENT" if dialect == "mysql" else "INTEGER NOT NULL"
        dt_default = "datetime NOT NULL DEFAULT CURRENT_TIMESTAMP" if dialect == "mysql" else "DATETIME NOT NULL"
        ddl = f"""
        CREATE TABLE mysterious_execution_queue (
            id {id_type},
            test_case_id bigint NOT NULL DEFAULT 0,
            report_id bigint NOT NULL DEFAULT 0,
            status varchar(32) NOT NULL DEFAULT 'pending',
            queue_policy varchar(64) NOT NULL DEFAULT '',
            trigger_type varchar(32) NOT NULL DEFAULT 'manual',
            run_param text NOT NULL,
            region varchar(255) NOT NULL DEFAULT '',
            requested_slave_count int NOT NULL DEFAULT 0,
            available_slave_count int NOT NULL DEFAULT 0,
            allocated_slave_count int NOT NULL DEFAULT 0,
            slave_hosts text NOT NULL,
            message text NOT NULL,
            enqueue_time datetime NULL,
            start_time datetime NULL,
            finish_time datetime NULL,
            creator_id varchar(32) NOT NULL DEFAULT '',
            creator varchar(32) NOT NULL DEFAULT '',
            modifier_id varchar(32) NOT NULL DEFAULT '',
            modifier varchar(32) NOT NULL DEFAULT '',
            create_time {dt_default},
            modify_time {dt_default},
            PRIMARY KEY (id)
        )
        """
        await conn.execute(text(ddl))
        if dialect == "mysql":
            await conn.execute(text("CREATE INDEX idx_execution_queue_status ON mysterious_execution_queue (status)"))
            await conn.execute(text("CREATE INDEX idx_execution_queue_test_case_id ON mysterious_execution_queue (test_case_id)"))
            await conn.execute(text("CREATE INDEX idx_execution_queue_report_id ON mysterious_execution_queue (report_id)"))
        log.info("已创建 mysterious_execution_queue 表")


async def ensure_execution_run_table() -> None:
    """Create persistent execution run table for upgraded deployments."""
    async with async_engine.begin() as conn:
        dialect = conn.dialect.name
        tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
        id_type = "bigint(20) NOT NULL AUTO_INCREMENT" if dialect == "mysql" else "INTEGER NOT NULL"
        dt_default = "datetime NOT NULL DEFAULT CURRENT_TIMESTAMP" if dialect == "mysql" else "DATETIME NOT NULL"
        if "mysterious_execution_run" not in tables:
            ddl = f"""
            CREATE TABLE mysterious_execution_run (
                id {id_type},
                report_id bigint NOT NULL DEFAULT 0,
                test_case_id bigint NOT NULL DEFAULT 0,
                region varchar(255) NOT NULL DEFAULT '',
                status varchar(32) NOT NULL DEFAULT 'preparing',
                worker_id varchar(128) NOT NULL DEFAULT '',
                pid int NOT NULL DEFAULT 0,
                pgid int NOT NULL DEFAULT 0,
                cmd text NOT NULL,
                jtl_path varchar(512) NOT NULL DEFAULT '',
                log_path varchar(512) NOT NULL DEFAULT '',
                heartbeat_at datetime NULL,
                started_at datetime NULL,
                finished_at datetime NULL,
                stop_requested_at datetime NULL,
                exit_code int NOT NULL DEFAULT 0,
                message text NOT NULL,
                creator_id varchar(32) NOT NULL DEFAULT '',
                creator varchar(32) NOT NULL DEFAULT '',
                modifier_id varchar(32) NOT NULL DEFAULT '',
                modifier varchar(32) NOT NULL DEFAULT '',
                create_time {dt_default},
                modify_time {dt_default},
                PRIMARY KEY (id)
            )
            """
            await conn.execute(text(ddl))
            log.info("已创建 mysterious_execution_run 表")

        indexes = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_indexes("mysterious_execution_run"))
        index_names = {idx.get("name") for idx in indexes}
        if "uk_execution_run_report_id" not in index_names:
            await conn.execute(text("""
                DELETE FROM mysterious_execution_run
                WHERE id NOT IN (
                    SELECT keep_id FROM (
                        SELECT MIN(id) AS keep_id
                        FROM mysterious_execution_run
                        GROUP BY report_id
                    ) AS keep_rows
                )
            """))
            await conn.execute(text("CREATE UNIQUE INDEX uk_execution_run_report_id ON mysterious_execution_run (report_id)"))
        if "idx_execution_run_status" not in index_names:
            await conn.execute(text("CREATE INDEX idx_execution_run_status ON mysterious_execution_run (status)"))


async def ensure_execution_node_table() -> None:
    """Create execution node lease table for upgraded deployments."""
    async with async_engine.begin() as conn:
        dialect = conn.dialect.name
        tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
        id_type = "bigint(20) NOT NULL AUTO_INCREMENT" if dialect == "mysql" else "INTEGER NOT NULL"
        dt_default = "datetime NOT NULL DEFAULT CURRENT_TIMESTAMP" if dialect == "mysql" else "DATETIME NOT NULL"
        if "mysterious_execution_node" not in tables:
            ddl = f"""
            CREATE TABLE mysterious_execution_node (
                id {id_type},
                report_id bigint NOT NULL DEFAULT 0,
                test_case_id bigint NOT NULL DEFAULT 0,
                execution_run_id bigint NOT NULL DEFAULT 0,
                node_id bigint NOT NULL DEFAULT 0,
                node_host varchar(128) NOT NULL DEFAULT '',
                region varchar(255) NOT NULL DEFAULT '',
                status varchar(32) NOT NULL DEFAULT 'leased',
                leased_at datetime NULL,
                released_at datetime NULL,
                release_message text NOT NULL,
                creator_id varchar(32) NOT NULL DEFAULT '',
                creator varchar(32) NOT NULL DEFAULT '',
                modifier_id varchar(32) NOT NULL DEFAULT '',
                modifier varchar(32) NOT NULL DEFAULT '',
                create_time {dt_default},
                modify_time {dt_default},
                PRIMARY KEY (id)
            )
            """
            await conn.execute(text(ddl))
            log.info("已创建 mysterious_execution_node 表")

        indexes = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_indexes("mysterious_execution_node"))
        index_names = {idx.get("name") for idx in indexes}
        if "idx_execution_node_report_id" not in index_names:
            await conn.execute(text("CREATE INDEX idx_execution_node_report_id ON mysterious_execution_node (report_id)"))
        if "idx_execution_node_node_id_status" not in index_names:
            await conn.execute(text("CREATE INDEX idx_execution_node_node_id_status ON mysterious_execution_node (node_id, status)"))
        if "idx_execution_node_status" not in index_names:
            await conn.execute(text("CREATE INDEX idx_execution_node_status ON mysterious_execution_node (status)"))


async def ensure_report_metric_snapshot_table() -> None:
    """Create report metric snapshot table for upgraded deployments."""
    async with async_engine.begin() as conn:
        dialect = conn.dialect.name
        tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
        id_type = "bigint(20) NOT NULL AUTO_INCREMENT" if dialect == "mysql" else "INTEGER NOT NULL"
        dt_default = "datetime NOT NULL DEFAULT CURRENT_TIMESTAMP" if dialect == "mysql" else "DATETIME NOT NULL"
        if "mysterious_report_metric_snapshot" not in tables:
            ddl = f"""
            CREATE TABLE mysterious_report_metric_snapshot (
                id {id_type},
                report_id bigint NOT NULL DEFAULT 0,
                window_sec int NOT NULL DEFAULT 5,
                bucket_start_ms bigint NOT NULL DEFAULT 0,
                timestamp varchar(32) NOT NULL DEFAULT '',
                qps double NOT NULL DEFAULT 0,
                avg_rt double NOT NULL DEFAULT 0,
                p95_rt double NOT NULL DEFAULT 0,
                p99_rt double NOT NULL DEFAULT 0,
                error_rate double NOT NULL DEFAULT 0,
                threads int NOT NULL DEFAULT 0,
                sample_count int NOT NULL DEFAULT 0,
                fail_count int NOT NULL DEFAULT 0,
                tps_peak double NOT NULL DEFAULT 0,
                creator_id varchar(32) NOT NULL DEFAULT '',
                creator varchar(32) NOT NULL DEFAULT '',
                modifier_id varchar(32) NOT NULL DEFAULT '',
                modifier varchar(32) NOT NULL DEFAULT '',
                create_time {dt_default},
                modify_time {dt_default},
                PRIMARY KEY (id)
            )
            """
            await conn.execute(text(ddl))
            log.info("已创建 mysterious_report_metric_snapshot 表")

        indexes = await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).get_indexes("mysterious_report_metric_snapshot")
        )
        if any(idx.get("name") == "uk_report_metric_report_window_bucket" for idx in indexes):
            return

        await conn.execute(text("""
            DELETE FROM mysterious_report_metric_snapshot
            WHERE id NOT IN (
                SELECT keep_id FROM (
                    SELECT MIN(id) AS keep_id
                    FROM mysterious_report_metric_snapshot
                    GROUP BY report_id, window_sec, bucket_start_ms
                ) AS keep_rows
            )
        """))
        await conn.execute(text(
            "CREATE UNIQUE INDEX uk_report_metric_report_window_bucket "
            "ON mysterious_report_metric_snapshot (report_id, window_sec, bucket_start_ms)"
        ))
        log.info("已创建 mysterious_report_metric_snapshot 唯一索引")


async def ensure_report_transaction_snapshot_table() -> None:
    """Create report transaction snapshot table for upgraded deployments."""
    async with async_engine.begin() as conn:
        dialect = conn.dialect.name
        tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
        id_type = "bigint(20) NOT NULL AUTO_INCREMENT" if dialect == "mysql" else "INTEGER NOT NULL"
        dt_default = "datetime NOT NULL DEFAULT CURRENT_TIMESTAMP" if dialect == "mysql" else "DATETIME NOT NULL"
        if "mysterious_report_transaction_snapshot" not in tables:
            ddl = f"""
            CREATE TABLE mysterious_report_transaction_snapshot (
                id {id_type},
                report_id bigint NOT NULL DEFAULT 0,
                transaction_name varchar(255) NOT NULL DEFAULT '',
                samples int NOT NULL DEFAULT 0,
                success_count int NOT NULL DEFAULT 0,
                fail_count int NOT NULL DEFAULT 0,
                success_rate double NOT NULL DEFAULT 0,
                tps double NOT NULL DEFAULT 0,
                avg_rt double NOT NULL DEFAULT 0,
                max_rt double NOT NULL DEFAULT 0,
                min_rt double NOT NULL DEFAULT 0,
                ratio double NOT NULL DEFAULT 0,
                creator_id varchar(32) NOT NULL DEFAULT '',
                creator varchar(32) NOT NULL DEFAULT '',
                modifier_id varchar(32) NOT NULL DEFAULT '',
                modifier varchar(32) NOT NULL DEFAULT '',
                create_time {dt_default},
                modify_time {dt_default},
                PRIMARY KEY (id)
            )
            """
            await conn.execute(text(ddl))
            log.info("已创建 mysterious_report_transaction_snapshot 表")

        indexes = await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).get_indexes("mysterious_report_transaction_snapshot")
        )
        if any(idx.get("name") == "uk_report_transaction_report_name" for idx in indexes):
            return

        await conn.execute(text("""
            DELETE FROM mysterious_report_transaction_snapshot
            WHERE id NOT IN (
                SELECT keep_id FROM (
                    SELECT MIN(id) AS keep_id
                    FROM mysterious_report_transaction_snapshot
                    GROUP BY report_id, transaction_name
                ) AS keep_rows
            )
        """))
        await conn.execute(text(
            "CREATE UNIQUE INDEX uk_report_transaction_report_name "
            "ON mysterious_report_transaction_snapshot (report_id, transaction_name)"
        ))
        log.info("已创建 mysterious_report_transaction_snapshot 唯一索引")


async def ensure_report_transaction_metric_snapshot_table() -> None:
    """Create report transaction metric snapshot table for upgraded deployments."""
    async with async_engine.begin() as conn:
        dialect = conn.dialect.name
        tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
        id_type = "bigint(20) NOT NULL AUTO_INCREMENT" if dialect == "mysql" else "INTEGER NOT NULL"
        dt_default = "datetime NOT NULL DEFAULT CURRENT_TIMESTAMP" if dialect == "mysql" else "DATETIME NOT NULL"
        if "mysterious_report_transaction_metric_snapshot" not in tables:
            ddl = f"""
            CREATE TABLE mysterious_report_transaction_metric_snapshot (
                id {id_type},
                report_id bigint NOT NULL DEFAULT 0,
                transaction_name varchar(255) NOT NULL DEFAULT '',
                window_sec int NOT NULL DEFAULT 60,
                bucket_start_ms bigint NOT NULL DEFAULT 0,
                timestamp varchar(32) NOT NULL DEFAULT '',
                qps double NOT NULL DEFAULT 0,
                avg_rt double NOT NULL DEFAULT 0,
                p95_rt double NOT NULL DEFAULT 0,
                p99_rt double NOT NULL DEFAULT 0,
                error_rate double NOT NULL DEFAULT 0,
                sample_count int NOT NULL DEFAULT 0,
                fail_count int NOT NULL DEFAULT 0,
                active_threads int NOT NULL DEFAULT 0,
                creator_id varchar(32) NOT NULL DEFAULT '',
                creator varchar(32) NOT NULL DEFAULT '',
                modifier_id varchar(32) NOT NULL DEFAULT '',
                modifier varchar(32) NOT NULL DEFAULT '',
                create_time {dt_default},
                modify_time {dt_default},
                PRIMARY KEY (id)
            )
            """
            await conn.execute(text(ddl))
            log.info("已创建 mysterious_report_transaction_metric_snapshot 表")

        columns = await conn.run_sync(
            lambda sync_conn: {
                col["name"] for col in inspect(sync_conn).get_columns("mysterious_report_transaction_metric_snapshot")
            }
        )
        if "active_threads" not in columns:
            ddl = (
                "int NOT NULL DEFAULT 0 COMMENT '交易活跃线程数'"
                if dialect == "mysql"
                else "INTEGER NOT NULL DEFAULT 0"
            )
            await conn.execute(text(
                f"ALTER TABLE mysterious_report_transaction_metric_snapshot ADD COLUMN active_threads {ddl}"
            ))
            log.info("已补齐 mysterious_report_transaction_metric_snapshot.active_threads 字段")

        indexes = await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).get_indexes("mysterious_report_transaction_metric_snapshot")
        )
        if any(idx.get("name") == "uk_report_transaction_metric_bucket" for idx in indexes):
            return

        await conn.execute(text("""
            DELETE FROM mysterious_report_transaction_metric_snapshot
            WHERE id NOT IN (
                SELECT keep_id FROM (
                    SELECT MIN(id) AS keep_id
                    FROM mysterious_report_transaction_metric_snapshot
                    GROUP BY report_id, transaction_name, window_sec, bucket_start_ms
                ) AS keep_rows
            )
        """))
        await conn.execute(text(
            "CREATE UNIQUE INDEX uk_report_transaction_metric_bucket "
            "ON mysterious_report_transaction_metric_snapshot "
            "(report_id, transaction_name, window_sec, bucket_start_ms)"
        ))
        log.info("已创建 mysterious_report_transaction_metric_snapshot 唯一索引")
