import json
from fastapi import APIRouter, Depends, HTTPException, Response, status, Request
from fastapi.responses import JSONResponse, RedirectResponse

from pydantic import BaseModel
from typing import Optional
import logging
import re

from open_webui.utils.chat import generate_chat_completion
from open_webui.utils.task import (
    title_generation_template,
    follow_up_generation_template,
    query_generation_template,
    image_prompt_generation_template,
    autocomplete_generation_template,
    tags_generation_template,
    emoji_generation_template,
    moa_response_generation_template,
)
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.constants import TASKS

from open_webui.routers.pipelines import process_pipeline_inlet_filter

from open_webui.utils.task import get_task_model_id

from open_webui.config import (
    DEFAULT_TITLE_GENERATION_PROMPT_TEMPLATE,
    DEFAULT_FOLLOW_UP_GENERATION_PROMPT_TEMPLATE,
    DEFAULT_TAGS_GENERATION_PROMPT_TEMPLATE,
    DEFAULT_IMAGE_PROMPT_GENERATION_PROMPT_TEMPLATE,
    DEFAULT_QUERY_GENERATION_PROMPT_TEMPLATE,
    DEFAULT_AUTOCOMPLETE_GENERATION_PROMPT_TEMPLATE,
    DEFAULT_EMOJI_GENERATION_PROMPT_TEMPLATE,
    DEFAULT_MOA_GENERATION_PROMPT_TEMPLATE,
    DEFAULT_VOICE_MODE_PROMPT_TEMPLATE,
)

log = logging.getLogger(__name__)

router = APIRouter()
# 질문 증강

@router.post("/augment-question")
async def augment_question(
    request: Request, form_data: dict, user=Depends(get_verified_user)
):
    """
    질문을 증강하는 API 엔드포인트
    """
    # 모델 가져오기
    models = request.app.state.MODELS

    # get_task_model_id로 적절한 task 모델 해석
    model_id = get_task_model_id(
        form_data.get("model", ""),
        request.app.state.config.TASK_MODEL,
        request.app.state.config.TASK_MODEL_EXTERNAL,
        models,
    )
    if not model_id or model_id not in models:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model not found: {form_data.get('model', '')}",
        )

    # 질문 증강 프롬프트 v2 - 최신 프롬프트 엔지니어링 적용
    template = """
<role>당신은 질문 증강 전문가입니다. 사용자의 단순한 질문을 AI가 최고의 답변을 제공할 수 있도록 풍부하고 구체적인 질문으로 변환합니다.</role>

<strategy>
다음 증강 기법을 질문 특성에 맞게 조합 적용하세요:

1. **맥락 구체화** - 배경, 상황, 대상, 목적을 명시
2. **다각도 확장** - 원인/방법/비교/장단점/사례 등 복수 관점 요청
3. **조건 명시** - 범위, 시간, 수량, 난이도, 형식 등 세부 조건 추가
4. **출력 형식 지정** - 표, 목록, 단계별, 비교표 등 최적 형식 요청
5. **실용성 강화** - 실무 적용 팁, 주의사항, 구체적 사례 요청
</strategy>

<examples>
입력: "전북 날씨"
출력: "전북 지역의 오늘과 이번 주 날씨 전망을 알려주세요. 기온 변화, 강수 확률, 미세먼지 농도를 포함하고, 외출이나 야외 활동 시 참고할 사항도 함께 안내해 주세요."

입력: "파이썬 정렬"
출력: "파이썬에서 사용할 수 있는 주요 정렬 알고리즘(버블, 선택, 퀵, 병합, Tim Sort 등)의 동작 원리와 시간복잡도를 비교하고, 각각의 코드 예제와 실무에서 어떤 상황에 적합한지 설명해 주세요. 파이썬 내장 sorted() 함수의 활용법도 포함해 주세요."

입력: "보고서 작성법"
출력: "직장에서 상사에게 보고할 업무 보고서를 효과적으로 작성하는 방법을 알려주세요. 보고서의 핵심 구성 요소(제목, 요약, 본문, 결론), 논리적 구조화 방법, 흔한 실수와 개선 팁을 설명하고, 바로 활용할 수 있는 간단한 템플릿 예시도 제시해 주세요."

입력: "AI 활용"
출력: "공공기관에서 업무 효율성을 높이기 위해 AI를 활용할 수 있는 구체적인 방안을 분야별(문서 작성, 데이터 분석, 민원 응대, 정책 연구 등)로 정리해 주세요. 각 분야의 추천 도구나 서비스, 도입 시 고려사항, 그리고 국내외 공공기관의 성공 사례도 함께 알려주세요."

입력: "엑셀 함수"
출력: "실무에서 자주 사용하는 엑셀 핵심 함수 20가지를 카테고리별(조회/참조, 텍스트, 날짜, 통계, 논리)로 정리하고, 각 함수의 사용법과 실제 업무 활용 예시를 표 형식으로 설명해 주세요. 초보자가 특히 알아두면 유용한 함수 조합도 추천해 주세요."
</examples>

<rules>
- 증강된 질문만 출력하세요. 설명, 접두사("보완된 질문:"), 인사말, 따옴표 등은 절대 포함하지 마세요.
- 원래 질문의 핵심 의도를 반드시 유지하되, 더 풍부하고 구체적으로 확장하세요.
- 존댓말 구어체("~해 주세요", "~알려주세요")로 자연스럽게 2~4문장으로 작성하세요.
- 이미 충분히 구체적인 질문은 과도하게 변형하지 말고 약간의 보완만 하세요.
</rules>

입력 질문: {question}
"""
    content = template.format(question=form_data["question"])

    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
        "metadata": {
            **(request.state.metadata if hasattr(request.state, "metadata") else {}),
            "chat_id": form_data.get("chat_id", None),
            "task": "question_augmentation",
            "task_body": form_data,
        },
    }

    try:
        payload = await process_pipeline_inlet_filter(request, payload, user, models)
    except Exception as e:
        raise e

    try:
        response = await generate_chat_completion(request, form_data=payload, user=user)

        #  단순 텍스트 응답 처리 (JSON 아님)
        if isinstance(response, dict) and "choices" in response:
            content = response["choices"][0]["message"]["content"].strip()
            # reasoning model의 <details> 태그 제거
            content = re.sub(r'<details>.*?</details>', '', content, flags=re.DOTALL).strip()
            return {
                "augmented_question": content
            }
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to generate augmented question"
            )
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": str(e)},
        )

