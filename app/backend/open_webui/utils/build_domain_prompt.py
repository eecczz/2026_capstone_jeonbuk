"""
크롤링된 페이지/문서 데이터에서 도메인 vocabulary 를 추출해 Whisper STT 의
initial_prompt 로 사용할 텍스트를 생성한다.

흐름:
1. crawled_page.title 에서 한국어 명사구 추출 (path separator '>' 분리)
2. Chroma sqlite 의 chunk 텍스트에서 추가 어휘 추출 (옵션)
3. 빈도 기반 정렬 + dedupe + stopword 제거
4. Whisper 입력 한도 (~224 token, ~700 한국어 자) 안에 들어가도록 잘라
   /data/owi/cache/whisper_domain_prompt.txt 로 저장

호출:
  python3 -m open_webui.utils.build_domain_prompt
또는 cron 으로 일별 재생성.

음성챗봇 voice-chat 호출 시 audio.py 가 이 파일 내용을 faster-whisper 의
`initial_prompt` 로 넘긴다 — 도메인 어휘에 디코딩 가중치가 자동 적용되어
"전북특별자치도", "추경예산" 등이 비슷한 발음의 다른 단어보다 우선 선택됨.
"""

import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable

import psycopg2

# 출력 경로 (영구 볼륨)
OUTPUT_PROMPT_PATH = "/data/owi/cache/whisper_domain_prompt.txt"
OUTPUT_VOCAB_PATH = "/data/owi/cache/domain_vocab.json"

# Whisper initial_prompt 한도 — 224 토큰 ≈ 한국어 ~600-800 자
MAX_PROMPT_CHARS = 700
# Fuzzy 매칭용 vocab 상한 (성능 한계, 너무 많으면 비교 비용 ↑)
MAX_VOCAB_TERMS = 800

# 일반 한국어 동사/조사/형용사 — 도메인 어휘로 가치 없음.
# 공개 페이지에 흔히 등장하지만 STT 어휘 hint 로는 noise.
STOPWORDS = {
    # UI/네비게이션 일반어
    "이용안내", "로그인", "회원가입", "사이트맵", "검색", "다음", "이전",
    "목록", "보기", "페이지", "메인", "메뉴", "홈", "공지사항",
    "리스트", "글", "글번호", "제목", "내용", "본문", "바로가기",
    "전북특별자치도", "전북도청", "도청", "전북", "전라북도", "대표",
    "정보", "안내", "이용", "확인", "조회", "JEONBUK", "STATE",
    "패밀리사이트", "사이트도우미", "누리집", "게시판", "게시물",
    "찾아오시는길", "인사말", "주요업무",
    # 일반 동사 활용형 / 어미 / 조사
    "있습니다", "없습니다", "합니다", "됩니다", "드립니다", "하세요",
    "주세요", "있어요", "없어요", "있는", "없는", "있도록", "있었",
    "되었", "되어", "하여", "하는", "되는", "되며", "되었습니다",
    "이를", "이는", "그리고", "또한", "하지만", "위해", "통해", "따라",
    "다음의", "다음과", "위와", "아래", "기존", "현재", "최근", "관련",
    "이번", "이번년도", "올해", "지난해", "올해의", "다른",
    "작성", "사용", "방법", "내용을", "내용은", "내용의",
    # 페이지 보일러플레이트
    "페이지를", "찾을", "수가", "메세지", "메시지", "요청하신", "받으신",
    "성공시대를", "열어가는", "시작으로", "새로운",
    # 너무 일반적인 한자어
    "센터", "본부", "지원", "사업", "업무", "운영", "관리", "검토",
    "신청", "접수", "처리", "발표", "공고",  # 이건 나중에 의도분류에서 따로 처리
    "현황", "통계", "결과", "공시", "문의",
    "분야별", "분야", "유형별",
    # 기타
    "외국어", "분과별", "그룹", "기타",
}


def _extract_korean_phrases(text: str) -> Iterable[str]:
    """한국어 명사구 추출 — 한글 2글자 이상 연속.

    Whisper 디코딩에 도움 되려면 actual word boundaries 가 중요하므로
    구분자 (>, |, ·, /, 공백 등) 로 자른 후 각 토큰을 한글만 필터.
    """
    if not text:
        return []
    # 일반적인 메뉴 구분자 통일
    text = re.sub(r"[>|·/\\]", " ", text)
    # 한글 2자 이상 연속만 추출
    tokens = re.findall(r"[가-힣]{2,}", text)
    return tokens


def _harvest_from_postgres(conn) -> Counter:
    """crawled_page.title 에서 명사 빈도 카운트."""
    counter: Counter = Counter()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT title FROM crawled_page WHERE title IS NOT NULL"
        )
        for (title,) in cur:
            for tok in _extract_korean_phrases(title):
                counter[tok] += 1
    return counter


