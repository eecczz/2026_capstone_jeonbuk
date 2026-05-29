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


# 단독 영문 약어 한국어 발음 매핑. dict 에 없으면 글자별 spell-out.
# Sohee 가 "KTX" → "케이 티 엑스" 가 아니라 영어식 "케이티엑스" 로 읽거나
# 통째로 영어 발음 시도해 한국어 음성 톤이 깨지는 문제 차단.
_ABBR_KO: dict[str, str] = {
    "AI": "에이아이", "KTX": "케이티엑스", "SRT": "에스알티",
    "USB": "유에스비", "GDP": "지디피", "GPS": "지피에스",
    "TV": "티비", "PC": "피씨", "OS": "오에스", "API": "에이피아이",
    "ID": "아이디", "PR": "피알", "QR": "큐알", "PDF": "피디에프",
    "URL": "유알엘", "FAQ": "에프에이큐", "OK": "오케이",
    "CCTV": "씨씨티비", "ATM": "에이티엠", "DNA": "디엔에이",
}
_ALPHA_KO: dict[str, str] = {
    "A": "에이", "B": "비", "C": "씨", "D": "디", "E": "이",
    "F": "에프", "G": "지", "H": "에이치", "I": "아이", "J": "제이",
    "K": "케이", "L": "엘", "M": "엠", "N": "엔", "O": "오",
    "P": "피", "Q": "큐", "R": "알", "S": "에스", "T": "티",
    "U": "유", "V": "브이", "W": "더블유", "X": "엑스", "Y": "와이",
    "Z": "제트",
}


def tts_text_postprocess(text: str) -> str:
    """TTS 입력 텍스트 후처리 — 자막은 원본 그대로, TTS 만 한국어 발음으로 변환.

    변환 규칙 (순서대로 적용):
      1) 전화번호 (063-280-2114) → "공육삼에 이팔공에 이일일사"
      2) URL → "홈페이지" (Sohee 가 영문 알파벳 못 읽음)
      3) 백분율 (45%) → "사십오 퍼센트"
      4) 콤마 숫자 (1,234,567) → 콤마 제거 후 한자어 자연 발음
      5) 5자리 이상 단독 숫자 → 자릿수 발음 (코드/식별자)
      6) 1~4자리 숫자 → 한자어 (단위 동반 "25세" 도 "이십오세" — Sohee 가
         단위 동반 숫자도 "twenty-five 세" 영어로 읽는 케이스 차단)
      7) 영문 약어 (KTX, USB 등) → 한국어 음. 매핑 없으면 글자별 spell-out

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

    # 4. 콤마 숫자 — 콤마만 제거. \b 는 한국어 인접 ('1,234원') 에서 매치 실패하므로
    # lookbehind/ahead 부정으로.
    text = re.sub(
        r"(?<!\d)\d{1,3}(?:,\d{3})+(?!\d)",
        lambda m: m.group(0).replace(",", ""),
        text,
    )

    # 5. 5자리 이상 단독 숫자
    text = re.sub(r"(?<!\d)\d{5,}(?!\d)", lambda m: _digits_to_kor(m.group(0)), text)

    # 6. 1~4자리 숫자 — 단위 동반/미동반 모두 한자어 변환
    text = re.sub(
        r"(?<![\d.\-])\d{1,4}(?![\d.\-])",
        lambda m: int_to_sino(int(m.group(0))),
        text,
    )

    # 7. 영문 약어 spell-out (단독 대문자 2~6자)
    def _repl_abbr(m):
        s = m.group(0)
        if s in _ABBR_KO:
            return _ABBR_KO[s]
        return "".join(_ALPHA_KO.get(c, c) for c in s)
    text = re.sub(r"\b[A-Z]{2,6}\b", _repl_abbr, text)

    return text
