import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from open_webui.models.usage_limits import UsageLimits, UsageLimitForm
from open_webui.models.usage_tokens import UsageTokens
from open_webui.models.users import Users
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.usage_limits import resolve_effective_limit_for_role
from open_webui.utils.time_utils import get_kst_date_str

router = APIRouter()


@router.get("/usage/summary")
async def get_usage_summary(user=Depends(get_verified_user)):
    user_id = user.id
    usage_date = get_kst_date_str()
    usage_rows = UsageTokens.get_usage_by_user(user_id, usage_date=usage_date)
    limits = UsageLimits.get_limits_by_user(f"role:{user.role}")

    models_map = {}
    for row in usage_rows:
        key = (row.model_id, row.origin)
        limit = resolve_effective_limit_for_role(
            role=user.role, model_id=row.model_id, origin=row.origin
        )
        remaining = None
        if limit is not None and row.total_tokens is not None:
            remaining = max(limit - row.total_tokens, 0)

        models_map[key] = {
            "model_id": row.model_id,
            "origin": row.origin,
            "prompt_tokens": row.prompt_tokens or 0,
            "completion_tokens": row.completion_tokens or 0,
            "total_tokens": row.total_tokens or 0,
            "request_count": row.request_count or 0,
            "limit": limit,
            "remaining": remaining,
        }

    for limit in limits:
        if not limit.model_id:
            continue
        key = (limit.model_id, limit.origin or None)
        if key in models_map:
            continue

        remaining = None
        if limit.token_limit is not None:
            remaining = limit.token_limit

        models_map[key] = {
            "model_id": limit.model_id,
            "origin": limit.origin,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "request_count": 0,
            "limit": limit.token_limit,
            "remaining": remaining,
        }

    models = list(models_map.values())

    origin_totals = []
    for origin in ["internal", "external"]:
        agg = UsageTokens.get_aggregate_usage(
            user_id=user_id, model_id=None, origin=origin, usage_date=usage_date
        )
        limit = resolve_effective_limit_for_role(
            role=user.role, model_id=None, origin=origin
        )
        remaining = None
        if limit is not None and agg.total_tokens is not None:
            remaining = max(limit - agg.total_tokens, 0)

        origin_totals.append(
            {
                "origin": origin,
                "prompt_tokens": agg.prompt_tokens or 0,
                "completion_tokens": agg.completion_tokens or 0,
                "total_tokens": agg.total_tokens or 0,
                "request_count": agg.request_count or 0,
                "limit": limit,
                "remaining": remaining,
            }
        )

    return {
        "user_id": user_id,
        "models": models,
        "origins": origin_totals,
        "usage_date": usage_date,
        "generated_at": int(time.time()),
    }


@router.get("/usage/summary/users")
async def get_usage_summary_users(
    query: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    sort_by: str = Query(default="total_tokens"),
    sort_order: str = Query(default="desc"),
    user=Depends(get_admin_user),
):
    valid_sort_columns = {
        "name", "email", "daily_tokens", "total_tokens",
        "request_count", "internal_tokens", "external_tokens",
    }
    if sort_by not in valid_sort_columns:
        sort_by = "total_tokens"
    if sort_order not in ("asc", "desc"):
        sort_order = "desc"

    offset = (page - 1) * limit
    usage_date = get_kst_date_str()

    result = UsageTokens.get_sorted_user_summaries(
        query=query,
        usage_date=usage_date,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
    )

    return {
        "page": page,
        "limit": limit,
        "total": result["total"],
        "usage_date": usage_date,
        "items": result["items"],
    }


@router.get("/usage/summary/{user_id}")
async def get_usage_summary_by_user_id(user_id: str, user=Depends(get_admin_user)):
    target_user = Users.get_user_by_id(user_id)
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    usage_date = get_kst_date_str()
    usage_rows = UsageTokens.get_usage_by_user(user_id, usage_date=usage_date)

    models = []
    for row in usage_rows:
        limit = resolve_effective_limit_for_role(
            role="user", model_id=row.model_id, origin=row.origin
        )
        remaining = None
        if limit is not None and row.total_tokens is not None:
            remaining = max(limit - row.total_tokens, 0)

        models.append(
            {
                "model_id": row.model_id,
                "origin": row.origin,
                "prompt_tokens": row.prompt_tokens or 0,
                "completion_tokens": row.completion_tokens or 0,
                "total_tokens": row.total_tokens or 0,
                "request_count": row.request_count or 0,
                "limit": limit,
                "remaining": remaining,
            }
        )

    return {
        "user_id": user_id,
        "models": models,
        "usage_date": usage_date,
        "generated_at": int(time.time()),
    }


@router.get("/usage/limits")
async def get_usage_limits(user=Depends(get_admin_user)):
    return UsageLimits.get_limits_by_user("role:user")


@router.post("/usage/limits")
async def upsert_usage_limit(form_data: UsageLimitForm, user=Depends(get_admin_user)):
    model_id = form_data.model_id or None
    origin = form_data.origin or None

    if origin is not None and origin not in ["internal", "external"]:
        raise HTTPException(status_code=400, detail="Invalid origin")

    token_limit = form_data.token_limit
    role_user_id = "role:user"

    if token_limit is None or token_limit < 0:
        deleted = UsageLimits.delete_limit(
            user_id=role_user_id, model_id=model_id, origin=origin
        )
        return {"ok": bool(deleted), "deleted": True}

    limit = UsageLimits.upsert_limit(
        UsageLimitForm(
            user_id=role_user_id,
            model_id=model_id,
            origin=origin,
            token_limit=token_limit,
        )
    )
    if not limit:
        raise HTTPException(status_code=500, detail="Failed to save usage limit")

    return {"ok": True, "limit": limit}
