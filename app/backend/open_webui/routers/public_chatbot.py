"""
전북도청 대도민 공개 AI 안내 챗봇 라우터.

기존 Open WebUI 인프라(RAG, 멀티턴, Function Calling)를 재사용하여
인증 없는 공개 엔드포인트로 대도민에게 도청·직속기관 홈페이지 내용을 안내합니다.

Phase 0: 텍스트 Q&A (stateless 멀티턴 — 클라이언트가 messages 배열로 히스토리 관리)
Phase 2: 음성 I/O (voice-chat 엔드포인트 추가)
"""

import json
import logging
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse, StreamingResponse

# (참고) 음성 응답 출력은 Phase 2a에서 브라우저 speechSynthesis API 활용,
# Phase 2b에서 Qwen3-TTS 서버 사이드 합성으로 전환 예정.

from open_webui.env import GLOBAL_LOG_LEVEL
from open_webui.models.users import UserModel
from open_webui.models.models import Models
from open_webui.utils.chat import generate_chat_completion
from open_webui.utils.middleware import process_chat_payload
from open_webui.utils.models import get_all_models

logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL)
log = logging.getLogger(__name__)

router = APIRouter()


####################
# GraphRAG 헬퍼
####################


async def _build_graph_context(request: Request, query: str) -> str:
    """
    쿼리로부터 graph_bonus_chunks 를 얻고, 그 URL들에 해당하는 vector DB 청크 텍스트를
    모아 system prompt 에 주입할 문자열을 만든다.

    성능 최적화:
    - URL 당 대표 청크 1개만 사용
    - 상위 3개 URL만 (기존 6개에서 축소)
    - Qdrant query 를 asyncio.gather 로 병렬 실행 (to_thread)
    - 최대 1200자로 절삭
    """
    import asyncio as _aio
    import time as _time
    from open_webui.retrieval.graph.retriever import graph_bonus_chunks
    from open_webui.retrieval.vector.factory import VECTOR_DB_CLIENT

    t0 = _time.perf_counter()
    url_scores = graph_bonus_chunks(query, max_chunks=8)
    t1 = _time.perf_counter()
    if not url_scores:
        return ""

    collection = getattr(
        request.app.state.config, "CRAWLER_COLLECTION_NAME", "jeonbuk_gov"
    )

    top_urls = [
        url
        for url, _ in sorted(
            url_scores.items(), key=lambda kv: kv[1], reverse=True
        )[:3]
    ]

    def _fetch_one(u: str):
        try:
            return VECTOR_DB_CLIENT.query(
                collection_name=collection, filter={"url": u}, limit=1
            )
        except Exception as e:
            log.debug(f"graph context fetch failed for {u}: {e}")
            return None

    results = await _aio.gather(
        *[_aio.to_thread(_fetch_one, u) for u in top_urls]
    )
    t2 = _time.perf_counter()

    lines: list[str] = []
    for url, result in zip(top_urls, results):
        if result is None:
            continue
        docs = (result.documents or [[]])[0] if result.documents else []
        metas = (result.metadatas or [[]])[0] if result.metadatas else []
        if not docs:
            continue
        text = (docs[0] or "")[:350]
        meta = metas[0] if metas else {}
        inst = meta.get("institution") or "-"
        lines.append(f"[{inst}] {text}\n출처: {url}")

    joined = "\n\n".join(lines)[:1200]
    log.info(
        f"graph context timing: bonus={1000 * (t1 - t0):.0f}ms "
        f"fetch={1000 * (t2 - t1):.0f}ms urls={len(top_urls)} chars={len(joined)}"
    )
    return joined


####################
# 공개 가상 사용자 (DB 저장 안 함, 메모리 객체)
####################

def _get_public_user(request: Request) -> UserModel:
    """공개 챗봇용 가상 사용자 객체를 반환.

    DB 사용자를 만들지 않고, generate_chat_completion 호출에 필요한
    최소 필드만 있는 Pydantic 객체를 동적으로 생성한다.
    role='admin'으로 설정하여 모델 접근 제어를 우회한다.
    """
    user_id = getattr(
        request.app.state.config,
        "PUBLIC_CHATBOT_USER_ID",
        "public-chatbot-user",
    )
    return UserModel(
        id=user_id,
        email="public-chatbot@jeonbuk.go.kr",
        name="대도민 공개 챗봇",
        role="admin",  # 모델 접근 제어 우회용 (bypass_filter=True와 병행)
        last_active_at=int(time.time()),
        created_at=int(time.time()),
        updated_at=int(time.time()),
    )


