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
import re
import sys
import tempfile
import time
import uuid
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


# ────────────────────────────────────────────────────────────────────────
# Reply 후처리: 마크다운 / 인용 마커 제거
# 도민 안내용 자연스러운 대화체로 보이도록 LLM 답변에서 LLM-스러운 표식을 제거.
# TTS 가 "별표 별표" 등 기호를 그대로 읽지 않도록 음성 파이프라인에도 동일 적용됨.
# ────────────────────────────────────────────────────────────────────────

_RE_BOLD_AST = re.compile(r"\*\*([^*\n]+?)\*\*")
_RE_BOLD_UND = re.compile(r"__([^_\n]+?)__")
_RE_ITALIC_AST = re.compile(r"(?<![\*\w])\*([^*\n]+?)\*(?![\*\w])")
_RE_ITALIC_UND = re.compile(r"(?<![\w_])_([^_\n]+?)_(?![\w_])")
_RE_INLINE_CODE = re.compile(r"`([^`\n]+?)`")
_RE_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_RE_HRULE = re.compile(r"^\s*(?:-\s*){3,}\s*$|^\s*(?:_\s*){3,}\s*$|^\s*(?:\*\s*){3,}\s*$", re.MULTILINE)
_RE_LIST_BULLET = re.compile(r"^[ \t]*[-*+•]\s+", re.MULTILINE)
_RE_NUM_BULLET = re.compile(r"^[ \t]*\d+\.\s+", re.MULTILINE)
_RE_BLOCKQUOTE = re.compile(r"^[ \t]*>\s?", re.MULTILINE)
_RE_CITATION = re.compile(r"\s*\[\d+(?:\s*,\s*\d+)*\]")  # [1], [2], [1, 3]
_RE_LINK_MD = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_RE_MULTI_NEWLINE = re.compile(r"\n{3,}")
_RE_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)


def _looks_like_korean(text: str, threshold: float = 0.35) -> bool:
    """STT 결과가 한국어 텍스트로 보이는지 휴리스틱 판정.

    한국어 도청 챗봇은 도민이 한국어로 말한다는 강한 사전 가정이 있다.
    Cohere/Whisper 가 영어로 잘못 transcribe 한 경우 (낯선 한국어 발음을
    가까운 영어 단어로 매핑) 그 결과를 신뢰하면 RAG 가 의미 없는 영어
    문구를 검색해서 사용자 경험이 망가진다.

    한글 음절(가-힣) 비율이 threshold 이상이거나, 한국어 종결어미/조사가
    검출되면 True. 너무 짧은 발화(<3자)는 검증 제외.
    """
    s = (text or "").strip()
    if len(s) < 3:
        return True  # 단답("네"/"응") 은 통과
    hangul = sum(1 for ch in s if "가" <= ch <= "힣")
    letters = sum(1 for ch in s if ch.isalpha())
    if letters == 0:
        return True
    ratio = hangul / letters
    if ratio >= threshold:
        return True
    # 한국어 특유 토큰 검출 — 짧은 문장이 다 한자어 영어식 표기로 나오는 경우 보정
    korean_markers = (
        "이에요",
        "예요",
        "입니다",
        "습니다",
        "해요",
        "주세요",
        "어떻게",
        "뭐예요",
        "뭔가요",
        "있나요",
        "되나요",
        "할까요",
        "은요",
        "는요",
    )
    if any(m in s for m in korean_markers):
        return True
    return False


_RE_SENTENCE_END = re.compile(r"([.!?。…]|다[\.\s]|요[\.\s]|니다[\.\s]|에요[\.\s])")


# Cohere transcribe / Whisper 호환 STT 의 prompt 파라미터.
# 디코딩 시 어휘 편향을 줘서 "전북특별자치도", "인재개발원" 등 도청 도메인 용어가
# 비슷한 발음의 다른 단어("한국/반북", "재개발원") 보다 우선 출력되도록 한다.
# 첫 번째 문장은 도민이 자주 쓰는 정중한 한국어 어조 예시 — Whisper API 의
# prompt 는 "이전 발화 예시" 로 해석되므로 어조도 안내함.
_PUBLIC_STT_DOMAIN_PROMPT = (
    "안녕하세요. 전북특별자치도청에 문의드립니다. "
    "전북특별자치도, 전북도청, 인재개발원, 농업기술원, 보건환경연구원, "
    "산림환경연구원, 도립국악원, 도립미술관, 어린이창의체험관, "
    "농식품인력개발원, 경제통상진흥원, 일자리센터, 동물위생시험소, "
    "수산기술연구소, 축산연구소, 도로관리사업소, 투어전북. "
    "전주시, 익산시, 군산시, 정읍시, 김제시, 남원시, "
    "진안군, 무주군, 장수군, 임실군, 순창군, 고창군, 부안군, 완주군. "
    "정보공개청구, 행정정보공개, 추경예산, 본예산, 지방재정공시, "
    "세입세출결산, 시민제안, 옴부즈만, 민원24. "
    "농어민기본소득, 청년수당, 공공일자리, 재난지원금, 소상공인지원, 정보화교육. "
    "국민취업지원제도, 구직촉진수당, 내일배움카드, 고용센터, 고용24, 워크넷."
)


