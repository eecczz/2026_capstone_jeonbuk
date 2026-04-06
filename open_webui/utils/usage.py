import logging
from typing import Optional

from fastapi import Request

from open_webui.env import GLOBAL_LOG_LEVEL
from open_webui.models.usage import UsageEvents
from open_webui.models.usage_tokens import UsageTokens
from open_webui.models.users import Users
from open_webui.utils.auth import decode_token
from open_webui.utils.misc import get_messages_content
from open_webui.utils.time_utils import get_kst_date_str

log = logging.getLogger(__name__)
log.setLevel(GLOBAL_LOG_LEVEL)


def _safe_int(value: Optional[object]) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def get_user_id_from_request(request: Request) -> Optional[str]:
    token = None
    if hasattr(request.state, "token") and request.state.token:
        token = request.state.token.credentials

    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[len("Bearer ") :]

    if not token and request.cookies.get("token"):
        token = request.cookies.get("token")

    if not token:
        return None

    if token.startswith("sk-"):
        user = Users.get_user_by_api_key(token)
        return user.id if user else None

    try:
        data = decode_token(token)
        if data and "id" in data:
            return data.get("id")
    except Exception:
        return None

    return None


def record_usage_event(
    *,
    user_id: str,
    kind: str,
    category: str,
    endpoint: str,
    provider: Optional[str] = None,
    model_id: Optional[str] = None,
    origin: Optional[str] = None,
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
) -> None:
    UsageEvents.insert_new_usage_event(
        user_id=user_id,
        kind=kind,
        category=category,
        endpoint=endpoint,
        provider=provider,
        model_id=model_id,
        request_count=request_count,
        prompt_tokens=_safe_int(prompt_tokens),
        completion_tokens=_safe_int(completion_tokens),
        total_tokens=_safe_int(total_tokens),
        input_bytes=_safe_int(input_bytes),
        output_bytes=_safe_int(output_bytes),
        status_code=_safe_int(status_code),
        latency_ms=_safe_int(latency_ms),
        chat_id=chat_id,
        message_id=message_id,
    )

    if origin and model_id and (prompt_tokens is not None or completion_tokens is not None or total_tokens is not None):
        UsageTokens.upsert_usage_token(
            user_id=user_id,
            model_id=model_id,
            origin=origin,
            usage_date=get_kst_date_str(),
            request_count=request_count,
            prompt_tokens=_safe_int(prompt_tokens),
            completion_tokens=_safe_int(completion_tokens),
            total_tokens=_safe_int(total_tokens),
        )


def get_origin_from_model(model: Optional[dict]) -> Optional[str]:
    if not model:
        return None
    if model.get("pipe"):
        return "internal"
    connection_type = model.get("connection_type")
    if connection_type == "local":
        return "internal"
    if connection_type == "external":
        return "external"
    return None


def estimate_tokens_for_text(text: str, encoding_name: str) -> Optional[int]:
    if not text:
        return None
    try:
        import tiktoken

        encoding = tiktoken.get_encoding(str(encoding_name))
        return len(encoding.encode(text))
    except Exception:
        return None


def estimate_tokens_for_messages(
    messages: list[dict], encoding_name: str
) -> Optional[int]:
    if not messages:
        return None
    return estimate_tokens_for_text(get_messages_content(messages), encoding_name)


def sum_token_counts(
    prompt_tokens: Optional[int], completion_tokens: Optional[int]
) -> Optional[int]:
    if prompt_tokens is None and completion_tokens is None:
        return None
    return (prompt_tokens or 0) + (completion_tokens or 0)