##################################
#
# Task Endpoints
#
##################################


class ActiveChatsForm(BaseModel):
    chat_ids: list[str]


@router.post("/active/chats")
async def check_active_chats(
    request: Request, form_data: ActiveChatsForm, user=Depends(get_verified_user)
):
    """Check which chat IDs have active tasks."""
    from open_webui.tasks import get_active_chat_ids

    active = await get_active_chat_ids(request.app.state.redis, form_data.chat_ids)
    return {"active_chat_ids": active}


@router.get("/config")
async def get_task_config(request: Request, user=Depends(get_verified_user)):
    return {
        "TASK_MODEL": request.app.state.config.TASK_MODEL,
        "TASK_MODEL_EXTERNAL": request.app.state.config.TASK_MODEL_EXTERNAL,
        "TITLE_GENERATION_PROMPT_TEMPLATE": request.app.state.config.TITLE_GENERATION_PROMPT_TEMPLATE,
        "IMAGE_PROMPT_GENERATION_PROMPT_TEMPLATE": request.app.state.config.IMAGE_PROMPT_GENERATION_PROMPT_TEMPLATE,
        "ENABLE_AUTOCOMPLETE_GENERATION": request.app.state.config.ENABLE_AUTOCOMPLETE_GENERATION,
        "AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH": request.app.state.config.AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH,
        "TAGS_GENERATION_PROMPT_TEMPLATE": request.app.state.config.TAGS_GENERATION_PROMPT_TEMPLATE,
        "FOLLOW_UP_GENERATION_PROMPT_TEMPLATE": request.app.state.config.FOLLOW_UP_GENERATION_PROMPT_TEMPLATE,
        "ENABLE_FOLLOW_UP_GENERATION": request.app.state.config.ENABLE_FOLLOW_UP_GENERATION,
        "ENABLE_TAGS_GENERATION": request.app.state.config.ENABLE_TAGS_GENERATION,
        "ENABLE_TITLE_GENERATION": request.app.state.config.ENABLE_TITLE_GENERATION,
        "ENABLE_SEARCH_QUERY_GENERATION": request.app.state.config.ENABLE_SEARCH_QUERY_GENERATION,
        "ENABLE_RETRIEVAL_QUERY_GENERATION": request.app.state.config.ENABLE_RETRIEVAL_QUERY_GENERATION,
        "QUERY_GENERATION_PROMPT_TEMPLATE": request.app.state.config.QUERY_GENERATION_PROMPT_TEMPLATE,
        "TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE": request.app.state.config.TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE,
        "VOICE_MODE_PROMPT_TEMPLATE": request.app.state.config.VOICE_MODE_PROMPT_TEMPLATE,
    }


