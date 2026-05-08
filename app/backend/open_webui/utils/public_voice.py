import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# 크롤 기반 동적 도메인 vocab — `build_domain_prompt.py` 가 생성한 JSON
# 파일을 lazy-load 해서 fuzzy 매칭 풀로 사용. 실시간 크롤 데이터 반영.
# ────────────────────────────────────────────────────────────────────────

_DYNAMIC_VOCAB_PATH = "/data/owi/cache/domain_vocab.json"
_dynamic_vocab_cache: Optional[list[str]] = None
_dynamic_vocab_mtime: float = 0.0
_vocab_lock = threading.Lock()


def _load_dynamic_vocab() -> list[str]:
    """파일이 갱신되면 자동 reload (mtime 비교, 캐시).

    실패 시 빈 리스트 반환 — 수동 정의 DOMAIN_TERMS 와 합쳐 사용.
    """
    global _dynamic_vocab_cache, _dynamic_vocab_mtime
    try:
        st = os.stat(_DYNAMIC_VOCAB_PATH)
    except OSError:
        return _dynamic_vocab_cache or []
    if _dynamic_vocab_cache is not None and st.st_mtime == _dynamic_vocab_mtime:
        return _dynamic_vocab_cache
    with _vocab_lock:
        # double-check
        if _dynamic_vocab_cache is not None and st.st_mtime == _dynamic_vocab_mtime:
            return _dynamic_vocab_cache
        try:
            data = json.loads(Path(_DYNAMIC_VOCAB_PATH).read_text(encoding="utf-8"))
            if isinstance(data, list):
                _dynamic_vocab_cache = [str(x) for x in data if isinstance(x, str)]
                _dynamic_vocab_mtime = st.st_mtime
                log.info(
                    f"loaded dynamic domain vocab: {len(_dynamic_vocab_cache)} terms"
                )
        except Exception as e:
            log.warning(f"dynamic vocab load failed: {e}")
            _dynamic_vocab_cache = _dynamic_vocab_cache or []
    return _dynamic_vocab_cache or []


DOMAIN_TERMS = [
    # 고용/취업 (기존)
    "국민취업지원제도",
    "구직촉진수당",
    "취업활동계획",
    "IAP",
    "상담사",
    "고용센터",
    "워크넷",
    "고용24",
    "일경험",
    "직업훈련",
    "내일배움카드",
    "참여수당",
    "훈련참여지원수당",
    "취업성공수당",
    "특정계층",
    "중위소득",
    "재산요건",
    "소득요건",
    "미래내일 일경험",
    # 전북도청 본청/직속기관
    "전북특별자치도",
    "전북도청",
    "인재개발원",
    "농업기술원",
    "보건환경연구원",
    "산림환경연구원",
    "도립국악원",
    "도립미술관",
    "어린이창의체험관",
    "농식품인력개발원",
    "경제통상진흥원",
    "일자리센터",
    "동물위생시험소",
    "수산기술연구소",
    "축산연구소",
    "도로관리사업소",
    "투어전북",
    # 민원/예산/정보공개
    "추경예산",
    "본예산",
    "정보공개청구",
    "행정정보공개",
    "민원24",
    "도청 민원실",
    "옴부즈만",
    "시민제안",
    "지방재정공시",
    "세입세출",
    # 도청 사업/지원
    "농어민기본소득",
    "청년수당",
    "공공일자리",
    "재난지원금",
    "소상공인지원",
    "정보화교육",
    "주민자치",
    # 시군 (전북 14개)
    "전주시",
    "익산시",
    "군산시",
    "정읍시",
    "김제시",
    "남원시",
    "진안군",
    "무주군",
    "장수군",
    "임실군",
    "순창군",
    "고창군",
    "부안군",
    "완주군",
]

