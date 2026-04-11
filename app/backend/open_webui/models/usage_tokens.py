import logging
import time
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, Integer, Text, UniqueConstraint, func
from sqlalchemy import asc, case, desc

from open_webui.internal.db import Base, get_db
from open_webui.env import GLOBAL_LOG_LEVEL

log = logging.getLogger(__name__)
log.setLevel(GLOBAL_LOG_LEVEL)


def _sum_optional(existing: Optional[int], incoming: Optional[int]) -> Optional[int]:
    if existing is None and incoming is None:
        return None
    return (existing or 0) + (incoming or 0)


class UsageToken(Base):
    __tablename__ = "usage_token"

    id = Column(Text, primary_key=True, unique=True)
    user_id = Column(Text, nullable=False)
    model_id = Column(Text, nullable=False)
    origin = Column(Text, nullable=False)
    usage_date = Column(Text, nullable=True)

    request_count = Column(Integer, nullable=False, default=1)

    prompt_tokens = Column(BigInteger, nullable=True)
    completion_tokens = Column(BigInteger, nullable=True)
    total_tokens = Column(BigInteger, nullable=True)

    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "model_id",
            "origin",
            "usage_date",
            name="usage_token_user_model_origin_idx",
        ),
    )


class UsageTokenModel(BaseModel):
    id: str
    user_id: str
    model_id: str
    origin: str
    usage_date: Optional[str] = None

    request_count: int

    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

    created_at: int
    updated_at: int

    model_config = ConfigDict(from_attributes=True)


class UsageTokenAggregate(BaseModel):
    user_id: str
    model_id: Optional[str] = None
    origin: Optional[str] = None
    usage_date: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    request_count: Optional[int] = None