class TaskConfigForm(BaseModel):
    TASK_MODEL: Optional[str]
    TASK_MODEL_EXTERNAL: Optional[str]
    ENABLE_TITLE_GENERATION: bool
    TITLE_GENERATION_PROMPT_TEMPLATE: str
    IMAGE_PROMPT_GENERATION_PROMPT_TEMPLATE: str
    ENABLE_AUTOCOMPLETE_GENERATION: bool
    AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH: int
    TAGS_GENERATION_PROMPT_TEMPLATE: str
    FOLLOW_UP_GENERATION_PROMPT_TEMPLATE: str
    ENABLE_FOLLOW_UP_GENERATION: bool
    ENABLE_TAGS_GENERATION: bool
    ENABLE_SEARCH_QUERY_GENERATION: bool
    ENABLE_RETRIEVAL_QUERY_GENERATION: bool
    QUERY_GENERATION_PROMPT_TEMPLATE: str
    TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE: str
    VOICE_MODE_PROMPT_TEMPLATE: Optional[str]


@router.post("/config/update")
async def update_task_config(
    request: Request, form_data: TaskConfigForm, user=Depends(get_admin_user)
):
    request.app.state.config.TASK_MODEL = form_data.TASK_MODEL
    request.app.state.config.TASK_MODEL_EXTERNAL = form_data.TASK_MODEL_EXTERNAL
    request.app.state.config.ENABLE_TITLE_GENERATION = form_data.ENABLE_TITLE_GENERATION
    request.app.state.config.TITLE_GENERATION_PROMPT_TEMPLATE = (
        form_data.TITLE_GENERATION_PROMPT_TEMPLATE
    )

    request.app.state.config.ENABLE_FOLLOW_UP_GENERATION = (
        form_data.ENABLE_FOLLOW_UP_GENERATION
    )
    request.app.state.config.FOLLOW_UP_GENERATION_PROMPT_TEMPLATE = (
        form_data.FOLLOW_UP_GENERATION_PROMPT_TEMPLATE
    )

    request.app.state.config.IMAGE_PROMPT_GENERATION_PROMPT_TEMPLATE = (
        form_data.IMAGE_PROMPT_GENERATION_PROMPT_TEMPLATE
    )

    request.app.state.config.ENABLE_AUTOCOMPLETE_GENERATION = (
        form_data.ENABLE_AUTOCOMPLETE_GENERATION
    )
    request.app.state.config.AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH = (
        form_data.AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH
    )

    request.app.state.config.TAGS_GENERATION_PROMPT_TEMPLATE = (
        form_data.TAGS_GENERATION_PROMPT_TEMPLATE
    )
    request.app.state.config.ENABLE_TAGS_GENERATION = form_data.ENABLE_TAGS_GENERATION
    request.app.state.config.ENABLE_SEARCH_QUERY_GENERATION = (
        form_data.ENABLE_SEARCH_QUERY_GENERATION
    )
    request.app.state.config.ENABLE_RETRIEVAL_QUERY_GENERATION = (
        form_data.ENABLE_RETRIEVAL_QUERY_GENERATION
    )

    request.app.state.config.QUERY_GENERATION_PROMPT_TEMPLATE = (
        form_data.QUERY_GENERATION_PROMPT_TEMPLATE
    )
    request.app.state.config.TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE = (
        form_data.TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE
    )

    request.app.state.config.VOICE_MODE_PROMPT_TEMPLATE = (
        form_data.VOICE_MODE_PROMPT_TEMPLATE
    )

    return {
        "TASK_MODEL": request.app.state.config.TASK_MODEL,
        "TASK_MODEL_EXTERNAL": request.app.state.config.TASK_MODEL_EXTERNAL,
        "ENABLE_TITLE_GENERATION": request.app.state.config.ENABLE_TITLE_GENERATION,
        "TITLE_GENERATION_PROMPT_TEMPLATE": request.app.state.config.TITLE_GENERATION_PROMPT_TEMPLATE,
        "IMAGE_PROMPT_GENERATION_PROMPT_TEMPLATE": request.app.state.config.IMAGE_PROMPT_GENERATION_PROMPT_TEMPLATE,
        "ENABLE_AUTOCOMPLETE_GENERATION": request.app.state.config.ENABLE_AUTOCOMPLETE_GENERATION,
        "AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH": request.app.state.config.AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH,
        "TAGS_GENERATION_PROMPT_TEMPLATE": request.app.state.config.TAGS_GENERATION_PROMPT_TEMPLATE,
        "ENABLE_TAGS_GENERATION": request.app.state.config.ENABLE_TAGS_GENERATION,
        "ENABLE_FOLLOW_UP_GENERATION": request.app.state.config.ENABLE_FOLLOW_UP_GENERATION,
        "FOLLOW_UP_GENERATION_PROMPT_TEMPLATE": request.app.state.config.FOLLOW_UP_GENERATION_PROMPT_TEMPLATE,
        "ENABLE_SEARCH_QUERY_GENERATION": request.app.state.config.ENABLE_SEARCH_QUERY_GENERATION,
        "ENABLE_RETRIEVAL_QUERY_GENERATION": request.app.state.config.ENABLE_RETRIEVAL_QUERY_GENERATION,
        "QUERY_GENERATION_PROMPT_TEMPLATE": request.app.state.config.QUERY_GENERATION_PROMPT_TEMPLATE,
        "TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE": request.app.state.config.TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE,
        "VOICE_MODE_PROMPT_TEMPLATE": request.app.state.config.VOICE_MODE_PROMPT_TEMPLATE,
    }