def _trim_text_for_tts(text: str, max_chars: int = 140, max_sentences: int = 2) -> str:
    """음성으로 들려줄 텍스트만 추출.

    - 첫 max_sentences 개 문장까지 사용
    - max_chars 초과면 마지막 문장 경계에서 자름
    - 끝에 별도 안내 멘트 추가 안 함 — 자막은 전체 답변, 음성은 일부 자른
      거라 안내 문구를 음성에만 붙이면 자막/음성이 어긋나 보임. 자연스럽게
      문장 끝에서 끊는 게 사용자 체감에도 깔끔.
    """
    s = (text or "").strip()
    if not s:
        return ""
    if len(s) <= max_chars:
        return s

    parts = re.split(r"(?<=[.!?。…])\s+|(?<=다)\s+|(?<=요)\s+", s)
    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        return s[:max_chars]

    out = []
    total = 0
    for p in parts[:max_sentences]:
        if total + len(p) + 1 > max_chars:
            break
        out.append(p)
        total += len(p) + 1
    if not out:
        out = [parts[0][: max_chars - 1]]

    return " ".join(out).strip()


def _humanize_reply(text: str) -> str:
    """LLM 답변에서 마크다운/인용 마커를 제거해 자연스러운 한국어 대화체로 변환."""
    if not text:
        return text or ""

    # 코드 블록 ``` ... ``` 통째 제거 전 내용만 남김
    text = re.sub(r"```[a-zA-Z0-9_-]*\n?(.*?)```", r"\1", text, flags=re.DOTALL)
    # 헤딩 (# 제거)
    text = _RE_HEADING.sub("", text)
    # 가로 구분선
    text = _RE_HRULE.sub("", text)
    # 인용 ">" 제거
    text = _RE_BLOCKQUOTE.sub("", text)
    # 마크다운 링크 [text](url) → "text (url)"
    text = _RE_LINK_MD.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    # 굵은 글씨 / 이탤릭 (안쪽 텍스트만 유지)
    text = _RE_BOLD_AST.sub(r"\1", text)
    text = _RE_BOLD_UND.sub(r"\1", text)
    text = _RE_ITALIC_AST.sub(r"\1", text)
    text = _RE_ITALIC_UND.sub(r"\1", text)
    # 인라인 코드 ` ` 제거
    text = _RE_INLINE_CODE.sub(r"\1", text)
    # 인용 마커 [1], [2,3]
    text = _RE_CITATION.sub("", text)
    # 리스트 마커는 유지 (가독성), 번호 매기기는 그대로 둠
    # 빈 줄 정리
    text = _RE_TRAILING_WS.sub("", text)
    text = _RE_MULTI_NEWLINE.sub("\n\n", text)
    return text.strip()

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field
from starlette.responses import FileResponse, JSONResponse, StreamingResponse

# (참고) 음성 응답 출력은 Phase 2a에서 브라우저 speechSynthesis API 활용,
# Phase 2b에서 Qwen3-TTS 서버 사이드 합성으로 전환 예정.

from open_webui.env import GLOBAL_LOG_LEVEL
from open_webui.models.users import UserModel
from open_webui.models.models import Models
from open_webui.utils.chat import generate_chat_completion
from open_webui.utils.middleware import process_chat_payload
from open_webui.utils.public_chatbot_rate_limit import (
    enforce_rate_limit_http as _enforce_rate_limit,
    enforce_text_input as _enforce_text_input,
)
from open_webui.utils.models import get_all_models
from open_webui.utils.public_voice import understand_public_voice

logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL)
log = logging.getLogger(__name__)

router = APIRouter()

PUBLIC_VOICE_AUDIO_PREFIX = "public-voice-"


def _public_chatbot_rag_files(request: Request) -> list[dict]:
    """Build explicit RAG file/collection attachments for the public chatbot.

    PUBLIC_CHATBOT_KNOWLEDGE_ID accepts comma separated entries:
    - <knowledge_id> or collection:<knowledge_id> for normal Knowledge bases
      (resolved to per-file `file-<id>` collections)
    - legacy:<collection_name> for direct vector collections such as crawler output

    Knowledge base UUID 는 그 자체로는 chroma collection 이 아니므로
    KB 의 file_ids 를 풀어서 각 파일을 type='file' 로 등록한다.
    그래야 retrieval.utils.get_sources_from_items 가 file-<id> chroma
    collection 을 검색해 chunk 를 가져올 수 있다.
    """
    raw_value = str(
        getattr(request.app.state.config, "PUBLIC_CHATBOT_KNOWLEDGE_ID", "") or ""
    ).strip()
    if not raw_value:
        return []

    from open_webui.models.knowledge import Knowledges

    files: list[dict] = []
    for raw_item in raw_value.split(","):
        item = raw_item.strip()
        if not item:
            continue

        if item.startswith("legacy:"):
            collection_name = item[len("legacy:") :].strip()
            if collection_name:
                files.append(
                    {
                        "id": collection_name,
                        "name": collection_name,
                        "type": "file",
                        "legacy": True,
                    }
                )
            continue

        if item.startswith("collection:"):
            item = item[len("collection:") :].strip()

        if not item:
            continue

        # KB UUID → 멤버 file 리스트로 풀어서 type='file' entries 로 등록
        # (knowledge_file 조인 테이블 사용 — data.file_ids JSON 컬럼은 deprecated)
        try:
            kb_files = Knowledges.get_files_by_id(item) or []
        except Exception as e:
            log.warning(f"Knowledges.get_files_by_id({item}) failed: {e}")
            kb_files = []

        if kb_files:
            for f in kb_files:
                fid = getattr(f, "id", None)
                if not fid:
                    continue
                fname = getattr(f, "filename", None) or fid
                files.append(
                    {
                        "id": fid,
                        "name": fname,
                        "type": "file",
                    }
                )
        else:
            # 빈 KB 거나 조회 실패 — fallback 으로 collection 항목 그대로 (기존 동작)
            files.append({"id": item, "name": item, "type": "collection"})

    return files


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
    audio_url: Optional[str] = None  # Qwen3-TTS 합성 결과 URL (실패 시 None → 클라이언트 fallback)


