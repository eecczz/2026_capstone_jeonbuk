import random
import logging
import sys

from fastapi import Request
from open_webui.models.users import UserModel
from open_webui.models.models import Models
from open_webui.utils.models import check_model_access
from open_webui.env import GLOBAL_LOG_LEVEL, BYPASS_MODEL_ACCESS_CONTROL

from open_webui.routers.openai import embeddings as openai_embeddings
from open_webui.routers.ollama import (
    embed as ollama_embed,
    GenerateEmbedForm,
)

from open_webui.utils.payload import convert_embed_payload_openai_to_ollama
from open_webui.utils.response import convert_embedding_response_ollama_to_openai
from open_webui.utils.usage import (
    estimate_tokens_for_text,
    get_origin_from_model,
    record_usage_event,
    sum_token_counts,
)
from open_webui.utils.usage_limits import check_usage_limit

logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL)
log = logging.getLogger(__name__)


async def generate_embeddings(
    request: Request,
    form_data: dict,
    user: UserModel,
    bypass_filter: bool = False,
):
    """
    Dispatch and handle embeddings generation based on the model type (OpenAI, Ollama).

    Args:
        request (Request): The FastAPI request context.
        form_data (dict): The input data sent to the endpoint.
        user (UserModel): The authenticated user.
        bypass_filter (bool): If True, disables access filtering (default False).

    Returns:
        dict: The embeddings response, following OpenAI API compatibility.
    """
    if BYPASS_MODEL_ACCESS_CONTROL:
        bypass_filter = True

    # Attach extra metadata from request.state if present
    if hasattr(request.state, "metadata"):
        if "metadata" not in form_data:
            form_data["metadata"] = request.state.metadata
        else:
            form_data["metadata"] = {
                **form_data["metadata"],
                **request.state.metadata,
            }

    # If "direct" flag present, use only that model
    if getattr(request.state, "direct", False) and hasattr(request.state, "model"):
        models = {
            request.state.model["id"]: request.state.model,
        }
    else:
        models = request.app.state.MODELS

    model_id = form_data.get("model")
    if model_id not in models:
        raise Exception("Model not found")
    model = models[model_id]
    origin = get_origin_from_model(model)

    encoding_name = request.app.state.config.TIKTOKEN_ENCODING_NAME
    input_data = form_data.get("input", "")
    if isinstance(input_data, list):
        input_text = "\n".join([str(item) for item in input_data])
    else:
        input_text = str(input_data)
    prompt_tokens = estimate_tokens_for_text(input_text, encoding_name)
    check_usage_limit(
        user_id=user.id,
        role=user.role,
        model_id=model.get("id"),
        origin=origin,
        estimated_tokens=prompt_tokens,
    )

    # Access filtering
    if not getattr(request.state, "direct", False):
        if not bypass_filter and user.role == "user":
            check_model_access(user, model)

    # Ollama backend — use /api/embed which supports batch input natively
    if model.get("owned_by") == "ollama":
        ollama_payload = convert_embed_payload_openai_to_ollama(form_data)
        response = await ollama_embed(
            request=request,
            form_data=GenerateEmbedForm(**ollama_payload),
            user=user,
        )
        response = convert_embedding_response_ollama_to_openai(response)
        usage = response.get("usage", {}) if isinstance(response, dict) else {}
        if usage:
            metadata = getattr(request.state, "metadata", {}) or {}
            record_usage_event(
                user_id=user.id,
                kind="external",
                category="embeddings",
                endpoint="/embeddings",
                provider=model.get("owned_by"),
                model_id=model.get("id"),
                origin=origin,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
                chat_id=metadata.get("chat_id"),
                message_id=metadata.get("message_id"),
            )
        else:
            metadata = getattr(request.state, "metadata", {}) or {}
            encoding_name = request.app.state.config.TIKTOKEN_ENCODING_NAME
            input_data = form_data.get("input", "")
            if isinstance(input_data, list):
                input_text = "\n".join([str(item) for item in input_data])
            else:
                input_text = str(input_data)
            prompt_tokens = estimate_tokens_for_text(input_text, encoding_name)
            total_tokens = sum_token_counts(prompt_tokens, None)
            if prompt_tokens is not None:
                record_usage_event(
                    user_id=user.id,
                    kind="external",
                    category="embeddings",
                    endpoint="/embeddings",
                    provider=model.get("owned_by"),
                    model_id=model.get("id"),
                    origin=origin,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=None,
                    total_tokens=total_tokens,
                    chat_id=metadata.get("chat_id"),
                    message_id=metadata.get("message_id"),
                )
        return response

    # Default: OpenAI or compatible backend
    response = await openai_embeddings(
        request=request,
        form_data=form_data,
        user=user,
    )
    usage = response.get("usage", {}) if isinstance(response, dict) else {}
    if usage:
        metadata = getattr(request.state, "metadata", {}) or {}
        record_usage_event(
            user_id=user.id,
            kind="external",
            category="embeddings",
            endpoint="/embeddings",
            provider=model.get("owned_by"),
            model_id=model.get("id"),
            origin=origin,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            chat_id=metadata.get("chat_id"),
            message_id=metadata.get("message_id"),
        )
    else:
        metadata = getattr(request.state, "metadata", {}) or {}
        encoding_name = request.app.state.config.TIKTOKEN_ENCODING_NAME
        input_data = form_data.get("input", "")
        if isinstance(input_data, list):
            input_text = "\n".join([str(item) for item in input_data])
        else:
            input_text = str(input_data)
        prompt_tokens = estimate_tokens_for_text(input_text, encoding_name)
        total_tokens = sum_token_counts(prompt_tokens, None)
        if prompt_tokens is not None:
            record_usage_event(
                user_id=user.id,
                kind="external",
                category="embeddings",
                endpoint="/embeddings",
                provider=model.get("owned_by"),
                model_id=model.get("id"),
                origin=origin,
                prompt_tokens=prompt_tokens,
                completion_tokens=None,
                total_tokens=total_tokens,
                chat_id=metadata.get("chat_id"),
                message_id=metadata.get("message_id"),
            )
    return response