@router.post("/title/completions")
async def generate_title(
    request: Request, form_data: dict, user=Depends(get_verified_user)
):

    if not request.app.state.config.ENABLE_TITLE_GENERATION:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"detail": "Title generation is disabled"},
        )

    if getattr(request.state, "direct", False) and hasattr(request.state, "model"):
        models = {
            request.state.model["id"]: request.state.model,
        }
    else:
        models = request.app.state.MODELS

    model_id = form_data["model"]
    if model_id not in models:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Model not found",
        )

    # Check if the user has a custom task model
    # If the user has a custom task model, use that model
    task_model_id = get_task_model_id(
        model_id,
        request.app.state.config.TASK_MODEL,
        request.app.state.config.TASK_MODEL_EXTERNAL,
        models,
    )

    log.debug(
        f"generating chat title using model {task_model_id} for user {user.email} "
    )

    if request.app.state.config.TITLE_GENERATION_PROMPT_TEMPLATE != "":
        template = request.app.state.config.TITLE_GENERATION_PROMPT_TEMPLATE
    else:
        template = DEFAULT_TITLE_GENERATION_PROMPT_TEMPLATE

    content = title_generation_template(template, form_data["messages"], user)

    max_tokens = (
        models[task_model_id].get("info", {}).get("params", {}).get("max_tokens", 1000)
    )

    payload = {
        "model": task_model_id,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
        **(
            {"max_tokens": max_tokens}
            if models[task_model_id].get("owned_by") == "ollama"
            else {
                "max_completion_tokens": max_tokens,
            }
        ),
        "metadata": {
            **(request.state.metadata if hasattr(request.state, "metadata") else {}),
            "task": str(TASKS.TITLE_GENERATION),
            "task_body": form_data,
            "chat_id": form_data.get("chat_id", None),
        },
    }

    # Process the payload through the pipeline
    try:
        payload = await process_pipeline_inlet_filter(request, payload, user, models)
    except Exception as e:
        raise e

    try:
        return await generate_chat_completion(request, form_data=payload, user=user)
    except Exception as e:
        log.error("Exception occurred", exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "An internal error has occurred."},
        )


