"""음성 챗봇 cleaning — mini-LLM 없는 룰 기반.

LLM 한 호출 (~1.5~2초) 의 효과를 룰로 분해해서 <10ms 에 동일 효과:
  · 필러 제거          → set 매칭
  · 멀티턴 dedup       → 토큰 unique 보존
  · 단답/추임새 판정    → 길이 + filler set
  · 지시대명사 치환    → 직전 user 발화의 명사구를 lookup → 단순 string replace
  · "질문은 ~입니다" 자막 → 단순 wrapping

LLM 안 쓰니 비용 0 + 끼워맞춤 hardcoding 0 (도청 vocab 안 쓰고 한국어 universal).
"""
from __future__ import annotations

import re
from typing import Optional


# ─────────────────────────────────────────────────────────────────────
# 1. 필러·추임새 set — 한국어 universal (도청 도메인 무관).
# 단 지시대명사 ("그건/그게/그것/거기/이것 등) 는 substitute_pronouns 가
# 처리하니 여기 넣지 않음 — 그래야 대명사 치환 우선 동작.
# ─────────────────────────────────────────────────────────────────────
_FILLERS: set[str] = {
    "어", "음", "아", "오", "응", "엥", "에",
    "네", "넵", "예", "맞", "맞아", "맞아요",
    "그", "그래", "그러니까", "그러게", "근데",
    "잠깐", "아니", "글쎄", "흠", "어머", "와", "헐", "뭐",
    "뭐야", "뭐였지",
}


def _norm_token(tok: str) -> str:
    """조사·종결어미 끝부분 제거 — '청년이' → '청년', '신청을' → '신청'."""
    return tok.strip(".,!?~ \t").rstrip("이가은는을를에서의도만")


def remove_fillers(text: str) -> str:
    """토큰 단위 필러 제거."""
    if not text:
        return ""
    tokens = text.split()
    out: list[str] = []
    for tok in tokens:
        bare = tok.strip(".,!?~ \t")
        if bare in _FILLERS:
            continue
        out.append(tok)
    return " ".join(out).strip()


# ─────────────────────────────────────────────────────────────────────
# 2. 단답·추임새만 들어왔는지 판정 (cleaning + retrieval skip 신호)
# ─────────────────────────────────────────────────────────────────────
def is_short_or_filler(text: str) -> bool:
    if not text:
        return True
    stripped = text.strip().strip(".,!?~")
    if len(stripped) <= 3:
        return True
    tokens = stripped.split()
    if not tokens:
        return True
    # 모든 토큰이 filler set 안
    if all(t.strip(".,!?~ \t") in _FILLERS for t in tokens):
        return True
    return False


# ─────────────────────────────────────────────────────────────────────
# 3. 멀티턴 dedup — 같은 토큰 반복 제거 (단순 unique 보존)
# ─────────────────────────────────────────────────────────────────────
def dedup_tokens(text: str) -> str:
    if not text:
        return ""
    seen = set()
    out: list[str] = []
    for tok in text.split():
        key = _norm_token(tok)
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(tok)
    return " ".join(out)


# ─────────────────────────────────────────────────────────────────────
# 4. 직전 user 발화에서 "주제 명사구" 추출 (대명사 치환용)
# ─────────────────────────────────────────────────────────────────────
# 한국어 의문사 + 동사 종결 어미 set — topic 에서 제외.
_INTERROGATIVES: set[str] = {
    "뭐", "뭐야", "무엇", "어떻게", "어디", "언제", "왜",
    "얼마", "몇", "어떤", "어느", "어떡해", "어쩌지",
}
_VERB_ENDINGS: set[str] = {
    "있어", "있나", "있나요", "있어요", "있을까", "있을까요",
    "있죠", "있는지", "있는가", "있는데", "있더라",
    "알려줘", "알려줄래", "알려주세요", "알려주실래요",
    "해줘", "해주세요", "해도", "해야", "할까", "할게",
    "맞나", "맞아요", "맞죠", "맞지",
    "이야", "이지", "이에요", "예요", "인가요", "인지", "인가",
    "되나", "되나요", "돼요", "될까", "되는지",
    "없는지", "없나요",
}