DOMAIN_REPLACEMENTS = [
    # 고용/취업
    (r"국취제", "국민취업지원제도"),
    (r"국민\s*취업\s*지원\s*제도", "국민취업지원제도"),
    (r"구직\s*(촉진|조치금|촉친)\s*수당", "구직촉진수당"),
    (r"고용\s*(이십사|24|이사)", "고용24"),
    (r"워크\s*넷", "워크넷"),
    (r"내일\s*배움\s*카드", "내일배움카드"),
    (r"취업\s*성공\s*수당", "취업성공수당"),
    (r"참여\s*수당", "참여수당"),
    (r"훈련\s*참여\s*지원\s*수당", "훈련참여지원수당"),
    (r"취업\s*활동\s*계획", "취업활동계획"),
    (r"미래\s*내일\s*일?\s*경험", "미래내일 일경험"),
    # 전북도청 본청/직속기관 (자주 잘못 인식되는 발음 보정)
    (r"전\s*북\s*특별\s*자치\s*도", "전북특별자치도"),
    (r"전\s*북\s*도\s*청", "전북도청"),
    (r"인재\s*개발원", "인재개발원"),
    (r"농업\s*기술원", "농업기술원"),
    (r"보건\s*환경\s*연구원", "보건환경연구원"),
    (r"산림\s*환경\s*연구원", "산림환경연구원"),
    (r"도립\s*국악원", "도립국악원"),
    (r"도립\s*미술관", "도립미술관"),
    (r"어린이\s*창의\s*체험관", "어린이창의체험관"),
    (r"경제\s*통상\s*진흥원", "경제통상진흥원"),
    (r"동물\s*위생\s*시험소", "동물위생시험소"),
    (r"수산\s*기술\s*연구소", "수산기술연구소"),
    (r"축산\s*연구소", "축산연구소"),
    (r"도로\s*관리\s*사업소", "도로관리사업소"),
    # 민원/예산
    (r"추경?\s*예산|추\s*경[\s가-힣]*예산|추\s*갱\s*예산|추\s*겅\s*예산", "추경예산"),
    (r"본\s*예산", "본예산"),
    (r"정보\s*공개\s*청구|정보\s*공게\s*청구|정보\s*공개\s*쳉구", "정보공개청구"),
    (r"민원\s*(이십사|24|이사)", "민원24"),
    (r"지방\s*재정\s*공시", "지방재정공시"),
    (r"세입\s*세출", "세입세출"),
    (r"세입\s*세출\s*결산", "세입세출결산"),
    # 도청 사업
    (r"농어민\s*기본\s*소득", "농어민기본소득"),
    (r"청년\s*수당", "청년수당"),
    (r"공공\s*일자리", "공공일자리"),
    (r"재난\s*지원금", "재난지원금"),
    (r"소상공인\s*지원", "소상공인지원"),
    (r"정보화\s*교육", "정보화교육"),
    # 시군 (오인식 보정)
    (r"전\s*주\s*시", "전주시"),
    (r"익\s*산\s*시", "익산시"),
    (r"군\s*산\s*시", "군산시"),
    (r"정\s*읍\s*시", "정읍시"),
    (r"김\s*제\s*시", "김제시"),
    (r"남\s*원\s*시", "남원시"),
    (r"진\s*안\s*군", "진안군"),
    (r"무\s*주\s*군", "무주군"),
    (r"장\s*수\s*군", "장수군"),
    (r"임\s*실\s*군", "임실군"),
    (r"순\s*창\s*군", "순창군"),
    (r"고\s*창\s*군", "고창군"),
    (r"부\s*안\s*군", "부안군"),
    (r"완\s*주\s*군", "완주군"),
]

WAKE_WORDS = [
    "도청아",
    "전북아",
    "챗봇아",
    "상담 챗봇아",
    "상담봇아",
]

REQUEST_SIGNALS = [
    "알려줘",
    "확인해줘",
    "찾아줘",
    "검색해줘",
    "설명해줘",
    "신청하려면",
    "어디서",
    "어떻게",
    "언제",
    "얼마",
    "필요한 서류",
    "제출서류",
    "돼요",
    "되나요",
    "가능한가요",
    "문의",
]

BACKGROUND_SIGNALS = [
    "야",
    "너",
    "맞냐",
    "아 맞다",
    "그러니까",
    "아까 그거",
    "이거 아닌가",
    "그거 했냐",
]

AFFIRMATIVE_SHORT = {"네", "예", "응", "맞아", "맞아요", "그래", "좋아요"}
NEGATIVE_SHORT = {"아니", "아니요", "아니야", "틀려", "안돼", "안 돼"}

SENSITIVE_SIGNALS = [
    "부정수급",
    "허위",
    "대리 신청",
    "대리신청",
    "소득 숨기",
    "소득숨기",
    "서류 조작",
    "서류조작",
    "수당만 받고",
]

INTENT_RULES = [
    ("schedule", ["언제", "일정", "기간", "마감", "지급일"]),
    ("amount", ["얼마", "금액", "수당", "지원금"]),
    ("channel", ["어디서", "사이트", "홈페이지", "고용24", "워크넷", "방문"]),
    ("documents", ["서류", "제출서류", "구비서류", "증빙"]),
    ("eligibility", ["돼요", "되나요", "가능", "자격", "대상", "요건", "안 돼요"]),
    ("cancel", ["취소", "포기", "중단", "철회", "신청취하"]),
    ("apply", ["신청", "접수", "원서", "참여"]),
]