@router.post("/follow_up/completions")
async def generate_follow_ups(
    request: Request, form_data: dict, user=Depends(get_verified_user)
):

    if not request.app.state.config.ENABLE_FOLLOW_UP_GENERATION:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"detail": "Follow-up generation is disabled"},
        )

    if getattr(request.state, "direct", False) and hasattr(request.state, "model"):
        models = {
            request.state.model["id"]: request.state.model,
        }
    else:
        models = request.app.state.MODELS

    model_id = form_data["model"]
    if model_id not in models:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Model not found",
        )

    # Check if the user has a custom task model
    # If the user has a custom task model, use that model
    task_model_id = get_task_model_id(
        model_id,
        request.app.state.config.TASK_MODEL,
        request.app.state.config.TASK_MODEL_EXTERNAL,
        models,
    )

    log.debug(
        f"generating chat title using model {task_model_id} for user {user.email} "
    )

    if request.app.state.config.FOLLOW_UP_GENERATION_PROMPT_TEMPLATE != "":
        template = request.app.state.config.FOLLOW_UP_GENERATION_PROMPT_TEMPLATE
    else:
        template = DEFAULT_FOLLOW_UP_GENERATION_PROMPT_TEMPLATE

    content = follow_up_generation_template(template, form_data["messages"], user)

    payload = {
        "model": task_model_id,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
        "metadata": {
            **(request.state.metadata if hasattr(request.state, "metadata") else {}),
            "task": str(TASKS.FOLLOW_UP_GENERATION),
            "task_body": form_data,
            "chat_id": form_data.get("chat_id", None),
        },
    }

    # Process the payload through the pipeline
    try:
        payload = await process_pipeline_inlet_filter(request, payload, user, models)
    except Exception as e:
        raise e

    try:
        return await generate_chat_completion(request, form_data=payload, user=user)
    except Exception as e:
        log.error("Exception occurred", exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "An internal error has occurred."},
        )


@router.post("/tags/completions")
async def generate_chat_tags(
    request: Request, form_data: dict, user=Depends(get_verified_user)
):

    if not request.app.state.config.ENABLE_TAGS_GENERATION:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"detail": "Tags generation is disabled"},
        )

    if getattr(request.state, "direct", False) and hasattr(request.state, "model"):
        models = {
            request.state.model["id"]: request.state.model,
        }
    else:
        models = request.app.state.MODELS

    model_id = form_data["model"]
    if model_id not in models:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Model not found",
        )

    # Check if the user has a custom task model
    # If the user has a custom task model, use that model
    task_model_id = get_task_model_id(
        model_id,
        request.app.state.config.TASK_MODEL,
        request.app.state.config.TASK_MODEL_EXTERNAL,
        models,
    )

    log.debug(
        f"generating chat tags using model {task_model_id} for user {user.email} "
    )

    if request.app.state.config.TAGS_GENERATION_PROMPT_TEMPLATE != "":
        template = request.app.state.config.TAGS_GENERATION_PROMPT_TEMPLATE
    else:
        template = DEFAULT_TAGS_GENERATION_PROMPT_TEMPLATE

    content = tags_generation_template(template, form_data["messages"], user)

    payload = {
        "model": task_model_id,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
        "metadata": {
            **(request.state.metadata if hasattr(request.state, "metadata") else {}),
            "task": str(TASKS.TAGS_GENERATION),
            "task_body": form_data,
            "chat_id": form_data.get("chat_id", None),
        },
    }

    # Process the payload through the pipeline
    try:
        payload = await process_pipeline_inlet_filter(request, payload, user, models)
    except Exception as e:
        raise e

    try:
        return await generate_chat_completion(request, form_data=payload, user=user)
    except Exception as e:
        log.error(f"Error generating chat completion: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "An internal error has occurred."},
        )