def extract_topic_from_history(history: list[dict]) -> Optional[str]:
    """가장 최근 user 발화에서 의문사·동사 제거 후 남은 명사구 = topic.

    예: "답례품 뭐 있어?"            → "답례품"
        "고향사랑기부제 신청 알려줘"  → "고향사랑기부제 신청"
        "청년 정책 뭐 있나요"         → "청년 정책"
    """
    if not history:
        return None
    for turn in reversed(history):
        if turn.get("role") != "user":
            continue
        content = (turn.get("content") or "").strip()
        if not content:
            continue
        # 필러 + 의문사 + 동사 종결 토큰 모두 제거 → 남은 게 topic
        tokens = content.split()
        topic_toks: list[str] = []
        for tok in tokens:
            bare = tok.strip(".,!?~ \t")
            bare_norm = _norm_token(bare)
            if bare in _FILLERS or bare_norm in _FILLERS:
                continue
            if bare in _INTERROGATIVES or bare_norm in _INTERROGATIVES:
                continue
            if bare in _VERB_ENDINGS or bare_norm in _VERB_ENDINGS:
                continue
            topic_toks.append(bare_norm if bare_norm else bare)
        if topic_toks:
            # 첫 2~3 토큰 = topic (보통 명사구 앞 = 핵심 주제)
            return " ".join(topic_toks[: min(3, len(topic_toks))])
    return None


# ─────────────────────────────────────────────────────────────────────
# 5. 지시대명사 치환
# ─────────────────────────────────────────────────────────────────────
_PRONOUNS_WITH_SUFFIX: dict[str, str] = {
    "거기서": "에서",
    "거기": "에서",
    "그거에서": "에서",
    "그것에서": "에서",
    "그건": "은",
    "그게": "이",
    "그거": "이",
    "그것": "이",
    "저거": "이",
    "저것": "이",
    "이거": "이",
    "이것": "이",
}


def substitute_pronouns(text: str, topic: Optional[str]) -> str:
    """발화 안 지시대명사를 topic 으로 치환. 첫 매칭만 (중복 치환 방지)."""
    if not topic or not text:
        return text
    # 긴 패턴 먼저 매칭 (거기서 > 거기, 그것에서 > 그것)
    for p in sorted(_PRONOUNS_WITH_SUFFIX.keys(), key=len, reverse=True):
        if p in text:
            suffix = _PRONOUNS_WITH_SUFFIX[p]
            return text.replace(p, f"{topic}{suffix}", 1)
    return text


# ─────────────────────────────────────────────────────────────────────
# 6. "질문은 …입니다" wrapping
# ─────────────────────────────────────────────────────────────────────
def wrap_as_question(text: str) -> str:
    """phase_overlay 자막용 — "질문은 [text] 에 대한 것입니다" 형식."""
    text = (text or "").strip().strip(".,!?~")
    if not text:
        return "질문이 잠시 명확하지 않아요."
    # 이미 의문문 (~?, ~까, ~요 등) 으로 끝나면 그대로
    if re.search(r"[가-힣](요|까|니|어|아|지)\?$", text + "?"):
        return f"질문은 {text} 입니다."
    return f"질문은 {text}에 대한 것입니다."


# ─────────────────────────────────────────────────────────────────────
# 7. 통합 — clean_user_text(raw, history) → (keyword, summary)
# ─────────────────────────────────────────────────────────────────────
def clean_user_text(raw: str, history: list[dict]) -> tuple[str, str]:
    """음성 누적 발화 raw → (RAG retrieval 키워드, phase_overlay 자막).

    예시:
      raw = "그 뭐야 어 음 청년 정책 알려줘"
      history = []
      → ("청년 정책 알려줘",
         "질문은 청년 정책 알려줘에 대한 것입니다.")

      raw = "거기서 농산물은 뭐 있어"
      history = [{"role":"user","content":"답례품 뭐 있어?"}, ...]
      → ("답례품에서 농산물은 뭐 있어",
         "질문은 답례품에서 농산물은 뭐 있어에 대한 것입니다.")

      raw = "네"  (단답)
      → ("", "질문이 잠시 명확하지 않아요.")  ← keyword 빈 문자열 = retrieval skip
    """
    if not raw:
        return "", "질문이 잠시 명확하지 않아요."

    # 단답·필러만 → cleaning + retrieval skip
    if is_short_or_filler(raw):
        return "", "질문이 잠시 명확하지 않아요."

    # 순서 중요:
    # 1) 직전 user 발화 명사구 → 대명사 치환 먼저 (필러 제거 전)
    #    "그건/그게" 등이 _FILLERS 에 안 들어가 있어야 살아남음.
    topic = extract_topic_from_history(history)
    cleaned = substitute_pronouns(raw, topic)
    # 2) 그 다음 필러 제거
    cleaned = remove_fillers(cleaned)
    # 3) 중복 토큰 정리 (멀티턴 합쳐진 경우)
    cleaned = dedup_tokens(cleaned)

    keyword = cleaned.strip()
    summary = wrap_as_question(keyword)
    return keyword, summary