####################
# 날짜 컨텍스트 주입 헬퍼
####################


def _today_context_prefix() -> str:
    """오늘 날짜를 LLM에 주입해 '다음주', '이번달' 같은 상대 시간 표현을 해석하게 한다."""
    now = datetime.now()
    weekday_kr = ["월", "화", "수", "목", "금", "토", "일"][now.weekday()]
    return f"\n\n[오늘 날짜] {now.strftime('%Y년 %m월 %d일')} ({weekday_kr}요일)"


# 도메인 한정 가이드 — 챗봇이 개인 LLM 처럼 남용되지 않도록 LLM 측에서 1차 필터.
# Phase B (회의 안건 7) — rate limit 외에 LLM instruction-following 으로 비도청 질문
# 정중 거부. Qwen 397B 가 잘 따른다.
_DOMAIN_GUARD_DIRECTIVES = (
    "\n\n## 답변 범위 — 가장 엄격하게 지킬 것\n"
    "당신은 **전북도청 및 산하·유관 기관 안내 전용 챗봇**입니다. 도청 업무·정책·"
    "사업·민원 안내·산하기관 정보 외 일반 지식 질문에는 답하지 마세요.\n\n"
    "다음 카테고리는 **반드시 거부**:\n"
    "- 코딩/프로그래밍 도움 (Python·SQL·HTML·디버깅 등)\n"
    "- 번역 (영-한, 한-영, 기타 언어 번역)\n"
    "- 일반 상식·수학·과학·역사·인물 질문 (도청 무관)\n"
    "- 글쓰기·작문·요약·기사 작성 도움\n"
    "- 의료·법률·금융 상담 (도청 안내 외)\n"
    "- 시·소설·노래 가사 등 창작\n"
    "- 다른 지자체·정부기관·외국 행정 안내 (전북도청 외)\n\n"
    "거부 멘트 (정중하게, 짧게):\n"
    '  "전북도청 대도민 안내 챗봇이라 그 질문은 도와드리기 어려워요. '
    "전북도청과 산하기관 안내 관련해 궁금하신 점 있으시면 말씀해 주세요.\"\n\n"
    "도청 어휘 (전북도청·전북특별자치도·시군청·도지사·정책·사업·답례품·"
    "고향사랑기부제·청년지원·신청·민원·홈페이지 등) 가 한 단어라도 들어가면 "
    "정상 답변. 애매하면 도청 관련 가능성 우선 가정."
)