@router.post("/image_prompt/completions")
async def generate_image_prompt(
    request: Request, form_data: dict, user=Depends(get_verified_user)
):
    if getattr(request.state, "direct", False) and hasattr(request.state, "model"):
        models = {
            request.state.model["id"]: request.state.model,
        }
    else:
        models = request.app.state.MODELS

    model_id = form_data["model"]
    if model_id not in models:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Model not found",
        )

    # Check if the user has a custom task model
    # If the user has a custom task model, use that model
    task_model_id = get_task_model_id(
        model_id,
        request.app.state.config.TASK_MODEL,
        request.app.state.config.TASK_MODEL_EXTERNAL,
        models,
    )

    log.debug(
        f"generating image prompt using model {task_model_id} for user {user.email} "
    )

    if request.app.state.config.IMAGE_PROMPT_GENERATION_PROMPT_TEMPLATE != "":
        template = request.app.state.config.IMAGE_PROMPT_GENERATION_PROMPT_TEMPLATE
    else:
        template = DEFAULT_IMAGE_PROMPT_GENERATION_PROMPT_TEMPLATE

    content = image_prompt_generation_template(template, form_data["messages"], user)

    payload = {
        "model": task_model_id,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
        "metadata": {
            **(request.state.metadata if hasattr(request.state, "metadata") else {}),
            "task": str(TASKS.IMAGE_PROMPT_GENERATION),
            "task_body": form_data,
            "chat_id": form_data.get("chat_id", None),
        },
    }

    # Process the payload through the pipeline
    try:
        payload = await process_pipeline_inlet_filter(request, payload, user, models)
    except Exception as e:
        raise e

    # 사용자 메시지에서 한국어 텍스트 추출 (이미지 오버레이용)
    _korean_overlay = ""
    for _msg in reversed(form_data.get("messages", [])):
        if _msg.get("role") == "user":
            _mc = _msg.get("content", "")
            if isinstance(_mc, str) and re.search(r'[\uac00-\ud7af]', _mc):
                _korean_overlay = _mc.strip()
                break

    try:
        response = await generate_chat_completion(request, form_data=payload, user=user)

        # LLM 응답에 |||한국어 강제 주입 (ComfyUI PromptSplitter용)
        if _korean_overlay:
            try:
                if isinstance(response, dict):
                    _choices = response.get("choices", [])
                    if _choices:
                        _content = _choices[0].get("message", {}).get("content", "")
                        if "|||" not in _content:
                            try:
                                _parsed = json.loads(_content)
                                if isinstance(_parsed, dict) and "prompt" in _parsed:
                                    _parsed["prompt"] += f"|||{_korean_overlay}"
                                    _choices[0]["message"]["content"] = json.dumps(_parsed, ensure_ascii=False)
                            except (json.JSONDecodeError, ValueError):
                                _choices[0]["message"]["content"] = _content.rstrip() + f"|||{_korean_overlay}"
            except Exception:
                pass

        return response
    except Exception as e:
        log.error("Exception occurred", exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "An internal error has occurred."},
        )


@router.post("/queries/completions")
async def generate_queries(
    request: Request, form_data: dict, user=Depends(get_verified_user)
):

    type = form_data.get("type")
    if type == "web_search":
        if not request.app.state.config.ENABLE_SEARCH_QUERY_GENERATION:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Search query generation is disabled",
            )
    elif type == "retrieval":
        if not request.app.state.config.ENABLE_RETRIEVAL_QUERY_GENERATION:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Query generation is disabled",
            )

    if getattr(request.state, "cached_queries", None):
        log.info(f"Reusing cached queries: {request.state.cached_queries}")
        return request.state.cached_queries

    if getattr(request.state, "direct", False) and hasattr(request.state, "model"):
        models = {
            request.state.model["id"]: request.state.model,
        }
    else:
        models = request.app.state.MODELS

    model_id = form_data["model"]
    if model_id not in models:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Model not found",
        )

    # Check if the user has a custom task model
    # If the user has a custom task model, use that model
    task_model_id = get_task_model_id(
        model_id,
        request.app.state.config.TASK_MODEL,
        request.app.state.config.TASK_MODEL_EXTERNAL,
        models,
    )

    log.debug(
        f"generating {type} queries using model {task_model_id} for user {user.email}"
    )

    if (request.app.state.config.QUERY_GENERATION_PROMPT_TEMPLATE).strip() != "":
        template = request.app.state.config.QUERY_GENERATION_PROMPT_TEMPLATE
    else:
        template = DEFAULT_QUERY_GENERATION_PROMPT_TEMPLATE

    content = query_generation_template(template, form_data["messages"], user)

    payload = {
        "model": task_model_id,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
        "metadata": {
            **(request.state.metadata if hasattr(request.state, "metadata") else {}),
            "task": str(TASKS.QUERY_GENERATION),
            "task_body": form_data,
            "chat_id": form_data.get("chat_id", None),
        },
    }

    # Process the payload through the pipeline
    try:
        payload = await process_pipeline_inlet_filter(request, payload, user, models)
    except Exception as e:
        raise e

    try:
        return await generate_chat_completion(request, form_data=payload, user=user)
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": str(e)},
        )


