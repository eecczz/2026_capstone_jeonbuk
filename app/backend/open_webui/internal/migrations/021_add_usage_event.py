"""Peewee migration to add usage_event table for per-user usage tracking."""

import logging
from contextlib import suppress

import peewee as pw
from peewee_migrate import Migrator

with suppress(ImportError):
    import playhouse.postgres_ext as pw_pext  # noqa: F401


def migrate(migrator: Migrator, database: pw.Database, *, fake: bool = False):
    try:
        if database.table_exists("usage_event"):
            logging.warning("[021_add_usage_event] Table 'usage_event' already exists.")
            return
    except Exception as e:
        logging.warning(f"[021_add_usage_event] Unable to inspect tables: {e}")
        return

    try:
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
            "CREATE INDEX IF NOT EXISTS usage_event_user_id_created_at_idx ON usage_event (user_id, created_at)"
        )
        database.execute_sql(
            "CREATE INDEX IF NOT EXISTS usage_event_category_provider_created_at_idx ON usage_event (category, provider, created_at)"
        )
    except Exception as e:
        logging.warning(f"[021_add_usage_event] Failed to create table: {e}")


def rollback(migrator: Migrator, database: pw.Database, *, fake: bool = False):
    try:
        if not database.table_exists("usage_event"):
            return
        database.execute_sql("DROP TABLE usage_event")
    except Exception as e:
        logging.warning(f"[021_add_usage_event] Failed to drop table: {e}")
