"""chatbot_db 의 alembic env — OWI customui 와 완전 독립.

- DB URL: DATABASE_URL_CHATBOT 우선, 없으면 OWI DATABASE_URL 의 dbname 만
  chatbot_db 로 치환.
- target_metadata: ChatbotBase.metadata (crawler 3 table 만).
"""
import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from open_webui.internal.db_chatbot import (
    CHATBOT_DATABASE_URL,
    ChatbotBase,
)
# crawler 모델 import 시 ChatbotBase.metadata 에 자동 등록
from open_webui.models import crawler  # noqa: F401


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


target_metadata = ChatbotBase.metadata

DB_URL = CHATBOT_DATABASE_URL
if DB_URL:
    config.set_main_option("sqlalchemy.url", DB_URL.replace("%", "%%"))


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