def _augment_with_graph_context(system_prompt: str, query_text: str) -> str:
    """Neo4j GraphRAG (Page fulltext + Entity 1-hop) 결과를 system_prompt 에 append.

    vector RAG (Qdrant top-k) 와 dual-track. voice ↔ text 둘 다 같은 함수 호출해
    동일 retrieval 결과 보장. graph 실패 시 silently skip (LLM 답변에 영향 X).
    """
    try:
        from open_webui.retrieval.graphrag.neo4j_client import (
            search_pages_by_text,
            search_entities_neighbors,
        )

        gpages = search_pages_by_text(query_text, limit=5)
        ent_hits = search_entities_neighbors(query_text, limit=5)
        sections: list[str] = []

        if gpages:
            lines = []
            for p in gpages:
                t = (p.get("title") or "").strip()[:80]
                u = (p.get("url") or "").strip()
                inst = (p.get("institution") or "").strip()
                cat = (p.get("category") or "").strip()
                preview = (p.get("content_preview") or "").strip()[:300]
                lines.append(f"- [{inst}/{cat}] {t} ({u})\n  {preview}")
            sections.append(
                "\n\n# 관련 페이지 (지식그래프 검색 결과)\n"
                "다음은 사용자 질문과 관련 가능성 있는 페이지의 요약입니다. "
                "필요 시 참고하되 구체 답변은 RAG 컨텍스트에서 인용해 주세요.\n"
                + "\n".join(lines)
            )

        if ent_hits:
            ent_lines = []
            for e in ent_hits:
                name = (e.get("name") or "").strip()
                etype = (e.get("type") or "").strip()
                neighbors = e.get("neighbors") or []
                nb_strs = []
                for n in neighbors[:8]:
                    if not isinstance(n, dict) or not n.get("name"):
                        continue
                    nb_strs.append(f"{n.get('predicate','')}: {n.get('name')}")
                if nb_strs:
                    ent_lines.append(f"- {name} ({etype}) — " + "; ".join(nb_strs))
            if ent_lines:
                sections.append(
                    "\n\n# 관련 엔티티 (지식그래프 1-hop)\n"
                    "사용자 질문과 매치된 엔티티 및 직접 연결된 정보. "
                    "예: 사업명·자격·금액·담당부서 등 관계 추론 시 참고.\n"
                    + "\n".join(ent_lines)
                )

        if sections:
            return system_prompt + "".join(sections)
    except Exception as e:
        log.debug(f"graphrag search skipped: {e}")
    return system_prompt


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

    # 2. 시스템 프롬프트 (설정값 + 오늘 날짜 + graph RAG)
    system_prompt = getattr(
        request.app.state.config,
        "PUBLIC_CHATBOT_SYSTEM_PROMPT",
        "당신은 전북특별자치도청 대도민 안내 AI입니다.",
    )
    system_prompt = system_prompt + _today_context_prefix() + _DOMAIN_GUARD_DIRECTIVES
    # voice 모드와 동일하게 graph RAG (Neo4j) 도 텍스트 모드에 적용 — 두 모드
    # 답변 일관성. 시군별 답례품/세부 품목 같은 fan-out 정보가 vector 만으론 누락.
    system_prompt = _augment_with_graph_context(system_prompt, user_message)

    # 3. messages 조립 (system → history → 현재 질문)
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
    # NOTE: chat_id 는 socket 이벤트 emitter 내부에서 .startswith("local:") 호출하므로
    # None 이면 AttributeError 발생. 빈 문자열로 두어 emitter 가 안전하게 분기하게 함.
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

    # 5. form_data 초기화 — chat_completion_files_handler 가 files 를
    # metadata["files"] 에서 읽으므로 양쪽에 모두 세팅한다.
    rag_files = _public_chatbot_rag_files(request)
    metadata["files"] = rag_files

    form_data = {
        "model": model_id,
        "messages": messages,
        "stream": False,
        "metadata": metadata,
        "files": rag_files,
    }

    # request.state에도 metadata 반영 (generate_chat_completion 내부에서 참조됨)
    try:
        request.state.metadata = metadata
    except Exception:
        pass

    # 6. 핵심 — process_chat_payload로 RAG 컨텍스트/도구 주입
    rag_sources: list[dict] = []
    log.info(
        f"[RAG-call sync] q={messages[-1]['content'][:60]!r} "
        f"sys_len={len(system_prompt)} hist={len(messages)-2} "
        f"files={len(rag_files)} model={model_id!r}"
    )
    try:
        form_data, metadata, events = await process_chat_payload(
            request, form_data, user, metadata, model
        )
        # process_chat_payload 가 metadata["sources"] 에 RAG 결과를 저장.
        # LLM 응답에는 sources 가 안 실리므로 (streaming/dict 모두) 여기서 직접 수집.
        rag_sources = list(metadata.get("sources") or [])
        log.info(
            f"public_chatbot RAG sources from metadata: {len(rag_sources)} groups"
        )
    except Exception as e:
        log.exception(f"process_chat_payload failed: {e}")
        # RAG 주입 실패해도 plain LLM 호출은 계속 시도
        log.warning("Falling back to plain LLM call without RAG injection")

    # 6.5 RAG/도구 결과는 이미 messages 에 주입됨 — OpenAI 호환 서버가
    # 'files', 'tool_servers' 등의 비표준 키를 거부하므로 호출 전에 제거.
    for k in (
        "files",
        "tool_servers",
        "knowledge",
        "tools",
        "tool_ids",
        "function_calling",
        "metadata",
        "session_id",
        "chat_id",
        "id",
        "messageId",
        "background_tasks",
        "features",
        "variables",
        "filter_ids",
        "params",
    ):
        form_data.pop(k, None)

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
    elif isinstance(response, JSONResponse) or hasattr(response, "body"):
        # generate_chat_completion 이 JSONResponse 를 반환하는 경로 (non-streaming) 처리
        try:
            body = response.body
            if isinstance(body, bytes):
                body = body.decode("utf-8", errors="replace")
            obj = json.loads(body) if body else {}
            try:
                reply_text = obj["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError):
                reply_text = (
                    obj.get("detail") or obj.get("message")
                    or json.dumps(obj, ensure_ascii=False)
                )
            sources = obj.get("sources", []) or []
        except Exception as e:
            log.warning(f"JSONResponse parse failed: {e}")
            reply_text = "응답을 처리하지 못했습니다."
    else:
        log.warning(
            f"unexpected response type from generate_chat_completion: {type(response).__name__}"
        )
        reply_text = str(response)

    # process_chat_payload 가 미리 수집한 RAG sources 가 있으면 우선 사용 (LLM
    # 응답에는 안 실리는 경우가 대부분). LLM 응답에 sources 가 따로 실린 드문
    # 경우만 sources 를 그대로 둔다.
    if rag_sources and not sources:
        sources = rag_sources

    # 마크다운 / [N] 인용 마커 제거 — TTS 가 기호를 그대로 읽지 않게 함
    return _humanize_reply(reply_text), sources, model_id


# ────────────────────────────────────────────────────────────────────────
# Streaming 변형 — 음성 WebSocket 경로 (voice_ws.py) 가 첫 토큰 도착 즉시
# sentence 단위로 TTS 합성 시작하도록 SSE delta 를 async generator 로 yield.
# 기존 _run_public_llm 의 form_data 구성을 그대로 복제 (분기 안 함으로써
# 텍스트 모드 /chat 의 동작에 영향 없음).
# ────────────────────────────────────────────────────────────────────────


_VOICE_MODE_DIRECTIVES = (
    "\n\n참고: 이번 답변은 도민에게 음성으로 들려드립니다.\n"
    "## 음성 답변 길이 — 가장 중요\n"
    "**2~3 문장, 총 150자 이내로 짧게**. 답변이 길면 합성·재생 누적 latency 가 커져 "
    "사용자가 답답해합니다.\n"
    "  - 핵심 결론 + 가장 중요한 부가 정보 1~2개 + 담당 연락처(있을 경우) 정도까지만.\n"
    "  - 세부 자격 조건·신청 기간·구비 서류 같은 상세는 음성에 다 넣지 말고 "
    "'자세한 건 ~ 홈페이지나 ~ 전화로 확인해 주세요' 정도로 안내.\n"
    "  - 사용자가 후속 질문하면 그때 추가 정보 더 들려주는 식.\n\n"
    "## 표현·발음\n"
    "자연스러운 한국어 구어체. URL·영문 코드명·영어 약어 (KTX/USB 등) 는 직접 쓰지 말고 "
    "한국어 풀어쓰기 ('전북도청 홈페이지', '고속철도') 로 바꿔 주세요. "
    "외래어/외국어 단어는 가능하면 한국어 동의어로 대체. "
    "콜론 정렬 목록 대신 자연 문장으로 이어가 주세요."
    "\n\n## 음성 인식(STT) 오류 대처 — 핵심 의도 파악 우선\n"
    "사용자 발화는 음성→텍스트 변환을 거쳐 오므로 단어 단위 오인식이 자주 섞여 있다. "
    "특히 멀티턴 합치기로 여러 단편 발화가 한 user_text 로 들어올 때, 의미 없는 짧은 단편 "
    "('어', '음', '아', '네', '맞', '잠깐', '그게' 등) 이나 도청 도메인과 무관한 한국어 동음 "
    "오인식 단어 ('발휘', '강을 건너', '왼쪽만 천천히', '용기 부실이 없다' 등) 가 끼어 있어도 "
    "**전체 발화의 핵심 의도만 파악해서 답하라**. "
    "다음 원칙을 지킬 것:\n"
    "1) 핵심 의도 파악이 우선 — 도청·시군·산하기관 어휘 (전북도청, 김관영 도지사, 종합계획, "
    "청년지원, 보도자료, 기부제, 공고 등) 가 한 단어라도 보이면 그게 진짜 질문 의도다. "
    "주변의 noise 단어는 STT 오인식으로 보고 무시한다.\n"
    "2) '말씀하신 \"X\" 가 무엇인지 정확히 모르겠어요' 식 답변은 절대 금지 — X 가 도청 도메인 "
    "어휘와 무관한 짧은 단편이면 STT 오인식일 가능성이 압도적으로 높다. 그런 단어는 답변에 "
    "언급하지도 말고 무시하라.\n"
    "3) 발화 안에서 도청 어휘를 한 개라도 잡으면 → 그 어휘에 대한 답을 자료에서 찾아 답한다. "
    "두 개 이상이면 가장 마지막에 언급된 것 또는 의미상 가장 명확한 것 우선.\n"
    "4) 정 모를 때만 '어떤 내용을 찾으시는지 한 번 더 말씀해 주실 수 있을까요?' 정도로 짧게 "
    "되묻기 — 깨진 단어를 인용하지 말 것."
)


async def _stream_public_llm_reply(
    request: Request,
    user: UserModel,
    user_message: str,
    history: list[dict],
    session_id: str,
    voice_mode: bool = False,
    retrieval_query: str | None = None,
):
    """LLM 응답을 SSE delta 별로 yield 하는 async generator.

    yield 형식:
    - ("delta", str)  — LLM 부분 응답 청크 (sentence aggregator 가 처리)
    - ("done",  str)  — 응답 종료, humanize 적용한 전체 reply text
    - ("error", str)  — 호출 실패 (호출자가 폴백 처리)

    voice_mode=True 면 system_prompt 끝에 구어체 / URL 금지 등 음성용 가이드 append.
    텍스트 모드 (/chat) 는 voice_mode=False 라 영향 없음.

    retrieval_query (음성 모드 전용): mini-LLM 으로 STT noise 제거한 깨끗한 키워드.
      - graph + vector RAG retrieval 단계에서만 사용 (정확한 chunk hit)
      - LLM 답변 생성에는 원본 user_message 그대로 (사용자 의도/맥락 보존)
    """
    effective_retrieval_query = (retrieval_query or "").strip() or user_message
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
            log.warning(f"get_all_models failed in stream: {e}")
    if model_id not in (request.app.state.MODELS or {}):
        fallback = getattr(
            request.app.state.config,
            "PUBLIC_CHATBOT_BASE_MODEL",
            "gpt-4o-mini",
        )
        if fallback not in (request.app.state.MODELS or {}):
            yield ("error", f"모델 미발견: {model_id}, {fallback}")
            return
        model_id = fallback
    model = request.app.state.MODELS[model_id]

    # 2~3. system_prompt + messages
    system_prompt = getattr(
        request.app.state.config,
        "PUBLIC_CHATBOT_SYSTEM_PROMPT",
        "당신은 전북특별자치도청 대도민 안내 AI입니다.",
    ) + _today_context_prefix() + _DOMAIN_GUARD_DIRECTIVES
    if voice_mode:
        system_prompt = system_prompt + _VOICE_MODE_DIRECTIVES

    # GraphRAG (Neo4j) 결과를 system_prompt 에 inject — 시군별 답례품/세부 품목
    # 같은 fan-out 정보가 vector chunk 만으로는 누락되기 쉬워서 graph 보조 검색.
    # voice ↔ text 일관성을 위해 _run_chat_internal (텍스트) 에도 동일 graph 호출.
    system_prompt = _augment_with_graph_context(system_prompt, effective_retrieval_query)

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    for turn in history or []:
        role = turn.get("role") if isinstance(turn, dict) else getattr(turn, "role", None)
        content = (
            turn.get("content")
            if isinstance(turn, dict)
            else getattr(turn, "content", None)
        )
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    # process_chat_payload 가 messages 마지막 user content 를 query 로 retrieval 호출.
    # 음성 모드면 effective_retrieval_query (cleaned keyword) 를 임시로 넣어 정확
    # retrieval 받고, 호출 후 다시 원본 user_message 로 swap 해 LLM 답변에는 원본
    # 의도가 전달되도록.
    messages.append({"role": "user", "content": effective_retrieval_query})

    # 4. metadata
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
    rag_files = _public_chatbot_rag_files(request)
    metadata["files"] = rag_files

    # 5. form_data (stream=True — 핵심 차이점)
    form_data: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "stream": True,
        "metadata": metadata,
        "files": rag_files,
    }
    try:
        request.state.metadata = metadata
    except Exception:
        pass

    # 6. RAG 컨텍스트 주입
    rag_sources: list[dict] = []
    log.info(
        f"[RAG-call stream] voice_mode={voice_mode} "
        f"q={messages[-1]['content'][:60]!r} "
        f"sys_len={len(system_prompt)} hist={len(messages)-2} "
        f"files={len(rag_files)} model={model_id!r}"
    )
    try:
        form_data, metadata, _events = await process_chat_payload(
            request, form_data, user, metadata, model
        )
        rag_sources = list(metadata.get("sources") or [])
        log.info(
            f"public_chatbot stream RAG sources from metadata: {len(rag_sources)} groups"
        )
        # 음성 모드 retrieval swap 복구 — RAG context 는 보존하면서 cleaned keyword 만
        # 원본 user_message 로 한 번 replace. LLM 이 사용자 원본 의도/맥락을 그대로 봄.
        if effective_retrieval_query != user_message:
            for _msg in reversed(form_data.get("messages") or []):
                if _msg.get("role") == "user":
                    _c = _msg.get("content")
                    if isinstance(_c, str) and effective_retrieval_query in _c:
                        _msg["content"] = _c.replace(
                            effective_retrieval_query, user_message, 1
                        )
                    break
    except Exception as e:
        log.warning(f"process_chat_payload failed in stream: {e}")

    # 비표준 키 제거
    for k in (
        "files", "tool_servers", "knowledge", "tools", "tool_ids",
        "function_calling", "metadata", "session_id", "chat_id", "id",
        "messageId", "background_tasks", "features", "variables",
        "filter_ids", "params",
    ):
        form_data.pop(k, None)
    form_data["stream"] = True  # process_chat_payload 가 덮어쓸 수 있어 다시 강제

    # 7. LLM 호출
    try:
        response = await generate_chat_completion(
            request, form_data, user=user, bypass_filter=True
        )
    except Exception as e:
        log.exception(f"generate_chat_completion stream failed: {e}")
        yield ("error", str(e))
        return

    # 8. SSE chunk → delta yield
    full_text = ""
    if isinstance(response, StreamingResponse):
        try:
            async for chunk in response.body_iterator:
                if isinstance(chunk, bytes):
                    chunk_str = chunk.decode("utf-8", errors="replace")
                elif isinstance(chunk, str):
                    chunk_str = chunk
                else:
                    continue
                for line in chunk_str.splitlines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:].strip()
                    if payload in ("", "[DONE]"):
                        continue
                    try:
                        obj = json.loads(payload)
                        delta = (
                            obj.get("choices", [{}])[0]
                            .get("delta", {})
                            .get("content", "")
                            or ""
                        )
                        if delta:
                            full_text += delta
                            yield ("delta", delta)
                    except Exception:
                        continue
        except Exception as e:
            log.exception(f"stream iteration error: {e}")
    elif isinstance(response, dict):
        # endpoint 가 streaming 미지원 — 한꺼번에 받음
        try:
            text = response["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            text = json.dumps(response, ensure_ascii=False)
        if text:
            full_text = text
            yield ("delta", text)
    elif isinstance(response, JSONResponse) or hasattr(response, "body"):
        try:
            body = response.body
            if isinstance(body, bytes):
                body = body.decode("utf-8", errors="replace")
            obj = json.loads(body) if body else {}
            try:
                text = obj["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError):
                text = obj.get("detail") or obj.get("message") or ""
            if text:
                full_text = text
                yield ("delta", text)
        except Exception as e:
            log.warning(f"JSONResponse parse failed in stream: {e}")

    # 9. sources event 발행 — voice 자막에 [출처] 표시용 (필요시 소비)
    if rag_sources:
        yield ("sources", rag_sources)

    # 10. 종료 — humanized 전체 텍스트
    final = _humanize_reply(full_text) if full_text else ""
    yield ("done", final)


# ────────────────────────────────────────────────────────────────────────
# 챗봇 UI HTML 영구 서빙 — overlay layer 의 /static/ 이 컨테이너 재시작/워커 reload
# 시점에 사라지는 문제를 우회. 영구 볼륨(/data/owi/public_chatbot/index.html)을
# 직접 읽어 응답한다. 브라우저는 /api/v1/public/chatbot.html 을 호출.
# ────────────────────────────────────────────────────────────────────────

_PUBLIC_CHATBOT_HTML_CANDIDATES = [
    "/data/owi/public_chatbot/index.html",
    "/app/backend/open_webui/static/public-chatbot.html",
]


@router.get("/chatbot.html")
async def public_chatbot_ui():
    """공개 챗봇 UI HTML 서빙 (영구 볼륨 우선, fallback 으로 image 경로).

    빈번한 UI 디자인 갱신이 캐시에 막히는 걸 방지하기 위해 Cache-Control:
    no-cache 헤더 부착. 브라우저가 항상 서버에 재검증.
    """
    no_cache_headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    for path in _PUBLIC_CHATBOT_HTML_CANDIDATES:
        try:
            if os.path.isfile(path):
                return FileResponse(
                    path,
                    media_type="text/html; charset=utf-8",
                    headers=no_cache_headers,
                )
        except Exception as e:
            log.debug(f"chatbot_ui candidate {path} failed: {e}")
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="공개 챗봇 UI 파일을 찾지 못했습니다.",
    )


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

    # 오남용 방지: 기존 분당 + 새로 추가된 일당/전체 cap, 입력 검증.
    await _check_rate_limit(request)
    _enforce_rate_limit(request)
    from open_webui.utils.public_chatbot_rate_limit import get_client_ip as _get_ip
    _enforce_text_input(body.message, ip=_get_ip(request))

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

    # 텍스트 입력에도 Qwen3-TTS 음성 출력 — 음성챗봇 컨셉에 맞춰 양방향 동일 UX.
    audio_url = None
    try:
        audio_url = await _synthesize_public_qwen_tts(request, reply_text)
    except Exception as e:
        log.warning(f"public_chat TTS synthesis failed: {e}")

    return PublicChatResponse(
        reply=reply_text,
        session_id=session_id,
        sources=sources,
        model=resolved_model,
        audio_url=audio_url,
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


async def _synthesize_public_qwen_tts(request: Request, text: str) -> Optional[str]:
    """OpenAI 호환 TTS 엔드포인트(현재 Qwen3-TTS @192.168.30.2:30201)로 음성 합성.

    DB 의 audio.tts.openai.api_base_url 설정을 사용하므로 STT 와 동일한 외부
    vLLM 서버 패턴을 따른다. 결과 파일을 SPEECH_CACHE_DIR 에 저장하고
    /api/v1/public/voice-audio/<filename> 로 서빙.

    실패 시 None 반환. 클라이언트는 audio_url 없으면 자막만 표시 (브라우저 TTS 미사용).
    """
    log.debug(f"enter text_len={len(text or '')!r}")
    if not text or not text.strip():
        log.debug("return None: empty text")
        return None

    # 음성 출력은 첫 3문장만 (도민이 길게 듣고 있기 어려움) — 화면엔 전체 reply
    text = _trim_text_for_tts(text)
    log.debug(f"after_trim len={len(text)} preview={text[:60]!r}")

    cfg = request.app.state.config
    engine = (getattr(cfg, "AUDIO_TTS_ENGINE", "") or getattr(cfg, "TTS_ENGINE", "") or "").lower()
    log.debug(f"engine={engine!r}")
    # 'qwen' 또는 'openai'(vLLM 호환) 양쪽 모두 지원
    if engine not in {"qwen", "openai", ""}:
        log.debug(f"return None: unsupported engine {engine!r}")
        log.info(f"Public voice TTS skipped — unsupported engine '{engine}'")
        return None

    base_url = (
        getattr(cfg, "AUDIO_TTS_OPENAI_API_BASE_URL", "")
        or getattr(cfg, "TTS_OPENAI_API_BASE_URL", "")
        or ""
    )
    api_key = (
        getattr(cfg, "AUDIO_TTS_OPENAI_API_KEY", "")
        or getattr(cfg, "TTS_OPENAI_API_KEY", "")
        or "dummy"
    )
    model = (
        getattr(cfg, "AUDIO_TTS_MODEL", "")
        or getattr(cfg, "TTS_MODEL", "")
        or "qwen3-tts"
    )
    voice = (
        getattr(cfg, "AUDIO_TTS_VOICE", "")
        or getattr(cfg, "TTS_VOICE", "")
        or "Sohee"
    )
    log.debug(f"base_url={base_url!r} model={model!r} voice={voice!r}")
    if not base_url:
        log.debug("return None: no base_url")
        log.info("Public voice TTS skipped — no TTS base url configured")
        return None

    try:
        from open_webui.routers.audio import SPEECH_CACHE_DIR
    except Exception as e:
        log.debug(f"return None: audio import failed {e}")
        log.exception(f"audio router import failed: {e}")
        return None

    cache_key = hashlib.sha256(
        json.dumps(
            {"text": text, "base": base_url, "model": model, "voice": voice},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    filename = f"{PUBLIC_VOICE_AUDIO_PREFIX}{cache_key}.mp3"
    file_path = Path(SPEECH_CACHE_DIR).joinpath(filename)
    log.debug(f"file_path={file_path} exists={file_path.is_file()}")

    if file_path.is_file():
        log.debug(f"return cached path={filename}")
        return f"/api/v1/public/voice-audio/{filename}"

    # 너무 긴 텍스트는 자르기 (TTS 안정성 + 사용자 듣기 편의)
    speak_text = text.strip()
    if len(speak_text) > 800:
        speak_text = speak_text[:800].rsplit(" ", 1)[0]

    try:
        import aiohttp

        url = base_url.rstrip("/") + "/audio/speech"
        payload = {
            "model": model,
            "voice": voice,
            "input": speak_text,
            "response_format": "mp3",
        }
        headers = {"Authorization": f"Bearer {api_key}"}
        # Qwen3-TTS 가 한국어 ~150자 기준으로 합성에 20~40초 걸려서 넉넉히 90초.
        # 동시 호출 부하 상황에서도 timeout 으로 인한 audio_url=None 회피.
        timeout = aiohttp.ClientTimeout(total=90)
        connector = aiohttp.TCPConnector(ssl=False)
        log.debug(f"POST {url} model={model} voice={voice} len={len(speak_text)}")
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as s:
            async with s.post(url, json=payload, headers=headers) as resp:
                log.debug(f"resp status={resp.status}")
                if resp.status >= 400:
                    body = (await resp.text())[:300]
                    log.debug(f"return None: HTTP {resp.status} body={body!r}")
                    log.warning(f"Public TTS HTTP {resp.status}: {body}")
                    return None
                audio_bytes = await resp.read()
        log.debug(f"audio_bytes len={len(audio_bytes or b'')}")
        if not audio_bytes:
            log.debug("return None: empty bytes")
            log.warning("Public TTS empty response")
            return None
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.debug(f"mkdir except {e}")
        try:
            with open(file_path, "wb") as f:
                f.write(audio_bytes)
            log.debug(f"wrote file {file_path} size={file_path.stat().st_size}")
        except Exception as e:
            log.debug(f"return None: write failed {e}")
            return None
        log.debug(f"return ok /api/v1/public/voice-audio/{filename}")
        return f"/api/v1/public/voice-audio/{filename}"
    except Exception as e:
        log.debug(f"return None: outer exception {type(e).__name__} {e}")
        log.exception(f"Public TTS request failed: {e}")
        return None


@router.get("/voice-audio/{filename}")
async def public_voice_audio(filename: str):
    """Serve cached public voice replies generated by Qwen3-TTS."""
    safe_name = os.path.basename(filename)
    if safe_name != filename or not safe_name.startswith(PUBLIC_VOICE_AUDIO_PREFIX):
        raise HTTPException(status_code=404, detail="Audio not found")

    suffix = Path(safe_name).suffix.lower()
    if suffix not in {".wav", ".mp3"}:
        raise HTTPException(status_code=404, detail="Audio not found")

    from open_webui.routers.audio import SPEECH_CACHE_DIR

    file_path = SPEECH_CACHE_DIR.joinpath(safe_name)
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Audio not found")

    media_type = "audio/wav" if suffix == ".wav" else "audio/mpeg"
    return FileResponse(file_path, media_type=media_type)


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
    seconds_since_bot: Optional[str] = Form(
        None,
        description="봇 마지막 응답 종료 후 경과 초 (echo 방지/대화 lock 용)",
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
    _enforce_rate_limit(request)
    # 음성 업로드는 텍스트 검증 X (STT 결과로 들어오는 텍스트는 별도)

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

    # 2. STT — 기존 transcribe() 재사용.
    #   - 한국어 강제 (metadata.language="ko")
    #   - 한국어 휴리스틱으로 재검증 — 영어로 잡힌 경우 거절
    #
    # 도메인 biasing prompt 는 Cohere transcribe 구현이 prompt 를 그대로 echo
    # 하거나 출력을 왜곡하는 문제로 비활성화 (실측 결과 정상 음성도 빈 응답이 됨).
    # 더 좋은 STT 모델로 교체 후 재시도 예정.
    user = _get_public_user(request)
    try:
        stt_result = audio_transcribe(
            request,
            audio_path,
            metadata={"language": "ko"},
            user=user,
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

    # [PURE MODE] 한국어 검증 / 도메인 사전 보정 / directedness 휴리스틱 모두 비활성화.
    # STT 의 raw 결과를 그대로 LLM 에 전달해서 순수 모델 품질 측정.

    # 3. 대화 히스토리 파싱
    history: list[dict] = []
    if history_json:
        try:
            parsed = json.loads(history_json)
            if isinstance(parsed, list):
                history = parsed
        except Exception:
            log.warning(f"invalid history_json: {history_json[:200]}")

    # 휴리스틱 재활성: 도메인 사전 보정 + directedness/intent/short utterance + sensitive 검증
    stt_confidence = (stt_result or {}).get("confidence")
    secs_since_bot: Optional[float] = None
    if seconds_since_bot:
        try:
            secs_since_bot = float(seconds_since_bot)
        except (TypeError, ValueError):
            secs_since_bot = None

    voice_understanding = understand_public_voice(
        question_text,
        history=history,
        stt_confidence=stt_confidence,
        seconds_since_bot=secs_since_bot,
    )
    log.info(
        "voice-chat | transcript=%r | normalized=%r | action=%s | "
        "directed=%.2f | conf=%.2f | intent=%s | corrections=%s",
        question_text[:120],
        voice_understanding.normalized_text[:120],
        voice_understanding.action,
        voice_understanding.directedness,
        voice_understanding.confidence,
        voice_understanding.intent,
        voice_understanding.corrections,
    )

    # 옆 사람 대화/배경 소음으로 판단되면 응답 안 함
    if voice_understanding.action == "ignore":
        return JSONResponse({
            "question": voice_understanding.raw_text,
            "normalized_question": voice_understanding.normalized_text,
            "reply": "",
            "session_id": None,
            "sources": [],
            "audio_url": None,
            "ignored": True,
            "confidence": voice_understanding.confidence,
            "directedness": voice_understanding.directedness,
            "intent": voice_understanding.intent,
            "reason": voice_understanding.reason,
            "corrections": voice_understanding.corrections,
        })

    # 신뢰도 낮음 / sensitive intent → 확인 질문
    if voice_understanding.action == "clarify":
        reply_text = voice_understanding.confirmation or (
            "제가 이해한 내용이 맞는지 한 번 확인해 주세요."
        )
        audio_url = await _synthesize_public_qwen_tts(request, reply_text)
        return JSONResponse({
            "question": voice_understanding.raw_text,
            "normalized_question": voice_understanding.normalized_text,
            "reply": reply_text,
            "session_id": None,
            "sources": [],
            "audio_url": audio_url,
            "needs_confirmation": True,
            "confidence": voice_understanding.confidence,
            "directedness": voice_understanding.directedness,
            "intent": voice_understanding.intent,
            "reason": voice_understanding.reason,
            "corrections": voice_understanding.corrections,
        })

    # 4. LLM 호출 (RAG 포함) — 정규화된 transcript 로
    try:
        reply_text, session_id, sources = await _run_chat_internal(
            request, voice_understanding.normalized_text, history
        )
    except HTTPException:
        raise
    except Exception as e:
        log.exception(f"_run_chat_internal failed: {e}")
        raise HTTPException(status_code=500, detail=f"챗봇 응답 생성 실패: {str(e)}")

    audio_url = await _synthesize_public_qwen_tts(request, reply_text)

    return JSONResponse({
        "question": voice_understanding.raw_text,
        "normalized_question": voice_understanding.normalized_text,
        "reply": reply_text,
        "session_id": session_id,
        "sources": sources,
        "audio_url": audio_url,
        "ignored": False,
        "needs_confirmation": False,
        "confidence": voice_understanding.confidence,
        "directedness": voice_understanding.directedness,
        "intent": voice_understanding.intent,
        "reason": voice_understanding.reason,
        "corrections": voice_understanding.corrections,
    })