@router.post("/auto/completions")
async def generate_autocompletion(
    request: Request, form_data: dict, user=Depends(get_verified_user)
):
    if not request.app.state.config.ENABLE_AUTOCOMPLETE_GENERATION:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Autocompletion generation is disabled",
        )

    type = form_data.get("type")
    prompt = form_data.get("prompt")
    messages = form_data.get("messages")

    if request.app.state.config.AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH > 0:
        if (
            len(prompt)
            > request.app.state.config.AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Input prompt exceeds maximum length of {request.app.state.config.AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH}",
            )

    if getattr(request.state, "direct", False) and hasattr(request.state, "model"):
        models = {
            request.state.model["id"]: request.state.model,
        }
    else:
        models = request.app.state.MODELS

    model_id = form_data["model"]
    if model_id not in models:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Model not found",
        )

    # Check if the user has a custom task model
    # If the user has a custom task model, use that model
    task_model_id = get_task_model_id(
        model_id,
        request.app.state.config.TASK_MODEL,
        request.app.state.config.TASK_MODEL_EXTERNAL,
        models,
    )

    log.debug(
        f"generating autocompletion using model {task_model_id} for user {user.email}"
    )

    if (request.app.state.config.AUTOCOMPLETE_GENERATION_PROMPT_TEMPLATE).strip() != "":
        template = request.app.state.config.AUTOCOMPLETE_GENERATION_PROMPT_TEMPLATE
    else:
        template = DEFAULT_AUTOCOMPLETE_GENERATION_PROMPT_TEMPLATE

    content = autocomplete_generation_template(template, prompt, messages, type, user)

    payload = {
        "model": task_model_id,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
        "metadata": {
            **(request.state.metadata if hasattr(request.state, "metadata") else {}),
            "task": str(TASKS.AUTOCOMPLETE_GENERATION),
            "task_body": form_data,
            "chat_id": form_data.get("chat_id", None),
        },
    }

    # Process the payload through the pipeline
    try:
        payload = await process_pipeline_inlet_filter(request, payload, user, models)
    except Exception as e:
        raise e

    try:
        return await generate_chat_completion(request, form_data=payload, user=user)
    except Exception as e:
        log.error(f"Error generating chat completion: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "An internal error has occurred."},
        )


@router.post("/emoji/completions")
async def generate_emoji(
    request: Request, form_data: dict, user=Depends(get_verified_user)
):

    if getattr(request.state, "direct", False) and hasattr(request.state, "model"):
        models = {
            request.state.model["id"]: request.state.model,
        }
    else:
        models = request.app.state.MODELS

    model_id = form_data["model"]
    if model_id not in models:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Model not found",
        )

    # Check if the user has a custom task model
    # If the user has a custom task model, use that model
    task_model_id = get_task_model_id(
        model_id,
        request.app.state.config.TASK_MODEL,
        request.app.state.config.TASK_MODEL_EXTERNAL,
        models,
    )

    log.debug(f"generating emoji using model {task_model_id} for user {user.email} ")

    template = DEFAULT_EMOJI_GENERATION_PROMPT_TEMPLATE

    content = emoji_generation_template(template, form_data["prompt"], user)

    payload = {
        "model": task_model_id,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
        **(
            {"max_tokens": 4}
            if models[task_model_id].get("owned_by") == "ollama"
            else {
                "max_completion_tokens": 4,
            }
        ),
        "metadata": {
            **(request.state.metadata if hasattr(request.state, "metadata") else {}),
            "task": str(TASKS.EMOJI_GENERATION),
            "task_body": form_data,
            "chat_id": form_data.get("chat_id", None),
        },
    }

    # Process the payload through the pipeline
    try:
        payload = await process_pipeline_inlet_filter(request, payload, user, models)
    except Exception as e:
        raise e

    try:
        return await generate_chat_completion(request, form_data=payload, user=user)
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": str(e)},
        )


@router.post("/moa/completions")
async def generate_moa_response(
    request: Request, form_data: dict, user=Depends(get_verified_user)
):

    if getattr(request.state, "direct", False) and hasattr(request.state, "model"):
        models = {
            request.state.model["id"]: request.state.model,
        }
    else:
        models = request.app.state.MODELS

    model_id = form_data["model"]

    if model_id not in models:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Model not found",
        )

    template = DEFAULT_MOA_GENERATION_PROMPT_TEMPLATE

    content = moa_response_generation_template(
        template,
        form_data["prompt"],
        form_data["responses"],
    )

    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": content}],
        "stream": form_data.get("stream", False),
        "metadata": {
            **(request.state.metadata if hasattr(request.state, "metadata") else {}),
            "chat_id": form_data.get("chat_id", None),
            "task": str(TASKS.MOA_RESPONSE_GENERATION),
            "task_body": form_data,
        },
    }

    # Process the payload through the pipeline
    try:
        payload = await process_pipeline_inlet_filter(request, payload, user, models)
    except Exception as e:
        raise e

    try:
        return await generate_chat_completion(request, form_data=payload, user=user)
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": str(e)},
        )
