"""TTS 입력 텍스트 한국어 발음 변환.

LLM 답변 텍스트를 그대로 Qwen3-TTS(Sohee)에 보내면 숫자·전화번호·URL 같은
부분에서 영어식/어색한 발음이 나온다. 한국어 자연 발음으로 변환하는 보조
함수 모음. voice_ws.py 의 _generate_reply 가 sentence 별로 호출.

호출 그래프 (코드 리뷰 시 따라가기 좋은 순서):
  tts_text_postprocess(text)
    ├─ _digits_to_kor(s)              ← KOR_DIGITS 직접 매핑 ("123" → "일이삼")
    └─ int_to_sino(n)                 ← 0~10^12 정수 → 한자어 ("45" → "사십오")
"""
from __future__ import annotations

import re

KOR_DIGITS: dict[str, str] = {
    "0": "공", "1": "일", "2": "이", "3": "삼", "4": "사",
    "5": "오", "6": "육", "7": "칠", "8": "팔", "9": "구",
}
KOR_SINO: list[str] = ["", "일", "이", "삼", "사", "오", "육", "칠", "팔", "구"]


def int_to_sino(n: int) -> str:
    """정수 → 한자어 한국어 발음 (0~10^12 범위, 그 이상은 str 그대로).

    예: 45 → 사십오, 2114 → 이천일백일십사, 100 → 일백, 22 → 이십이.
    TTS 가 "45" 를 "포티 파이브" 또는 "사오" 로 어색하게 읽는 걸 자연스럽게.
    """
    if n == 0:
        return "영"
    if n < 10:
        return KOR_SINO[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        s = ("" if tens == 1 else KOR_SINO[tens]) + "십"
        if ones:
            s += KOR_SINO[ones]
        return s
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        s = KOR_SINO[hundreds] + "백"
        if rest:
            s += int_to_sino(rest)
        return s
    if n < 10000:
        thousands, rest = divmod(n, 1000)
        s = KOR_SINO[thousands] + "천"
        if rest:
            s += int_to_sino(rest)
        return s
    if n < 10**8:
        man, rest = divmod(n, 10000)
        s = int_to_sino(man) + "만"
        if rest:
            s += " " + int_to_sino(rest)
        return s
    if n < 10**12:
        eok, rest = divmod(n, 10**8)
        s = int_to_sino(eok) + "억"
        if rest:
            s += " " + int_to_sino(rest)
        return s
    return str(n)  # 1조 이상은 그대로


# 단위 동반 숫자는 자연 한국어 발음. 미동반 단독 숫자만 한자어 변환.
_UNIT_KO = (
    "세", "살", "원", "년", "월", "일", "시", "분", "초", "명", "개",
    "층", "호", "회", "차", "번", "쪽", "건", "위", "급", "도", "%",
)


def tts_text_postprocess(text: str) -> str:
    """TTS 입력 텍스트 후처리 — 자막은 원본 그대로, TTS 만 한국어 발음으로 변환.

    변환 규칙 (순서대로 적용):
      1) 전화번호 (063-280-2114) → "공육삼에 이팔공에 이일일사"
      2) URL → "홈페이지" (Sohee 가 영문 알파벳 못 읽음)
      3) 백분율 (45%) → "사십오 퍼센트"
      4) 콤마 숫자 (1,234,567) → 콤마 제거 후 한자어 자연 발음
      5) 5자리 이상 단독 숫자 → 자릿수 발음 (코드/식별자)
      6) 단위 미동반 1~4자리 단독 숫자 → 한자어 (영어식 "twenty-five" 차단)

    너무 광범위하게 변환하면 LLM 답변 의도에서 멀어질 수 있어, 자주 어색한
    패턴만 명시적으로 처리.
    """
    def _digits_to_kor(d: str) -> str:
        return "".join(KOR_DIGITS.get(c, c) for c in d)

    # 1. 전화번호
    def _repl_phone(m):
        groups = m.group(0).split("-")
        return "에 ".join(_digits_to_kor(g) for g in groups)
    text = re.sub(r"\b\d{2,4}-\d{3,4}-\d{4}\b", _repl_phone, text)
    text = re.sub(r"\b\d{2,4}-\d{2,4}\b", _repl_phone, text)

    # 2. URL
    text = re.sub(r"https?://\S+", "홈페이지", text)

    # 3. 백분율
    def _repl_pct(m):
        return f"{int_to_sino(int(m.group(1)))} 퍼센트"
    text = re.sub(r"(\d{1,4})\s*%", _repl_pct, text)
    text = re.sub(r"%", " 퍼센트 ", text)

    # 4. 콤마 숫자 — 콤마만 제거
    text = re.sub(r"\b\d{1,3}(?:,\d{3})+\b", lambda m: m.group(0).replace(",", ""), text)

    # 5. 5자리 이상 단독 숫자
    text = re.sub(r"\b\d{5,}\b", lambda m: _digits_to_kor(m.group(0)), text)

    # 6. 단위 미동반 1~4자리 단독 숫자
    def _repl_short_num(m):
        end = m.end()
        rest = text[end:end + 1] if end < len(text) else ""
        if rest in _UNIT_KO:
            return m.group(0)
        return int_to_sino(int(m.group(0)))
    text = re.sub(r"(?<![\d.\-])\d{1,4}(?![\d.\-])", _repl_short_num, text)

    return text
