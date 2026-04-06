import logging
import time
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, Text, UniqueConstraint

from open_webui.internal.db import Base, get_db
from open_webui.env import GLOBAL_LOG_LEVEL

log = logging.getLogger(__name__)
log.setLevel(GLOBAL_LOG_LEVEL)


class UsageLimit(Base):
    __tablename__ = "usage_limit"

    id = Column(Text, primary_key=True, unique=True)
    user_id = Column(Text, nullable=False)
    model_id = Column(Text, nullable=True)
    origin = Column(Text, nullable=True)
    token_limit = Column(BigInteger, nullable=True)

    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "model_id", "origin", name="usage_limit_user_model_origin_idx"),
    )


class UsageLimitModel(BaseModel):
    id: str
    user_id: str
    model_id: Optional[str] = None
    origin: Optional[str] = None
    token_limit: Optional[int] = None
    created_at: int
    updated_at: int

    model_config = ConfigDict(from_attributes=True)


class UsageLimitForm(BaseModel):
    user_id: Optional[str] = None
    model_id: Optional[str] = None
    origin: Optional[str] = None
    token_limit: Optional[int] = None


class UsageLimitsTable:
    def upsert_limit(self, form: UsageLimitForm) -> Optional[UsageLimitModel]:
        with get_db() as db:
            try:
                existing = (
                    db.query(UsageLimit)
                    .filter(
                        UsageLimit.user_id == form.user_id,
                        UsageLimit.model_id == form.model_id,
                        UsageLimit.origin == form.origin,
                    )
                    .first()
                )
                now = int(time.time())
                if existing:
                    existing.token_limit = form.token_limit
                    existing.updated_at = now
                    db.add(existing)
                    db.commit()
                    db.refresh(existing)
                    return UsageLimitModel.model_validate(existing)

                limit = UsageLimitModel(
                    **{
                        "id": str(uuid.uuid4()),
                        "user_id": form.user_id,
                        "model_id": form.model_id,
                        "origin": form.origin,
                        "token_limit": form.token_limit,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                result = UsageLimit(**limit.model_dump())
                db.add(result)
                db.commit()
                db.refresh(result)
                return UsageLimitModel.model_validate(result) if result else None
            except Exception as e:
                log.exception(f"Error upserting usage limit: {e}")
                return None

    def delete_limit(self, *, user_id: str, model_id: Optional[str], origin: Optional[str]) -> bool:
        with get_db() as db:
            try:
                db.query(UsageLimit).filter(
                    UsageLimit.user_id == user_id,
                    UsageLimit.model_id == model_id,
                    UsageLimit.origin == origin,
                ).delete()
                db.commit()
                return True
            except Exception as e:
                log.exception(f"Error deleting usage limit: {e}")
                return False

    def get_limits_by_user(self, user_id: str) -> list[UsageLimitModel]:
        with get_db() as db:
            return [
                UsageLimitModel.model_validate(row)
                for row in db.query(UsageLimit).filter(UsageLimit.user_id == user_id).all()
            ]

    def get_all_limits(self) -> list[UsageLimitModel]:
        with get_db() as db:
            return [UsageLimitModel.model_validate(row) for row in db.query(UsageLimit).all()]


UsageLimits = UsageLimitsTable()
