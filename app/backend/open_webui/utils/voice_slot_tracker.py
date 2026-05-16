"""음성 챗봇 turn merge 용 슬롯 추출 / 변화 비교 / 추임새 판정.

도청 도메인 vocab 한정. 정규식 + 키워드 set 기반이라 latency 0.

사용처: routers/voice_ws.py 의 _TurnAwareLLMService 가 요약 filler 발동
여부 결정 + frontend 에 슬롯 chip push.
"""
from __future__ import annotations

import re

# ──────────────── 슬롯 카테고리 ────────────────

# 지역 — 도청 본청 + 시·군 14개 + 자주 언급되는 직속기관·운영 사이트
REGION_KEYWORDS: set[str] = {
    "전북도청", "전북특별자치도청", "전북특별자치도", "도청",
    "전주시", "전주", "익산시", "익산", "군산시", "군산",
    "정읍시", "정읍", "김제시", "김제", "남원시", "남원",
    "완주군", "완주", "진안군", "진안", "무주군", "무주",
    "장수군", "장수", "임실군", "임실", "순창군", "순창",
    "고창군", "고창", "부안군", "부안",
    "인재개발원", "농업기술원", "보건환경연구원", "산림환경연구원",
    "도립국악원", "도립미술관", "어린이창의체험관", "농식품인력개발원",
    "경제통상진흥원", "일자리센터", "동물위생시험소", "수산기술연구소",
    "축산연구소", "도로관리사업소", "투어전북",
}

# 대상 — 정책 수혜 대상 분류
AUDIENCE_KEYWORDS: set[str] = {
    "청년", "노인", "어르신", "어린이", "아동", "청소년",
    "농민", "농업인", "어민", "축산", "임업",
    "소상공인", "자영업", "기업",
    "장애인", "다문화", "결혼이민", "북한이탈",
    "여성", "남성", "1인가구", "한부모",
    "신혼", "임산부", "출산", "육아", "보육",
    "취업준비", "구직", "재직",
}

# 의도/액션
INTENT_KEYWORDS: set[str] = {
    "신청", "접수", "조회", "확인", "안내", "문의",
    "위치", "주소", "전화", "전화번호", "연락처",
    "예약", "방문", "이용", "참여",
    "지원", "보조금", "수당", "혜택",
    "절차", "방법", "자격", "조건",
}

# 상태/조건
STATUS_KEYWORDS: set[str] = {
    "미혼", "기혼", "이혼", "사별",
    "무주택", "유주택", "세입자", "전세", "월세",
    "저소득", "차상위", "기초생활",
    "재학", "졸업", "휴학",
    "실업", "재직", "구직",
}

# 나이 — 정규식 (만 N세, N살)
AGE_PATTERN = re.compile(r"만\s?(\d{1,3})\s?세|(\d{1,3})\s?살")

# 추임새 set — 짧고 의미 없는 발화
FILLER_TOKENS: set[str] = {
    "어", "음", "아", "어어", "음음", "아아",
    "잠깐", "잠깐만", "잠시만",
    "아니", "아냐", "아니아니",
    "그게", "그러니까", "그래서", "근데",
    "네", "예", "응", "맞아", "맞아요", "맞습니다",
    "어머", "와", "오", "헐",
    "하", "흠",
}


def is_filler_only(text: str) -> bool:
    """단순 추임새/단답인지 판정. 슬롯 0개 + 짧음 + 추임새 토큰 ratio 높음."""
    s = (text or "").strip()
    if not s:
        return True
    if len(s) <= 2:
        # 1~2 글자는 추임새일 가능성 매우 높음 ("네", "어", "응")
        return True

    tokens = [t for t in re.split(r"[\s,.\?!]+", s) if t]
    if not tokens:
        return True
    filler_count = sum(1 for t in tokens if t in FILLER_TOKENS)
    # 토큰 절반 이상이 추임새 + 문장 짧음 → filler only
    if filler_count / len(tokens) >= 0.5 and len(s) <= 10:
        return True

    # 슬롯이 하나도 추출 안 됨 + 짧음 → filler
    if len(s) <= 6 and not extract_slots(s):
        return True
    return False


def extract_slots(text: str) -> dict[str, list[str]]:
    """텍스트에서 카테고리별 슬롯 값 추출.

    반환: {"region": [...], "audience": [...], "intent": [...], "status": [...], "age": [...]}
    빈 카테고리는 dict 에서 제외.
    """
    s = (text or "").strip()
    if not s:
        return {}
    out: dict[str, list[str]] = {}

    found_region = sorted({k for k in REGION_KEYWORDS if k in s}, key=len, reverse=True)
    # 더 긴 매치 우선 + 짧은 거가 긴 거의 부분이면 제거 (전주시 매치되면 전주 제거)
    deduped_region: list[str] = []
    for k in found_region:
        if not any(k != other and k in other for other in deduped_region):
            deduped_region.append(k)
    if deduped_region:
        out["region"] = deduped_region

    found_audience = sorted({k for k in AUDIENCE_KEYWORDS if k in s})
    if found_audience:
        out["audience"] = found_audience

    found_intent = sorted({k for k in INTENT_KEYWORDS if k in s})
    if found_intent:
        out["intent"] = found_intent

    found_status = sorted({k for k in STATUS_KEYWORDS if k in s})
    if found_status:
        out["status"] = found_status

    ages: list[str] = []
    for m in AGE_PATTERN.finditer(s):
        n = m.group(1) or m.group(2)
        ages.append(f"만 {n}세")
    if ages:
        out["age"] = sorted(set(ages))

    return out


def merge_slots(old: dict[str, list[str]], new: dict[str, list[str]]) -> dict[str, list[str]]:
    """누적 슬롯에 새 슬롯 합치기. 카테고리별 값 unique 유지."""
    merged: dict[str, list[str]] = {k: list(v) for k, v in (old or {}).items()}
    for cat, values in (new or {}).items():
        existing = merged.setdefault(cat, [])
        for v in values:
            if v not in existing:
                existing.append(v)
    return merged


def compute_delta(old: dict[str, list[str]], new: dict[str, list[str]]) -> dict:
    """이전 누적 슬롯 vs 새 슬롯 비교. 새로 추가된 값 개수와 분류 반환.

    반환: {"new_slot_count": N, "new_values": [...], "added_categories": [...]}
    """
    new_values: list[str] = []
    added_cats: set[str] = set()
    for cat, values in (new or {}).items():
        old_values = set((old or {}).get(cat, []))
        for v in values:
            if v not in old_values:
                new_values.append(v)
                added_cats.add(cat)
    return {
        "new_slot_count": len(new_values),
        "new_values": new_values,
        "added_categories": sorted(added_cats),
    }


def flatten_slots_for_display(slots: dict[str, list[str]]) -> list[str]:
    """frontend chip 표시용 — 카테고리 순서대로 평탄화."""
    order = ["region", "audience", "age", "status", "intent"]
    out: list[str] = []
    for cat in order:
        for v in slots.get(cat, []):
            if v not in out:
                out.append(v)
    return out
