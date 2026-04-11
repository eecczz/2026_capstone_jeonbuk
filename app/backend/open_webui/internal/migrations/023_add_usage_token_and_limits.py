"""Peewee migration to add usage_token and usage_limit tables."""

import logging
from contextlib import suppress

import peewee as pw
from peewee_migrate import Migrator

with suppress(ImportError):
    import playhouse.postgres_ext as pw_pext  # noqa: F401


def migrate(migrator: Migrator, database: pw.Database, *, fake: bool = False):
    try:
        if not database.table_exists("usage_token"):
            database.execute_sql(
                """
                CREATE TABLE usage_token (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 1,
                    prompt_tokens BIGINT NULL,
                    completion_tokens BIGINT NULL,
                    total_tokens BIGINT NULL,
                    created_at BIGINT NOT NULL,
                    updated_at BIGINT NOT NULL
                )
                """
            )
            database.execute_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS usage_token_user_model_origin_idx ON usage_token (user_id, model_id, origin)"
            )
            database.execute_sql(
                "CREATE INDEX IF NOT EXISTS usage_token_user_id_idx ON usage_token (user_id)"
            )
    except Exception as e:
        logging.warning(f"[023_add_usage_token_and_limits] Failed to create usage_token: {e}")

    try:
        if not database.table_exists("usage_limit"):
            database.execute_sql(
                """
                CREATE TABLE usage_limit (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    model_id TEXT NULL,
                    origin TEXT NULL,
                    token_limit BIGINT NULL,
                    created_at BIGINT NOT NULL,
                    updated_at BIGINT NOT NULL
                )
                """
            )
            database.execute_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS usage_limit_user_model_origin_idx ON usage_limit (user_id, model_id, origin)"
            )
            database.execute_sql(
                "CREATE INDEX IF NOT EXISTS usage_limit_user_id_idx ON usage_limit (user_id)"
            )
    except Exception as e:
        logging.warning(f"[023_add_usage_token_and_limits] Failed to create usage_limit: {e}")


def rollback(migrator: Migrator, database: pw.Database, *, fake: bool = False):
    try:
        if database.table_exists("usage_token"):
            database.execute_sql("DROP TABLE usage_token")
    except Exception as e:
        logging.warning(f"[023_add_usage_token_and_limits] Failed to drop usage_token: {e}")

    try:
        if database.table_exists("usage_limit"):
            database.execute_sql("DROP TABLE usage_limit")
    except Exception as e:
        logging.warning(f"[023_add_usage_token_and_limits] Failed to drop usage_limit: {e}")