def _harvest_from_chroma() -> Counter:
    """Chroma sqlite 의 chunk text 에서 명사 빈도 카운트.

    크롤링 컬렉션 (crawl-*) 만 대상. file-* 는 일반 사용자 업로드라 도메인 어휘
    풍부도 차이 큼 — 도청 도메인 vocab 만 신호로 쓰는 게 맞음.
    """
    counter: Counter = Counter()
    chroma_path = "/data/owi/vector_db/chroma.sqlite3"
    if not Path(chroma_path).is_file():
        return counter
    import sqlite3
    con = sqlite3.connect(chroma_path)
    cur = con.cursor()
    try:
        cur.execute(
            """SELECT em.string_value FROM embedding_metadata em
               JOIN embeddings e ON e.id=em.id
               JOIN segments s ON s.id=e.segment_id
               JOIN collections c ON c.id=s.collection
               WHERE em.key='chroma:document' AND c.name LIKE 'crawl-%'
               LIMIT 10000"""
        )
        for (doc,) in cur:
            for tok in _extract_korean_phrases(doc or ""):
                counter[tok] += 1
    finally:
        con.close()
    return counter


def _build_prompt(counter: Counter, max_chars: int = MAX_PROMPT_CHARS) -> str:
    """가장 freq 높은 도메인 명사를 prompt 에 채워 넣음.

    제외 기준:
    - STOPWORDS
    - 1~2글자 (정보량 낮음)
    - 너무 흔한 (>20% 빈도) 또는 너무 희귀한 (1회) 단어
    """
    total = sum(counter.values()) or 1
    candidates = []
    seen = set()
    for token, freq in counter.most_common():
        if token in STOPWORDS:
            continue
        if len(token) < 3:
            continue
        if token in seen:
            continue
        # 너무 흔한 토큰 (메뉴 prefix 류) — 비율 기준
        if freq > total * 0.15:
            continue
        seen.add(token)
        candidates.append(token)

    # Whisper prompt 는 자연스러운 문장이 더 잘 작동.
    # 첫 문장에 도청 정중 어조 hint, 이후 어휘 콤마 나열.
    header = (
        "안녕하세요. 전북특별자치도청에 문의드립니다. "
        "전북특별자치도, 전북도청. 자주 등장하는 어휘는 "
    )
    body_parts = []
    used = len(header) + 2  # final period
    for term in candidates:
        if used + len(term) + 2 > max_chars:
            break
        body_parts.append(term)
        used += len(term) + 2

    body = ", ".join(body_parts)
    return f"{header}{body}."


def _build_vocab_list(counter: Counter, max_terms: int = MAX_VOCAB_TERMS) -> list[str]:
    """Fuzzy 매칭용 vocab 리스트 (Top N, dedupe + stopword)."""
    total = sum(counter.values()) or 1
    out = []
    seen = set()
    for token, freq in counter.most_common():
        if token in STOPWORDS:
            continue
        if len(token) < 3:
            continue
        if freq > total * 0.10:  # 너무 흔한 (>10%) 제외 — 메뉴 prefix
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= max_terms:
            break
    return out


def main() -> None:
    import json as _json
    db_url = os.environ.get(
        "DATABASE_URL", "postgresql://admin:sprint26!@localhost:5432/customui"
    )
    print(f"connecting to {db_url[:60]}...")
    conn = psycopg2.connect(db_url)
    try:
        print("harvesting crawled_page.title ...")
        c1 = _harvest_from_postgres(conn)
        print(f"  unique tokens (postgres): {len(c1)}")

        print("harvesting chroma chunks ...")
        c2 = _harvest_from_chroma()
        print(f"  unique tokens (chroma):   {len(c2)}")

        merged: Counter = Counter()
        for tok, n in c1.items():
            merged[tok] += n
        for tok, n in c2.items():
            merged[tok] += int(n * 0.3)
        print(f"  total unique: {len(merged)}")

        # 1) STT initial_prompt (짧은 텍스트)
        prompt = _build_prompt(merged)
        Path(OUTPUT_PROMPT_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(OUTPUT_PROMPT_PATH).write_text(prompt, encoding="utf-8")
        print(f"  prompt 길이: {len(prompt)} chars -> {OUTPUT_PROMPT_PATH}")

        # 2) Fuzzy 매칭용 vocab JSON (top N)
        vocab = _build_vocab_list(merged)
        Path(OUTPUT_VOCAB_PATH).write_text(
            _json.dumps(vocab, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"  vocab terms: {len(vocab)} -> {OUTPUT_VOCAB_PATH}")

        print()
        print("=== prompt 미리보기 (앞 400자) ===")
        print(prompt[:400])
        print()
        print("=== vocab 샘플 (앞 30개) ===")
        print(vocab[:30])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
