"""Peewee migration to add `source` (and optionally `access_control`) to knowledge.

This keeps the database schema aligned with the SQLAlchemy model which expects
`knowledge.source` (used by GPTs filtering). The migration is idempotent and only
adds columns that are missing.
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
        logging.warning(f"[020_add_knowledge_source] Skipping column check on {table}: {e}")
        return []

    return [col for col in columns if col not in existing]


def migrate(migrator: Migrator, database: pw.Database, *, fake: bool = False):
    try:
        has_table = database.table_exists("knowledge")
    except Exception as e:
        logging.warning(f"[020_add_knowledge_source] Unable to inspect tables: {e}")
        return

    if not has_table:
        logging.warning("[020_add_knowledge_source] Table 'knowledge' not found, skipping.")
        return

    missing = _missing_columns(database, "knowledge", ["access_control", "source"])

    for col in missing:
        try:
            if col == "access_control":
                database.execute_sql("ALTER TABLE knowledge ADD COLUMN access_control TEXT")
            elif col == "source":
                database.execute_sql("ALTER TABLE knowledge ADD COLUMN source TEXT DEFAULT 'workspace'")
        except Exception as e:
            logging.warning(f"[020_add_knowledge_source] Skipping column {col}: {e}")


def rollback(migrator: Migrator, database: pw.Database, *, fake: bool = False):
    try:
        current = {col.name for col in database.get_columns("knowledge")}
    except Exception as e:
        logging.warning(f"[020_add_knowledge_source] Unable to inspect columns on rollback: {e}")
        return

    to_remove = [col for col in ["access_control", "source"] if col in current]
    for col in to_remove:
        try:
            database.execute_sql(f"ALTER TABLE knowledge DROP COLUMN {col}")
        except Exception as e:
            logging.warning(f"[020_add_knowledge_source] Failed to drop column {col}: {e}")
