"""Peewee migration to add usage_date to usage_token."""

import logging
from contextlib import suppress
from datetime import datetime, timezone, timedelta

import peewee as pw
from peewee_migrate import Migrator

with suppress(ImportError):
    import playhouse.postgres_ext as pw_pext  # noqa: F401


def _get_kst_date_str() -> str:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    except Exception:
        return datetime.now(timezone(timedelta(hours=9))).date().isoformat()


def migrate(migrator: Migrator, database: pw.Database, *, fake: bool = False):
    try:
        if database.table_exists("usage_token"):
            columns = [col.name for col in database.get_columns("usage_token")]
            if "usage_date" not in columns:
                database.execute_sql("ALTER TABLE usage_token ADD COLUMN usage_date TEXT NULL")

            date_str = _get_kst_date_str()
            # Use literal to avoid driver param-style mismatch during migration
            database.execute_sql(
                f"UPDATE usage_token SET usage_date = '{date_str}' WHERE usage_date IS NULL"
            )

            # Rebuild unique index to include usage_date
            database.execute_sql(
                "DROP INDEX IF EXISTS usage_token_user_model_origin_idx"
            )
            database.execute_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS usage_token_user_model_origin_idx "
                "ON usage_token (user_id, model_id, origin, usage_date)"
            )
            database.execute_sql(
                "CREATE INDEX IF NOT EXISTS usage_token_usage_date_idx ON usage_token (usage_date)"
            )
    except Exception as e:
        logging.warning(
            f"[024_add_usage_date_to_usage_token] Failed to update usage_token: {e}"
        )


def rollback(migrator: Migrator, database: pw.Database, *, fake: bool = False):
    try:
        if database.table_exists("usage_token"):
            database.execute_sql(
                "DROP INDEX IF EXISTS usage_token_usage_date_idx"
            )
            database.execute_sql(
                "DROP INDEX IF EXISTS usage_token_user_model_origin_idx"
            )
            database.execute_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS usage_token_user_model_origin_idx "
                "ON usage_token (user_id, model_id, origin)"
            )
    except Exception as e:
        logging.warning(
            f"[024_add_usage_date_to_usage_token] Failed to rollback usage_token: {e}"
        )
