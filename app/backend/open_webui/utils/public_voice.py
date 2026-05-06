import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Optional


DOMAIN_TERMS = [
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
]

DOMAIN_REPLACEMENTS = [
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
    corrected = text
    corrections: list[str] = []

    for pattern, replacement in DOMAIN_REPLACEMENTS:
        next_text = re.sub(pattern, replacement, corrected, flags=re.IGNORECASE)
        if next_text != corrected:
            corrections.append(replacement)
            corrected = next_text

    compacted = _compact(corrected)
    for term in DOMAIN_TERMS:
        term_compact = _compact(term)
        if term_compact in compacted:
            continue

        for size in range(max(2, len(term_compact) - 2), len(term_compact) + 3):
            for start in range(0, max(0, len(compacted) - size) + 1):
                window = compacted[start : start + size]
                if SequenceMatcher(None, window, term_compact).ratio() >= 0.88:
                    corrections.append(term)
                    corrected = f"{corrected} ({term})"
                    compacted = _compact(corrected)
                    break
            else:
                continue
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


def _directedness_score(text: str, history: list[dict[str, Any]]) -> tuple[float, str]:
    compacted = text.strip()
    if any(word in compacted for word in WAKE_WORDS):
        return 1.0, "wake_word"

    score = 0.35
    if any(signal in compacted for signal in REQUEST_SIGNALS):
        score += 0.35
    if compacted.endswith(("?", "요", "까", "나요", "줘", "세요")):
        score += 0.15
    if history:
        score += 0.1
    if any(signal in compacted for signal in BACKGROUND_SIGNALS):
        score -= 0.35
    if len(_compact(compacted)) <= 3 and compacted not in AFFIRMATIVE_SHORT | NEGATIVE_SHORT:
        score -= 0.25

    score = max(0.0, min(1.0, score))
    reason = "directed" if score >= 0.75 else "ambiguous" if score >= 0.45 else "background"
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

    directedness, directed_reason = _directedness_score(raw, history)
    confidence = _estimate_confidence(text, directedness, stt_confidence)
    intent = _classify_intent(text)
    sensitive = any(signal in text for signal in SENSITIVE_SIGNALS)

    if directedness < 0.4 and not short_context:
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
