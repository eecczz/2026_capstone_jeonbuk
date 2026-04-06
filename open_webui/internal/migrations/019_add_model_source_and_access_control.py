"""Peewee migrations -- 019_add_model_source_and_access_control.py.

This migration aligns the stored `model` table schema with the SQLAlchemy model
definition used by the backend. It adds the new columns introduced for GPTs
(`source`) as well as the newer control fields (`is_active`, `access_control`).
"""

import logging
from contextlib import suppress

import peewee as pw
from peewee_migrate import Migrator

with suppress(ImportError):
    import playhouse.postgres_ext as pw_pext  # noqa: F401


def _missing_columns(database: pw.Database, table: str, columns: list[str]) -> list[str]:
    try:
        existing = {col.name for col in database.get_columns(table)}
    except Exception as e:  # Defensive: some test/mocked DBs surface non-iterable cursors
        logging.warning(f"[019_add_model_source_and_access_control] Skipping column check on {table}: {e}")
        return []

    return [col for col in columns if col not in existing]


def migrate(migrator: Migrator, database: pw.Database, *, fake: bool = False):
    """Apply the migration."""

    try:
        has_model_table = database.table_exists("model")
    except Exception as e:
        logging.warning(f"[019_add_model_source_and_access_control] Unable to inspect tables: {e}")
        return

    if not has_model_table:
        logging.warning("[019_add_model_source_and_access_control] Table 'model' not found, skipping.")
        return

    missing = _missing_columns(database, "model", ["access_control", "is_active", "source"])

    if not missing:
        return

    for col in missing:
        try:
            if col == "access_control":
                database.execute_sql("ALTER TABLE model ADD COLUMN access_control TEXT")
            elif col == "is_active":
                database.execute_sql("ALTER TABLE model ADD COLUMN is_active BOOLEAN DEFAULT TRUE")
            elif col == "source":
                database.execute_sql("ALTER TABLE model ADD COLUMN source TEXT DEFAULT 'workspace'")
        except Exception as e:
            logging.warning(f"[019_add_model_source_and_access_control] Skipping column {col}: {e}")


def rollback(migrator: Migrator, database: pw.Database, *, fake: bool = False):
    """Rollback the migration."""

    try:
        current = {col.name for col in database.get_columns("model")}
    except Exception as e:
        logging.warning(f"[019_add_model_source_and_access_control] Unable to inspect columns on rollback: {e}")
        return

    to_remove = [col for col in ["access_control", "is_active", "source"] if col in current]
    for col in to_remove:
        try:
            database.execute_sql(f"ALTER TABLE model DROP COLUMN {col}")
        except Exception as e:
            logging.warning(f"[019_add_model_source_and_access_control] Failed to drop column {col}: {e}")
