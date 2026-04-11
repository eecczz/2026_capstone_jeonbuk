"""Peewee migration to aggregate usage_event to one row per user."""

import logging
from contextlib import suppress

import peewee as pw
from peewee_migrate import Migrator

with suppress(ImportError):
    import playhouse.postgres_ext as pw_pext  # noqa: F401


def migrate(migrator: Migrator, database: pw.Database, *, fake: bool = False):
    try:
        if not database.table_exists("usage_event"):
            return
    except Exception as e:
        logging.warning(f"[022_aggregate_usage_event_by_user] Unable to inspect tables: {e}")
        return

    try:
        database.execute_sql("ALTER TABLE usage_event RENAME TO usage_event_old")

        database.execute_sql(
            """
            CREATE TABLE usage_event (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                category TEXT NOT NULL,
                provider TEXT NULL,
                model_id TEXT NULL,
                endpoint TEXT NOT NULL,
                request_count INTEGER NOT NULL DEFAULT 1,
                prompt_tokens BIGINT NULL,
                completion_tokens BIGINT NULL,
                total_tokens BIGINT NULL,
                input_bytes BIGINT NULL,
                output_bytes BIGINT NULL,
                status_code INTEGER NULL,
                latency_ms INTEGER NULL,
                chat_id TEXT NULL,
                message_id TEXT NULL,
                created_at BIGINT NOT NULL
            )
            """
        )

        database.execute_sql(
            """
            INSERT INTO usage_event (
                id,
                user_id,
                kind,
                category,
                provider,
                model_id,
                endpoint,
                request_count,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                input_bytes,
                output_bytes,
                status_code,
                latency_ms,
                chat_id,
                message_id,
                created_at
            )
            SELECT
                MIN(id) AS id,
                user_id,
                'aggregate' AS kind,
                'aggregate' AS category,
                NULL AS provider,
                NULL AS model_id,
                'aggregate' AS endpoint,
                SUM(request_count) AS request_count,
                SUM(prompt_tokens) AS prompt_tokens,
                SUM(completion_tokens) AS completion_tokens,
                SUM(total_tokens) AS total_tokens,
                SUM(input_bytes) AS input_bytes,
                SUM(output_bytes) AS output_bytes,
                NULL AS status_code,
                NULL AS latency_ms,
                NULL AS chat_id,
                NULL AS message_id,
                MAX(created_at) AS created_at
            FROM usage_event_old
            GROUP BY user_id
            """
        )

        database.execute_sql("DROP TABLE usage_event_old")

        database.execute_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS usage_event_user_id_idx ON usage_event (user_id)"
        )
        database.execute_sql(
            "CREATE INDEX IF NOT EXISTS usage_event_user_id_created_at_idx ON usage_event (user_id, created_at)"
        )
        database.execute_sql(
            "CREATE INDEX IF NOT EXISTS usage_event_category_provider_created_at_idx ON usage_event (category, provider, created_at)"
        )
    except Exception as e:
        logging.warning(f"[022_aggregate_usage_event_by_user] Failed to aggregate usage_event: {e}")


def rollback(migrator: Migrator, database: pw.Database, *, fake: bool = False):
    logging.warning(
        "[022_aggregate_usage_event_by_user] Rollback not supported (data loss)."
    )
