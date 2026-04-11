import logging
from typing import Optional

from fastapi import HTTPException, status

from open_webui.env import GLOBAL_LOG_LEVEL
from open_webui.models.usage_limits import UsageLimits, UsageLimitModel
from open_webui.models.usage_tokens import UsageTokens
from open_webui.utils.time_utils import get_kst_date_str

log = logging.getLogger(__name__)
log.setLevel(GLOBAL_LOG_LEVEL)


def _get_applicable_limits(
    limits: list[UsageLimitModel], *, user_id: str, model_id: Optional[str], origin: Optional[str]
) -> list[UsageLimitModel]:
    applicable = []
    for limit in limits:
        if limit.user_id != user_id:
            continue

        if limit.model_id is not None and limit.origin is not None:
            if limit.model_id == model_id and limit.origin == origin:
                applicable.append(limit)
            continue

        if limit.model_id is not None:
            if limit.model_id == model_id:
                applicable.append(limit)
            continue

        if limit.origin is not None:
            if limit.origin == origin:
                applicable.append(limit)
            continue

        # Global limit (no model_id, no origin)
        applicable.append(limit)

    return applicable


def resolve_effective_limit(
    *, user_id: str, model_id: Optional[str], origin: Optional[str]
) -> Optional[int]:
    limits = UsageLimits.get_limits_by_user(user_id)
    applicable = _get_applicable_limits(
        limits, user_id=user_id, model_id=model_id, origin=origin
    )

    token_limits = [
        limit.token_limit for limit in applicable if limit.token_limit is not None
    ]
    if not token_limits:
        return None
    return min(token_limits)


def resolve_effective_limit_for_role(
    *, role: str, model_id: Optional[str], origin: Optional[str]
) -> Optional[int]:
    role_user_id = f"role:{role}"
    return resolve_effective_limit(
        user_id=role_user_id, model_id=model_id, origin=origin
    )


def get_usage_for_scope(
    *, user_id: str, model_id: Optional[str], origin: Optional[str]
) -> int:
    agg = UsageTokens.get_aggregate_usage(
        user_id=user_id,
        model_id=model_id,
        origin=origin,
        usage_date=get_kst_date_str(),
    )
    return int(agg.total_tokens or 0)


def check_usage_limit(
    *,
    user_id: str,
    role: str,
    model_id: Optional[str],
    origin: Optional[str],
    estimated_tokens: Optional[int] = None,
):
    if role == "admin":
        return

    role_user_id = f"role:{role}"
    limits = UsageLimits.get_limits_by_user(role_user_id)
    applicable = _get_applicable_limits(
        limits, user_id=role_user_id, model_id=model_id, origin=origin
    )

    for limit in applicable:
        if limit.token_limit is None:
            continue

        scope_model_id = limit.model_id if limit.model_id is not None else None
        scope_origin = limit.origin if limit.origin is not None else None

        used = get_usage_for_scope(
            user_id=user_id, model_id=scope_model_id, origin=scope_origin
        )
        projected = used + (estimated_tokens or 0)

        if estimated_tokens is None:
            if used >= limit.token_limit:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="사용량 한도를 초과했습니다.",
                )
        else:
            if projected > limit.token_limit:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="사용량 한도를 초과했습니다.",
                )