####################
# Rate limiting (IP 기반, Redis INCR + EXPIRE 방식)
####################

# Redis 가 없거나 에러 시 fallback으로 쓰는 인-메모리 저장소.
# 프로덕션 4 workers 환경에서는 워커마다 분리되어 있으므로 Redis 사용 필수.
_fallback_rate_store: dict[str, list[float]] = {}


async def _check_rate_limit(request: Request) -> None:
    """IP당 분당 요청 수 제한.

    Redis 가 있으면 `public_chatbot:rl:{ip}` 키에 INCR + EXPIRE 60 방식으로
    워커 간 정확히 공유. 없으면 프로세스 로컬 인메모리 fallback.
    """
    limit = int(
        getattr(request.app.state.config, "PUBLIC_CHATBOT_RATE_LIMIT_PER_MINUTE", 10)
    )
    if limit <= 0:
        return  # 제한 비활성화

    client_ip = request.client.host if request.client else "unknown"

    redis = getattr(request.app.state, "redis", None)
    if redis is not None:
        try:
            key = f"public_chatbot:rl:{client_ip}"
            # 첫 INCR 이면 1 반환 → 그때 EXPIRE 설정. 60초 슬라이딩 윈도우는 아니지만
            # 고정 윈도우 방식으로도 충분.
            count = await redis.incr(key)
            if count == 1:
                await redis.expire(key, 60)
            if count > limit:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"요청이 너무 많습니다. 분당 {limit}회로 제한됩니다. 잠시 후 다시 시도해 주세요.",
                )
            return
        except HTTPException:
            raise
        except Exception as e:
            log.warning(f"Redis rate limit failed, falling back to in-memory: {e}")
            # fall through to in-memory

    # In-memory fallback (단일 워커 또는 Redis 장애 시)
    now = time.time()
    window_start = now - 60.0
    history = _fallback_rate_store.get(client_ip, [])
    history = [t for t in history if t > window_start]
    if len(history) >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"요청이 너무 많습니다. 분당 {limit}회로 제한됩니다. 잠시 후 다시 시도해 주세요.",
        )
    history.append(now)
    _fallback_rate_store[client_ip] = history


####################
# 요청/응답 스키마
####################


class PublicChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class PublicChatRequest(BaseModel):
    message: str = Field(..., description="도민의 현재 질문")
    history: list[PublicChatMessage] = Field(
        default_factory=list,
        description="이전 대화 히스토리 (클라이언트가 관리). "
        "[{role:'user',content:'...'}, {role:'assistant',content:'...'}] 순서.",
    )
    session_id: Optional[str] = Field(
        None, description="선택적 세션 식별자 (로깅·추적용)"
    )


class PublicChatResponse(BaseModel):
    reply: str
    session_id: str
    sources: list[dict] = Field(default_factory=list)
    model: str


####################
# 날짜 컨텍스트 주입 헬퍼
####################


def _today_context_prefix() -> str:
    """오늘 날짜를 LLM에 주입해 '다음주', '이번달' 같은 상대 시간 표현을 해석하게 한다."""
    now = datetime.now()
    weekday_kr = ["월", "화", "수", "목", "금", "토", "일"][now.weekday()]
    return f"\n\n[오늘 날짜] {now.strftime('%Y년 %m월 %d일')} ({weekday_kr}요일)"


####################
# 메인 챗 엔드포인트
####################


