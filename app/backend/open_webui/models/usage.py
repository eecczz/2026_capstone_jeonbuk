import logging
import time
import uuid
from typing import Optional

from open_webui.internal.db import Base, get_db
from open_webui.env import GLOBAL_LOG_LEVEL

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, Integer, Text

log = logging.getLogger(__name__)
log.setLevel(GLOBAL_LOG_LEVEL)


def _sum_optional(existing: Optional[int], incoming: Optional[int]) -> Optional[int]:
    if existing is None and incoming is None:
        return None
    return (existing or 0) + (incoming or 0)


####################
# Usage DB Schema
####################


class UsageEvent(Base):
    __tablename__ = "usage_event"

    id = Column(Text, primary_key=True, unique=True)
    user_id = Column(Text, nullable=False)
    kind = Column(Text, nullable=False)
    category = Column(Text, nullable=False)
    provider = Column(Text, nullable=True)
    model_id = Column(Text, nullable=True)
    endpoint = Column(Text, nullable=False)
    request_count = Column(Integer, nullable=False, default=1)

    prompt_tokens = Column(BigInteger, nullable=True)
    completion_tokens = Column(BigInteger, nullable=True)
    total_tokens = Column(BigInteger, nullable=True)

    input_bytes = Column(BigInteger, nullable=True)
    output_bytes = Column(BigInteger, nullable=True)

    status_code = Column(Integer, nullable=True)
    latency_ms = Column(Integer, nullable=True)

    chat_id = Column(Text, nullable=True)
    message_id = Column(Text, nullable=True)

    created_at = Column(BigInteger, nullable=False)


class UsageEventModel(BaseModel):
    id: str
    user_id: str
    kind: str
    category: str
    provider: Optional[str] = None
    model_id: Optional[str] = None
    endpoint: str
    request_count: int

    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

    input_bytes: Optional[int] = None
    output_bytes: Optional[int] = None

    status_code: Optional[int] = None
    latency_ms: Optional[int] = None

    chat_id: Optional[str] = None
    message_id: Optional[str] = None

    created_at: int

    model_config = ConfigDict(from_attributes=True)


class UsageEventsTable:
    def insert_new_usage_event(
        self,
        *,
        user_id: str,
        kind: str,
        category: str,
        endpoint: str,
        provider: Optional[str] = None,
        model_id: Optional[str] = None,
        request_count: int = 1,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        total_tokens: Optional[int] = None,
        input_bytes: Optional[int] = None,
        output_bytes: Optional[int] = None,
        status_code: Optional[int] = None,
        latency_ms: Optional[int] = None,
        chat_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> Optional[UsageEventModel]:
        with get_db() as db:
            try:
                existing = (
                    db.query(UsageEvent).filter(UsageEvent.user_id == user_id).first()
                )
                if existing:
                    existing.request_count = (existing.request_count or 0) + (
                        request_count or 0
                    )
                    existing.prompt_tokens = _sum_optional(
                        existing.prompt_tokens, prompt_tokens
                    )
                    existing.completion_tokens = _sum_optional(
                        existing.completion_tokens, completion_tokens
                    )
                    existing.total_tokens = _sum_optional(
                        existing.total_tokens, total_tokens
                    )
                    existing.input_bytes = _sum_optional(
                        existing.input_bytes, input_bytes
                    )
                    existing.output_bytes = _sum_optional(
                        existing.output_bytes, output_bytes
                    )
                    existing.kind = kind
                    existing.category = category
                    existing.endpoint = endpoint
                    existing.provider = provider
                    existing.model_id = model_id
                    if status_code is not None:
                        existing.status_code = status_code
                    if latency_ms is not None:
                        existing.latency_ms = latency_ms
                    if chat_id:
                        existing.chat_id = chat_id
                    if message_id:
                        existing.message_id = message_id
                    existing.created_at = int(time.time())
                    db.add(existing)
                    db.commit()
                    db.refresh(existing)
                    return UsageEventModel.model_validate(existing)

                event = UsageEventModel(
                    **{
                        "id": str(uuid.uuid4()),
                        "user_id": user_id,
                        "kind": kind,
                        "category": category,
                        "provider": provider,
                        "model_id": model_id,
                        "endpoint": endpoint,
                        "request_count": request_count,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": total_tokens,
                        "input_bytes": input_bytes,
                        "output_bytes": output_bytes,
                        "status_code": status_code,
                        "latency_ms": latency_ms,
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "created_at": int(time.time()),
                    }
                )
                result = UsageEvent(**event.model_dump())
                db.add(result)
                db.commit()
                db.refresh(result)
                return UsageEventModel.model_validate(result) if result else None
            except Exception as e:
                log.exception(f"Error creating usage event: {e}")
                return None


UsageEvents = UsageEventsTable()