class UsageTokensTable:
    def upsert_usage_token(
        self,
        *,
        user_id: str,
        model_id: str,
        origin: str,
        usage_date: Optional[str] = None,
        request_count: int = 1,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        total_tokens: Optional[int] = None,
    ) -> Optional[UsageTokenModel]:
        with get_db() as db:
            try:
                existing = (
                    db.query(UsageToken)
                    .filter(
                        UsageToken.user_id == user_id,
                        UsageToken.model_id == model_id,
                        UsageToken.origin == origin,
                        UsageToken.usage_date == usage_date,
                    )
                    .first()
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
                    existing.updated_at = int(time.time())

                    db.add(existing)
                    db.commit()
                    db.refresh(existing)
                    return UsageTokenModel.model_validate(existing)

                now = int(time.time())
                event = UsageTokenModel(
                    **{
                        "id": str(uuid.uuid4()),
                        "user_id": user_id,
                        "model_id": model_id,
                        "origin": origin,
                        "usage_date": usage_date,
                        "request_count": request_count,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": total_tokens,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                result = UsageToken(**event.model_dump())
                db.add(result)
                db.commit()
                db.refresh(result)
                return UsageTokenModel.model_validate(result) if result else None
            except Exception as e:
                log.exception(f"Error creating usage token record: {e}")
                return None

    def get_usage_by_user(
        self, user_id: str, *, usage_date: Optional[str] = None
    ) -> list[UsageTokenModel]:
        with get_db() as db:
            query = db.query(UsageToken).filter(UsageToken.user_id == user_id)
            if usage_date is not None:
                query = query.filter(UsageToken.usage_date == usage_date)
            return [UsageTokenModel.model_validate(row) for row in query.all()]

    def get_aggregate_usage(
        self,
        *,
        user_id: str,
        model_id: Optional[str] = None,
        origin: Optional[str] = None,
        usage_date: Optional[str] = None,
    ) -> UsageTokenAggregate:
        with get_db() as db:
            query = db.query(
                func.sum(UsageToken.prompt_tokens).label("prompt_tokens"),
                func.sum(UsageToken.completion_tokens).label("completion_tokens"),
                func.sum(UsageToken.total_tokens).label("total_tokens"),
                func.sum(UsageToken.request_count).label("request_count"),
            ).filter(UsageToken.user_id == user_id)

            if model_id is not None:
                query = query.filter(UsageToken.model_id == model_id)

            if origin is not None:
                query = query.filter(UsageToken.origin == origin)

            if usage_date is not None:
                query = query.filter(UsageToken.usage_date == usage_date)

            row = query.first()
            return UsageTokenAggregate(
                user_id=user_id,
                model_id=model_id,
                origin=origin,
                usage_date=usage_date,
                prompt_tokens=row.prompt_tokens if row else None,
                completion_tokens=row.completion_tokens if row else None,
                total_tokens=row.total_tokens if row else None,
                request_count=row.request_count if row else None,
            )

    def get_user_totals(
        self,
        *,
        user_ids: Optional[list[str]] = None,
        usage_date: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        with get_db() as db:
            query = (
                db.query(
                    UsageToken.user_id,
                    func.sum(UsageToken.total_tokens).label("total_tokens"),
                    func.sum(UsageToken.request_count).label("request_count"),
                )
                .group_by(UsageToken.user_id)
                .order_by(desc(func.sum(UsageToken.total_tokens)))
            )

            if user_ids is not None:
                query = query.filter(UsageToken.user_id.in_(user_ids))

            if usage_date is not None:
                query = query.filter(UsageToken.usage_date == usage_date)

            if offset:
                query = query.offset(offset)
            if limit:
                query = query.limit(limit)

            rows = query.all()
            return [
                {
                    "user_id": row.user_id,
                    "total_tokens": int(row.total_tokens or 0),
                    "request_count": int(row.request_count or 0),
                }
                for row in rows
            ]

    def get_user_origin_totals(
        self, *, user_id: str, usage_date: Optional[str] = None
    ) -> dict:
        with get_db() as db:
            query = (
                db.query(
                    UsageToken.origin,
                    func.sum(UsageToken.total_tokens).label("total_tokens"),
                )
                .filter(UsageToken.user_id == user_id)
                .group_by(UsageToken.origin)
            )
            if usage_date is not None:
                query = query.filter(UsageToken.usage_date == usage_date)
            rows = query.all()

            result = {"internal": 0, "external": 0}
            for row in rows:
                if row.origin in result:
                    result[row.origin] = int(row.total_tokens or 0)
            return result

    def get_users_origin_totals(
        self, *, user_ids: list[str], usage_date: Optional[str] = None
    ) -> dict:
        if not user_ids:
            return {}
        with get_db() as db:
            query = (
                db.query(
                    UsageToken.user_id,
                    UsageToken.origin,
                    func.sum(UsageToken.total_tokens).label("total_tokens"),
                )
                .filter(UsageToken.user_id.in_(user_ids))
                .group_by(UsageToken.user_id, UsageToken.origin)
            )
            if usage_date is not None:
                query = query.filter(UsageToken.usage_date == usage_date)
            rows = query.all()

            result: dict[str, dict] = {uid: {"internal": 0, "external": 0} for uid in user_ids}
            for row in rows:
                if row.user_id in result and row.origin in result[row.user_id]:
                    result[row.user_id][row.origin] = int(row.total_tokens or 0)
            return result

    def get_sorted_user_summaries(
        self,
        *,
        query: Optional[str] = None,
        usage_date: Optional[str] = None,
        sort_by: str = "total_tokens",
        sort_order: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        from open_webui.models.users import User

        with get_db() as db:
            # Daily aggregation subquery
            daily_sub = (
                db.query(
                    UsageToken.user_id,
                    func.coalesce(func.sum(UsageToken.total_tokens), 0).label(
                        "daily_tokens"
                    ),
                    func.coalesce(func.sum(UsageToken.request_count), 0).label(
                        "request_count"
                    ),
                )
                .filter(UsageToken.usage_date == usage_date)
                .group_by(UsageToken.user_id)
                .subquery("daily")
            )

            # Total aggregation subquery (all time)
            total_sub = (
                db.query(
                    UsageToken.user_id,
                    func.coalesce(func.sum(UsageToken.total_tokens), 0).label(
                        "total_tokens"
                    ),
                )
                .group_by(UsageToken.user_id)
                .subquery("totals")
            )

            # Origin aggregation subquery (daily)
            origin_sub = (
                db.query(
                    UsageToken.user_id,
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    UsageToken.origin == "internal",
                                    UsageToken.total_tokens,
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ).label("internal_tokens"),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    UsageToken.origin == "external",
                                    UsageToken.total_tokens,
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ).label("external_tokens"),
                )
                .filter(UsageToken.usage_date == usage_date)
                .group_by(UsageToken.user_id)
                .subquery("origins")
            )

            # Label expressions for sorting
            daily_tokens_col = func.coalesce(daily_sub.c.daily_tokens, 0).label(
                "daily_tokens"
            )
            total_tokens_col = func.coalesce(total_sub.c.total_tokens, 0).label(
                "total_tokens"
            )
            request_count_col = func.coalesce(daily_sub.c.request_count, 0).label(
                "request_count"
            )
            internal_tokens_col = func.coalesce(
                origin_sub.c.internal_tokens, 0
            ).label("internal_tokens")
            external_tokens_col = func.coalesce(
                origin_sub.c.external_tokens, 0
            ).label("external_tokens")

            main_query = (
                db.query(
                    User.id,
                    User.name,
                    User.email,
                    daily_tokens_col,
                    total_tokens_col,
                    request_count_col,
                    internal_tokens_col,
                    external_tokens_col,
                )
                .outerjoin(daily_sub, User.id == daily_sub.c.user_id)
                .outerjoin(total_sub, User.id == total_sub.c.user_id)
                .outerjoin(origin_sub, User.id == origin_sub.c.user_id)
            )

            # Search filter
            if query:
                search_term = f"%{query}%"
                main_query = main_query.filter(
                    (User.name.ilike(search_term)) | (User.email.ilike(search_term))
                )

            # Count total (separate query on User table for efficiency)
            count_q = db.query(func.count(User.id))
            if query:
                search_term = f"%{query}%"
                count_q = count_q.filter(
                    (User.name.ilike(search_term)) | (User.email.ilike(search_term))
                )
            total = count_q.scalar()

            # Sorting
            sort_column_map = {
                "name": User.name,
                "email": User.email,
                "daily_tokens": daily_tokens_col,
                "total_tokens": total_tokens_col,
                "request_count": request_count_col,
                "internal_tokens": internal_tokens_col,
                "external_tokens": external_tokens_col,
            }

            sort_col = sort_column_map.get(sort_by, total_tokens_col)
            if sort_order == "asc":
                main_query = main_query.order_by(asc(sort_col))
            else:
                main_query = main_query.order_by(desc(sort_col))

            main_query = main_query.offset(offset).limit(limit)

            rows = main_query.all()
            items = [
                {
                    "user_id": row.id,
                    "name": row.name,
                    "email": row.email,
                    "daily_tokens": int(row.daily_tokens),
                    "total_tokens": int(row.total_tokens),
                    "request_count": int(row.request_count),
                    "internal_tokens": int(row.internal_tokens),
                    "external_tokens": int(row.external_tokens),
                }
                for row in rows
            ]

            return {"total": total, "items": items}

    def get_user_count(self, *, user_ids: Optional[list[str]] = None) -> int:
        with get_db() as db:
            query = db.query(func.count(func.distinct(UsageToken.user_id)))
            if user_ids is not None:
                query = query.filter(UsageToken.user_id.in_(user_ids))
            row = query.first()
            return int(row[0] or 0)


UsageTokens = UsageTokensTable()