async def _run_public_llm(
    request: Request,
    user: UserModel,
    user_message: str,
    history: list[dict],
    session_id: str,
) -> tuple[str, list[dict], str]:
    """공개 챗봇 LLM 호출 공통 헬퍼.

    핵심: 기존 chat_completion 엔드포인트의 전체 흐름을 최소 재현 —
    process_chat_payload()로 RAG 컨텍스트/도구 주입까지 포함시킨 뒤
    generate_chat_completion()을 호출한다.

    Returns: (reply_text, sources, resolved_model_id)
    """
    # 1. 모델 결정 (래퍼 우선, 없으면 base 폴백)
    model_id = getattr(
        request.app.state.config,
        "PUBLIC_CHATBOT_MODEL_ID",
        "jeonbuk-public-chatbot",
    )

    if not request.app.state.MODELS:
        try:
            await get_all_models(request, user=user)
        except Exception as e:
            log.warning(f"get_all_models failed in public chatbot: {e}")

    if model_id not in (request.app.state.MODELS or {}):
        fallback = getattr(
            request.app.state.config,
            "PUBLIC_CHATBOT_BASE_MODEL",
            "gpt-4o-mini",
        )
        log.warning(
            f"public_chatbot model '{model_id}' not found, falling back to '{fallback}'"
        )
        if fallback not in (request.app.state.MODELS or {}):
            raise HTTPException(
                status_code=500,
                detail=f"LLM 모델을 찾을 수 없습니다: {model_id}, {fallback}",
            )
        model_id = fallback

    model = request.app.state.MODELS[model_id]

    # 2. 시스템 프롬프트 (설정값 + 오늘 날짜)
    system_prompt = getattr(
        request.app.state.config,
        "PUBLIC_CHATBOT_SYSTEM_PROMPT",
        "당신은 전북특별자치도청 대도민 안내 AI입니다.",
    )
    system_prompt = system_prompt + _today_context_prefix()

    # 3. messages 조립 (system → history → 현재 질문)
    #    GraphRAG 증강: feature flag ON이면 쿼리 엔티티로부터 관련 페이지 URL을 찾아
    #    그 페이지의 벡터 청크 텍스트를 system prompt에 별첨으로 주입한다.
    if getattr(
        request.app.state.config, "ENABLE_GRAPH_RAG_RETRIEVAL", False
    ):
        try:
            graph_context = await _build_graph_context(request, user_message)
            if graph_context:
                system_prompt = (
                    system_prompt
                    + "\n\n## 그래프 추론으로 추가 발견된 관련 자료\n"
                    + graph_context
                )
                log.info(
                    f"graph RAG augmented system prompt with "
                    f"{len(graph_context)} chars"
                )
        except Exception as e:
            log.warning(f"graph retrieval augmentation failed: {e}")

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt}
    ]
    for turn in history or []:
        role = turn.get("role") if isinstance(turn, dict) else getattr(turn, "role", None)
        content = (
            turn.get("content") if isinstance(turn, dict) else getattr(turn, "content", None)
        )
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})

    # 4. metadata 조립 (기존 chat_completion 엔드포인트 포맷 참고)
    message_id = str(uuid.uuid4())
    metadata = {
        "user_id": user.id,
        "chat_id": "",  # stateless: 기존 chat 테이블 사용 안 함
        "message_id": message_id,
        "session_id": session_id,
        "parent_message_id": None,
        "parent_message": None,
        "filter_ids": [],
        "tool_ids": None,
        "tool_servers": None,
        "files": None,
        "features": {},
        "variables": {},
        "model": model,
        "direct": False,
        "params": {
            "stream_delta_chunk_size": None,
            "reasoning_tags": None,
            "function_calling": "default",
        },
        "public_chatbot": True,
    }

    # 5. form_data 초기화
    form_data = {
        "model": model_id,
        "messages": messages,
        "stream": False,
        "metadata": metadata,
    }

    # request.state에도 metadata 반영 (generate_chat_completion 내부에서 참조됨)
    try:
        request.state.metadata = metadata
    except Exception:
        pass

    # 6. 핵심 — process_chat_payload로 RAG 컨텍스트/도구 주입
    _t_rag = time.perf_counter()
    try:
        form_data, metadata, events = await process_chat_payload(
            request, form_data, user, metadata, model
        )
    except Exception as e:
        log.exception(f"process_chat_payload failed: {e}")
        log.warning("Falling back to plain LLM call without RAG injection")
    _t_rag_done = time.perf_counter()

    # 7. LLM 호출
    try:
        response = await generate_chat_completion(
            request,
            form_data,
            user=user,
            bypass_filter=True,
        )
    except Exception as e:
        log.exception(f"generate_chat_completion failed: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"챗봇 응답 생성 중 오류: {str(e)}",
        )
    _t_llm_done = time.perf_counter()
    log.info(
        f"public chat timing: rag={1000 * (_t_rag_done - _t_rag):.0f}ms "
        f"llm={1000 * (_t_llm_done - _t_rag_done):.0f}ms"
    )

    # 8. 응답 파싱 (dict / StreamingResponse 둘 다 처리)
    reply_text = ""
    sources: list[dict] = []

    if isinstance(response, StreamingResponse):
        chunks: list[bytes] = []
        async for chunk in response.body_iterator:
            if isinstance(chunk, bytes):
                chunks.append(chunk)
            elif isinstance(chunk, str):
                chunks.append(chunk.encode("utf-8"))
        raw = b"".join(chunks).decode("utf-8", errors="replace")
        for line in raw.splitlines():
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if payload in ("", "[DONE]"):
                continue
            try:
                obj = json.loads(payload)
                delta = obj.get("choices", [{}])[0].get("delta", {}).get("content", "")
                if delta:
                    reply_text += delta
            except Exception:
                continue
    elif isinstance(response, dict):
        try:
            reply_text = response["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            reply_text = json.dumps(response, ensure_ascii=False)
        sources = response.get("sources", []) or []
    else:
        reply_text = str(response)

    return reply_text.strip(), sources, model_id


async def _stream_public_llm(
    request: Request,
    user: UserModel,
    user_message: str,
    history: list[dict],
    session_id: str,
):
    """SSE 스트리밍 버전. _run_public_llm과 같은 흐름이지만 LLM 응답을 토큰 단위로 yield."""
    # 1. 모델 결정
    model_id = getattr(
        request.app.state.config,
        "PUBLIC_CHATBOT_MODEL_ID",
        "jeonbuk-public-chatbot",
    )
    if not request.app.state.MODELS:
        try:
            await get_all_models(request, user=user)
        except Exception as e:
            log.warning(f"get_all_models failed in public chatbot stream: {e}")
    if model_id not in (request.app.state.MODELS or {}):
        fallback = getattr(
            request.app.state.config,
            "PUBLIC_CHATBOT_BASE_MODEL",
            "gpt-4o-mini",
        )
        if fallback not in (request.app.state.MODELS or {}):
            raise HTTPException(
                status_code=500,
                detail=f"LLM 모델을 찾을 수 없습니다: {model_id}, {fallback}",
            )
        model_id = fallback
    model = request.app.state.MODELS[model_id]

    # 2. 시스템 프롬프트 + 그래프 컨텍스트
    system_prompt = getattr(
        request.app.state.config,
        "PUBLIC_CHATBOT_SYSTEM_PROMPT",
        "당신은 전북특별자치도청 대도민 안내 AI입니다.",
    )
    system_prompt = system_prompt + _today_context_prefix()
    if getattr(
        request.app.state.config, "ENABLE_GRAPH_RAG_RETRIEVAL", False
    ):
        try:
            graph_context = await _build_graph_context(request, user_message)
            if graph_context:
                system_prompt = (
                    system_prompt
                    + "\n\n## 그래프 추론으로 추가 발견된 관련 자료\n"
                    + graph_context
                )
        except Exception as e:
            log.warning(f"graph retrieval augmentation failed (stream): {e}")

    # 3. messages
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt}
    ]
    for turn in history or []:
        role = turn.get("role") if isinstance(turn, dict) else getattr(turn, "role", None)
        content = (
            turn.get("content") if isinstance(turn, dict) else getattr(turn, "content", None)
        )
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})

    # 4. metadata + form_data (stream=True)
    message_id = str(uuid.uuid4())
    metadata = {
        "user_id": user.id,
        "chat_id": "",
        "message_id": message_id,
        "session_id": session_id,
        "parent_message_id": None,
        "parent_message": None,
        "filter_ids": [],
        "tool_ids": None,
        "tool_servers": None,
        "files": None,
        "features": {},
        "variables": {},
        "model": model,
        "direct": False,
        "params": {
            "stream_delta_chunk_size": None,
            "reasoning_tags": None,
            "function_calling": "default",
        },
        "public_chatbot": True,
    }
    form_data = {
        "model": model_id,
        "messages": messages,
        "stream": True,
        "metadata": metadata,
    }
    try:
        request.state.metadata = metadata
    except Exception:
        pass

    # 5. RAG 주입 (process_chat_payload, hybrid 비활성 + query gen 비활성 상태)
    try:
        form_data, metadata, _events = await process_chat_payload(
            request, form_data, user, metadata, model
        )
    except Exception as e:
        log.exception(f"process_chat_payload (stream) failed: {e}")

    # 6. LLM 호출 (stream)
    try:
        response = await generate_chat_completion(
            request, form_data, user=user, bypass_filter=True
        )
    except Exception as e:
        log.exception(f"generate_chat_completion (stream) failed: {e}")
        yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
        return

    # 7. 응답 스트림 → 토큰 delta 만 추출해서 yield
    if isinstance(response, StreamingResponse):
        buf = ""
        async for chunk in response.body_iterator:
            if isinstance(chunk, bytes):
                buf += chunk.decode("utf-8", errors="replace")
            elif isinstance(chunk, str):
                buf += chunk
            # SSE line-by-line 파싱
            while "\n\n" in buf:
                event, buf = buf.split("\n\n", 1)
                for line in event.splitlines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:].strip()
                    if payload == "" or payload == "[DONE]":
                        continue
                    try:
                        obj = json.loads(payload)
                        delta = (
                            obj.get("choices", [{}])[0]
                            .get("delta", {})
                            .get("content", "")
                        )
                        if delta:
                            out = json.dumps(
                                {"delta": delta}, ensure_ascii=False
                            )
                            yield f"data: {out}\n\n"
                    except Exception:
                        continue
    elif isinstance(response, dict):
        # non-stream fallback
        try:
            text = response["choices"][0]["message"]["content"] or ""
        except Exception:
            text = ""
        if text:
            yield f"data: {json.dumps({'delta': text}, ensure_ascii=False)}\n\n"

    # 8. 종료 마커
    final = json.dumps(
        {"done": True, "session_id": session_id, "model": model_id},
        ensure_ascii=False,
    )
    yield f"data: {final}\n\n"