WEEKDAYS = {
    "월요일": 0,
    "화요일": 1,
    "수요일": 2,
    "목요일": 3,
    "금요일": 4,
    "토요일": 5,
    "일요일": 6,
}

KOREAN_NUMBERS = {
    "공": 0,
    "영": 0,
    "일": 1,
    "하나": 1,
    "한": 1,
    "이": 2,
    "둘": 2,
    "두": 2,
    "삼": 3,
    "셋": 3,
    "세": 3,
    "사": 4,
    "넷": 4,
    "네": 4,
    "오": 5,
    "다섯": 5,
    "육": 6,
    "여섯": 6,
    "칠": 7,
    "일곱": 7,
    "팔": 8,
    "여덟": 8,
    "구": 9,
    "아홉": 9,
}


@dataclass
class VoiceUnderstanding:
    raw_text: str
    normalized_text: str
    action: str
    confidence: float
    directedness: float
    intent: Optional[str] = None
    confirmation: Optional[str] = None
    reason: Optional[str] = None
    corrections: list[str] = field(default_factory=list)


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _last_topic(history: list[dict[str, Any]]) -> str:
    for turn in reversed(history or []):
        content = str(turn.get("content", "")).strip()
        if turn.get("role") in {"user", "assistant"} and content:
            return content[:180]
    return ""


def _korean_number_to_int(token: str) -> Optional[int]:
    token = token.strip()
    if token.isdigit():
        return int(token)
    if token in KOREAN_NUMBERS:
        return KOREAN_NUMBERS[token]
    if "십" in token:
        head, _, tail = token.partition("십")
        value = KOREAN_NUMBERS.get(head, 1 if head == "" else None)
        if value is None:
            return None
        result = value * 10
        if tail:
            if tail not in KOREAN_NUMBERS:
                return None
            result += KOREAN_NUMBERS[tail]
        return result
    return None


def _apply_domain_corrections(text: str) -> tuple[str, list[str]]:
    """STT 결과 → 도메인 사전 fuzzy 매칭으로 보정.

    1) DOMAIN_REPLACEMENTS regex (정확한 패턴) — 가장 신뢰도 높음
    2) DOMAIN_TERMS (수동 60개) + 동적 vocab (크롤 기반 수백개) fuzzy 매칭
       — 0.88 이상 ratio 시 텍스트에 (정식 명칭) 형태로 부기
    """
    corrected = text
    corrections: list[str] = []

    # 1단계: regex 사전 보정 (정확 패턴)
    for pattern, replacement in DOMAIN_REPLACEMENTS:
        next_text = re.sub(pattern, replacement, corrected, flags=re.IGNORECASE)
        if next_text != corrected:
            corrections.append(replacement)
            corrected = next_text

    # 2단계: 수동 + 동적 vocab fuzzy 매칭
    compacted = _compact(corrected)
    # 수동 사전이 우선 (도청 핵심 용어), 그 다음 크롤 기반 동적 vocab
    candidate_terms = list(dict.fromkeys(list(DOMAIN_TERMS) + _load_dynamic_vocab()))

    matched_in_pass: set[str] = set()
    for term in candidate_terms:
        term_compact = _compact(term)
        if not term_compact or term in matched_in_pass:
            continue
        if term_compact in compacted:
            continue

        # window slide — term 길이 ±2 범위
        found = False
        lo = max(2, len(term_compact) - 2)
        hi = len(term_compact) + 3
        for size in range(lo, hi):
            if size > len(compacted):
                continue
            for start in range(0, len(compacted) - size + 1):
                window = compacted[start : start + size]
                if SequenceMatcher(None, window, term_compact).ratio() >= 0.88:
                    corrections.append(term)
                    matched_in_pass.add(term)
                    corrected = f"{corrected} ({term})"
                    compacted = _compact(corrected)
                    found = True
                    break
            if found:
                break

    return corrected, list(dict.fromkeys(corrections))


