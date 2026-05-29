"""챗봇 own DB (chatbot_db) — crawler 3 table 만 격리.

본 컨테이너 이관 시 customui (OWI 본체) 와 별개로 dump/restore 하기 위한
data 격리. SQLAlchemy 별도 engine + Base + session 으로 OWI 본체와 충돌 없이
운영. customui 에 동일 schema 의 crawler 3 table 이 임시로 남아있을 수 있지만,
Phase 1b 이후 우리 코드는 chatbot_db 만 쳐다본다.

사용:
    from open_webui.internal.db_chatbot import (
        ChatbotBase, get_chatbot_db_context,
    )

    class CrawledPage(ChatbotBase):
        __tablename__ = "crawled_page"
        ...

    with get_chatbot_db_context() as db:
        db.query(CrawledPage).filter(...).first()

env 변수:
    DATABASE_URL_CHATBOT — 미설정 시 customui 의 DATABASE_URL 에서 dbname 만
    'chatbot_db' 로 치환해 default 구성.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import (
    Session,
    declarative_base,
    scoped_session,
    sessionmaker,
)

from open_webui.env import DATABASE_URL as _OWI_DATABASE_URL


def _resolve_chatbot_db_url() -> str:
    """DATABASE_URL_CHATBOT 우선, 없으면 OWI DATABASE_URL 의 dbname 만 교체."""
    url = os.environ.get("DATABASE_URL_CHATBOT", "").strip()
    if url:
        return url
    # OWI DATABASE_URL = postgresql://admin:sprint26!@localhost:5432/customui
    # → postgresql://admin:sprint26!@localhost:5432/chatbot_db
    if not _OWI_DATABASE_URL:
        return "postgresql://admin:sprint26!@localhost:5432/chatbot_db"
    last_slash = _OWI_DATABASE_URL.rfind("/")
    if last_slash <= 0:
        return _OWI_DATABASE_URL
    return _OWI_DATABASE_URL[: last_slash + 1] + "chatbot_db"


CHATBOT_DATABASE_URL = _resolve_chatbot_db_url()

chatbot_engine = create_engine(
    CHATBOT_DATABASE_URL,
    pool_pre_ping=True,
    pool_size=int(os.environ.get("DATABASE_POOL_SIZE_CHATBOT", "5")),
    max_overflow=int(os.environ.get("DATABASE_POOL_MAX_OVERFLOW_CHATBOT", "10")),
)

ChatbotSessionLocal = sessionmaker(
    autocommit=False, autoflush=False, bind=chatbot_engine, expire_on_commit=False
)

# OWI Base 와 metadata 완전 분리 — 같은 __tablename__ 가 OWI customui 에도 있어도
# chatbot_db 의 같은 이름 table 로 mapping 됨. metadata 가 다르므로 충돌 X.
ChatbotBase = declarative_base()

ChatbotScopedSession = scoped_session(ChatbotSessionLocal)


def get_chatbot_session():
    db = ChatbotSessionLocal()
    try:
        yield db
    finally:
        db.close()


get_chatbot_db = contextmanager(get_chatbot_session)


@contextmanager
def get_chatbot_db_context(db: Optional[Session] = None):
    """기존 session 재사용 가능 (OWI 의 get_db_context 와 같은 패턴)."""
    if isinstance(db, Session):
        yield db
    else:
        sess = ChatbotSessionLocal()
        try:
            yield sess
        finally:
            sess.close()