@router.post("/chat", response_model=PublicChatResponse)
async def public_chat(request: Request, body: PublicChatRequest):
    """공개 텍스트 챗봇 엔드포인트.

    인증 불필요, IP당 분당 N회 rate limit 적용.
    클라이언트가 `history` 배열로 대화 맥락을 관리하는 stateless 방식.
    """
    if not getattr(request.app.state.config, "ENABLE_PUBLIC_CHATBOT", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="공개 챗봇 서비스가 비활성화되어 있습니다.",
        )

    await _check_rate_limit(request)

    user = _get_public_user(request)
    session_id = body.session_id or str(uuid.uuid4())

    # history(Pydantic 객체 배열)를 dict 배열로 변환
    history_dicts = [
        {"role": h.role, "content": h.content} for h in (body.history or [])
    ]

    reply_text, sources, resolved_model = await _run_public_llm(
        request,
        user,
        body.message,
        history_dicts,
        session_id,
    )

    return PublicChatResponse(
        reply=reply_text,
        session_id=session_id,
        sources=sources,
        model=resolved_model,
    )


@router.post("/chat/stream")
async def public_chat_stream(request: Request, body: PublicChatRequest):
    """공개 챗봇 SSE 스트리밍 엔드포인트.

    응답 형식: text/event-stream
    각 청크: `data: {"delta": "..."}\\n\\n`
    종료:   `data: {"done": true, "session_id": "...", "model": "..."}\\n\\n`

    프론트는 fetch + ReadableStream 으로 받아서 화면에 즉시 누적 표시한다.
    """
    if not getattr(request.app.state.config, "ENABLE_PUBLIC_CHATBOT", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="공개 챗봇 서비스가 비활성화되어 있습니다.",
        )

    await _check_rate_limit(request)

    user = _get_public_user(request)
    session_id = body.session_id or str(uuid.uuid4())
    history_dicts = [
        {"role": h.role, "content": h.content} for h in (body.history or [])
    ]

    async def _gen():
        try:
            async for chunk in _stream_public_llm(
                request, user, body.message, history_dicts, session_id
            ):
                yield chunk
        except HTTPException as e:
            err = json.dumps({"error": str(e.detail)}, ensure_ascii=False)
            yield f"data: {err}\n\n"
        except Exception as e:
            log.exception(f"public_chat_stream failed: {e}")
            err = json.dumps({"error": str(e)}, ensure_ascii=False)
            yield f"data: {err}\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