def _normalize_dates_and_numbers(text: str, now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    normalized = text

    def replace_type(match: re.Match) -> str:
        value = _korean_number_to_int(match.group(1))
        if value in {1, 2}:
            return f"{value}유형"
        return match.group(0)

    normalized = re.sub(r"(?<![가-힣0-9])([일이12])\s*유형", replace_type, normalized)

    def replace_date(match: re.Match) -> str:
        year_raw, month_raw, day_raw = match.groups()
        year = _korean_number_to_int(year_raw) if not year_raw.isdigit() else int(year_raw)
        month = _korean_number_to_int(month_raw)
        day = _korean_number_to_int(day_raw)
        if year is None or month is None or day is None:
            return match.group(0)
        if year < 100:
            year += 2000
        return f"{year:04d}-{month:02d}-{day:02d}"

    normalized = re.sub(
        r"([0-9]{2,4}|[공영일이삼사오육칠팔구십]+)년\s*([0-9]{1,2}|[일이삼사오육칠팔구십]+)월\s*([0-9]{1,2}|[일이삼사오육칠팔구십]+)일",
        replace_date,
        normalized,
    )

    for label, week_offset in [("이번 주", 0), ("다음 주", 1)]:
        for weekday_name, weekday in WEEKDAYS.items():
            phrase = f"{label} {weekday_name}"
            if phrase in normalized:
                start_of_week = now - timedelta(days=now.weekday())
                target = start_of_week + timedelta(days=weekday, weeks=week_offset)
                normalized = normalized.replace(
                    phrase, f"{target.strftime('%Y-%m-%d')}({weekday_name})"
                )

    return normalized


def _normalize_short_utterance(text: str, history: list[dict[str, Any]]) -> Optional[str]:
    compacted = text.strip()
    topic = _last_topic(history)
    if not topic:
        return None
    if compacted in AFFIRMATIVE_SHORT:
        return f"이전 대화 맥락에 대한 긍정 응답입니다. 이전 맥락: {topic}"
    if compacted in NEGATIVE_SHORT:
        return f"이전 대화 맥락에 대한 부정 응답입니다. 이전 맥락: {topic}"
    if compacted in {"그거", "저거", "이거", "그건", "그럼"}:
        return f"다음 표현은 이전 대화 주제를 가리킵니다: '{compacted}'. 이전 맥락: {topic}"
    return None


_QUESTION_TAIL_RE = re.compile(
    r"(?:가요|나요|는가|던가|을까|ㄹ까|냐고|니까|어요|예요|이에요|있나요|있어요|돼요|되나요|할까요|할까|을까요|일까요)\s*[?.!]?\s*$"
)
_DECLARATIVE_TAIL_RE = re.compile(
    r"(?:다|네|구나|이지|이야|는데|던데|군|었어|랬어|랬는데|어요|예요)\s*\.?\s*$"
)


def _directedness_score(
    text: str,
    history: list[dict[str, Any]],
    seconds_since_bot: Optional[float] = None,
) -> tuple[float, str]:
    """발화가 챗봇을 향한 것인지 점수 (0~1).

    설계 원칙: **챗봇 환경에서 사용자가 명시적으로 마이크를 켜고 말한 발화는
    기본적으로 directed 로 가정**. background 신호가 명확할 때만 감점.
    음성 모드는 사용자가 의도적으로 활성화하므로 false negative 비용이 더 큼.

    seconds_since_bot:
      - 없음/None: 영향 없음
      - <1.5s: 봇 자기 TTS 의 echo 가능성 → 큰 감점
      - 1.5~10s: 봇 응답 직후 대화 lock → 가점
      - >10s: 영향 없음
    """
    compacted = text.strip()
    if any(word in compacted for word in WAKE_WORDS):
        return 1.0, "wake_word"

    # 기본 점수 상향 — 마이크를 켜고 말한 시점부터 directed 로 가정
    score = 0.55

    if any(signal in compacted for signal in REQUEST_SIGNALS):
        score += 0.25

    # 의문형 강조 / 평서형 약한 감점
    if _QUESTION_TAIL_RE.search(compacted):
        score += 0.15
    elif _DECLARATIVE_TAIL_RE.search(compacted):
        if len(_compact(compacted)) > 4:
            score -= 0.10

    if compacted.endswith(("?", "까", "나요", "줘", "세요")):
        score += 0.05

    # 한국어 충분히 긴 문장은 directed 가능성 높음
    hangul_chars = sum(1 for c in compacted if "가" <= c <= "힣")
    if hangul_chars >= 5:
        score += 0.10
    if hangul_chars >= 12:
        score += 0.05

    if history:
        score += 0.05
    if any(signal in compacted for signal in BACKGROUND_SIGNALS):
        score -= 0.35
    if len(_compact(compacted)) <= 3 and compacted not in AFFIRMATIVE_SHORT | NEGATIVE_SHORT:
        score -= 0.25

    # 봇 응답 직후 echo / 대화 lock
    if seconds_since_bot is not None:
        if seconds_since_bot < 1.5:
            score -= 0.40
        elif seconds_since_bot < 10.0:
            score += 0.10

    score = max(0.0, min(1.0, score))
    reason = (
        "directed" if score >= 0.70 else "ambiguous" if score >= 0.40 else "background"
    )
    return score, reason


def _classify_intent(text: str) -> Optional[str]:
    for intent, keywords in INTENT_RULES:
        if any(keyword in text for keyword in keywords):
            return intent
    return None


def _estimate_confidence(text: str, directedness: float, stt_confidence: Optional[float]) -> float:
    if stt_confidence is not None:
        base = stt_confidence
    else:
        base = 0.76

    compacted = _compact(text)
    if len(compacted) <= 2:
        base -= 0.25
    elif len(compacted) <= 5:
        base -= 0.1
    if re.search(r"[?？]{2,}|[^\w\s가-힣.,?!~%-]", text):
        base -= 0.08

    base = (base * 0.72) + (directedness * 0.28)
    return round(max(0.0, min(1.0, base)), 2)


def understand_public_voice(
    transcript: str,
    history: list[dict[str, Any]],
    stt_confidence: Optional[float] = None,
    now: Optional[datetime] = None,
    seconds_since_bot: Optional[float] = None,
) -> VoiceUnderstanding:
    raw = re.sub(r"\s+", " ", (transcript or "").strip())
    if not raw:
        return VoiceUnderstanding(
            raw_text="",
            normalized_text="",
            action="clarify",
            confidence=0.0,
            directedness=0.0,
            confirmation="음성을 정확히 듣지 못했습니다. 다시 말씀해 주세요.",
            reason="empty_transcript",
        )

    short_context = _normalize_short_utterance(raw, history)
    text = short_context or raw
    text, corrections = _apply_domain_corrections(text)
    text = _normalize_dates_and_numbers(text, now=now)

    directedness, directed_reason = _directedness_score(
        raw, history, seconds_since_bot=seconds_since_bot
    )
    confidence = _estimate_confidence(text, directedness, stt_confidence)
    intent = _classify_intent(text)
    sensitive = any(signal in text for signal in SENSITIVE_SIGNALS)

    # 한국어가 충분히 들어간 발화는 ignore 안 함 (사용자가 명시적으로 마이크 켜고
    # 말한 거니까 STT 결과가 한국어로 깔끔히 잡히면 그대로 응답으로 처리).
    hangul_chars = sum(1 for c in raw if "가" <= c <= "힣")
    if directedness < 0.30 and hangul_chars < 3 and not short_context:
        return VoiceUnderstanding(
            raw_text=raw,
            normalized_text=text,
            action="ignore",
            confidence=confidence,
            directedness=directedness,
            intent=intent,
            reason=directed_reason,
            corrections=corrections,
        )

    if confidence < 0.6:
        return VoiceUnderstanding(
            raw_text=raw,
            normalized_text=text,
            action="clarify",
            confidence=confidence,
            directedness=directedness,
            intent=intent,
            confirmation="음성을 정확히 이해하지 못했습니다. 다시 한번 말씀해 주세요.",
            reason="low_confidence",
            corrections=corrections,
        )

    if confidence < 0.8 or sensitive or intent in {"apply", "cancel", "eligibility"}:
        if sensitive:
            confirmation = (
                "말씀하신 내용은 부정수급이나 허위 신청과 관련된 민감한 문의로 이해했습니다. "
                "정확한 절차 안내가 필요하니 이 내용으로 확인해 드릴까요?"
            )
        elif intent == "eligibility":
            confirmation = f"말씀하신 내용은 자격요건 확인 문의로 이해했습니다. 맞으실까요?"
        elif intent == "apply":
            confirmation = f"말씀하신 내용은 신청 방법 문의로 이해했습니다. 맞으실까요?"
        elif intent == "cancel":
            confirmation = f"말씀하신 내용은 취소나 중단 절차 문의로 이해했습니다. 맞으실까요?"
        else:
            confirmation = f"제가 이해한 내용이 맞다면 '{text}'에 대한 문의입니다. 맞으실까요?"

        return VoiceUnderstanding(
            raw_text=raw,
            normalized_text=text,
            action="clarify",
            confidence=confidence,
            directedness=directedness,
            intent=intent,
            confirmation=confirmation,
            reason="needs_confirmation",
            corrections=corrections,
        )

    return VoiceUnderstanding(
        raw_text=raw,
        normalized_text=text,
        action="answer",
        confidence=confidence,
        directedness=directedness,
        intent=intent,
        reason=directed_reason,
        corrections=corrections,
    )