####################
# 상태 확인 엔드포인트
####################


@router.get("/health")
async def public_chat_health(request: Request):
    """공개 챗봇 서비스 상태 확인 (인증 불필요)."""
    enabled = getattr(request.app.state.config, "ENABLE_PUBLIC_CHATBOT", False)
    model_id = getattr(
        request.app.state.config, "PUBLIC_CHATBOT_MODEL_ID", "jeonbuk-public-chatbot"
    )
    kb_id = getattr(request.app.state.config, "PUBLIC_CHATBOT_KNOWLEDGE_ID", "")
    return {
        "enabled": enabled,
        "model_id": model_id,
        "knowledge_id": kb_id or None,
        "stt_engine": getattr(request.app.state.config, "STT_ENGINE", ""),
        "tts_engine": getattr(request.app.state.config, "TTS_ENGINE", ""),
        "rate_limit_per_minute": getattr(
            request.app.state.config, "PUBLIC_CHATBOT_RATE_LIMIT_PER_MINUTE", 10
        ),
    }


####################
# 음성 챗 엔드포인트 (Phase 2)
####################


async def _run_chat_internal(
    request: Request, message: str, history: list[dict]
) -> tuple[str, str, list[dict]]:
    """voice-chat용 wrapper. _run_public_llm을 호출하고 session_id 생성."""
    user = _get_public_user(request)
    session_id = str(uuid.uuid4())
    reply_text, sources, _model = await _run_public_llm(
        request, user, message, history, session_id
    )
    return reply_text, session_id, sources


@router.post("/voice-chat")
async def public_voice_chat(
    request: Request,
    file: UploadFile = File(..., description="도민의 음성 질문 (webm/wav/mp3)"),
    history_json: Optional[str] = Form(
        None, description="이전 대화 히스토리 (JSON 문자열)"
    ),
):
    """음성 → STT → RAG+LLM → TTS → 음성 응답 (Phase 2).

    요청:
    - multipart/form-data
    - file: 녹음된 오디오 파일
    - history_json: 이전 대화 히스토리 JSON 배열 (선택, 클라이언트가 관리)

    응답:
    - JSON: { reply, question, session_id, sources, audio_url }
    - audio_url은 TTS 결과 오디오를 받을 수 있는 엔드포인트
      (기존 /api/v1/audio/speech 대신, 여기서 직접 생성·캐시 경로를 반환)

    프론트는 이 JSON을 받아서 reply 텍스트를 화면에 표시하고
    audio_url을 <audio src="">로 재생한다.
    """
    if not getattr(request.app.state.config, "ENABLE_PUBLIC_CHATBOT", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="공개 챗봇이 비활성화되어 있습니다.",
        )

    await _check_rate_limit(request)

    # 1. 업로드 오디오 파일을 임시 경로에 저장
    try:
        from open_webui.routers.audio import (
            SPEECH_CACHE_DIR,
            transcribe as audio_transcribe,
        )
    except Exception as e:
        log.exception(f"audio module import failed: {e}")
        raise HTTPException(
            status_code=500,
            detail="음성 모듈 로딩 실패",
        )

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="빈 음성 파일입니다.")

    # 임시 디렉토리에 저장
    suffix = os.path.splitext(file.filename or "")[1] or ".webm"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        tmp.write(contents)
        tmp.flush()
        tmp.close()
        audio_path = tmp.name
    except Exception as e:
        log.exception(f"tempfile write failed: {e}")
        raise HTTPException(status_code=500, detail="음성 파일 저장 실패")

    # 2. STT — 기존 transcribe() 재사용
    user = _get_public_user(request)
    try:
        stt_result = audio_transcribe(
            request, audio_path, metadata=None, user=user
        )
        question_text = (stt_result or {}).get("text", "").strip()
    except Exception as e:
        log.exception(f"STT failed: {e}")
        raise HTTPException(status_code=500, detail=f"STT 오류: {str(e)}")
    finally:
        try:
            os.unlink(audio_path)
        except Exception:
            pass

    if not question_text:
        raise HTTPException(
            status_code=400, detail="음성에서 텍스트를 추출하지 못했습니다."
        )

    # 3. 대화 히스토리 파싱
    history: list[dict] = []
    if history_json:
        try:
            parsed = json.loads(history_json)
            if isinstance(parsed, list):
                history = parsed
        except Exception:
            log.warning(f"invalid history_json: {history_json[:200]}")

    # 4. LLM 호출 (RAG 포함)
    try:
        reply_text, session_id, sources = await _run_chat_internal(
            request, question_text, history
        )
    except HTTPException:
        raise
    except Exception as e:
        log.exception(f"_run_chat_internal failed: {e}")
        raise HTTPException(
            status_code=500, detail=f"챗봇 응답 생성 실패: {str(e)}"
        )

    # 5. 응답 반환 (Phase 2a: 텍스트만 반환, 음성 재생은 프론트 브라우저 speechSynthesis 사용)
    # TTS 완전 통합은 audio.py의 speech() 함수를 리팩터링해야 하므로 차후 작업.
    return JSONResponse(
        {
            "question": question_text,
            "reply": reply_text,
            "session_id": session_id,
            "sources": sources,
            "audio_url": None,  # Phase 2a: 프론트 브라우저 TTS로 대체
        }
    )
