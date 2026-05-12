"""LLM 기반 엔티티/관계 추출 — Semantica Korean NER fallback 대신 gpt-4o-mini.

목적: 크롤된 페이지에서 도청 도메인에 의미있는 엔티티(기관/사람/장소/연락처/
정책명/사업명/금액/날짜)와 관계를 뽑아 Neo4j 에 적재. 멀티홉 질의(예: "○○
부서의 사업 중 예산이 가장 큰 건?")에 graph 탐색이 활용되도록.

설계 원칙:
- httpx async, 환경변수 우선 (NO request.state 의존) — crawler worker 가 호출.
- JSON 모드(response_format=json_object) 로 응답 파싱 단순화.
- 비용 관리: content_preview 앞 ~3000자만 → 페이지당 LLM 호출 1회 (~$0.0003).
- 실패 graceful — Neo4j 적재는 None / 빈 list 로 처리.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import httpx

log = logging.getLogger(__name__)

# 추출 대상 최대 텍스트 길이 — 토큰 비용 관리 + LLM 컨텍스트 안정성.
MAX_EXTRACT_CHARS = 3000


def _get_llm_config() -> Optional[tuple[str, str, str]]:
    """(base_url, api_key, model) — 환경변수 우선, 없으면 PG config 에서 0번째.

    환경변수: GRAPHRAG_LLM_BASE_URL / GRAPHRAG_LLM_API_KEY / GRAPHRAG_LLM_MODEL
    """
    base = os.environ.get("GRAPHRAG_LLM_BASE_URL")
    key = os.environ.get("GRAPHRAG_LLM_API_KEY")
    model = os.environ.get("GRAPHRAG_LLM_MODEL") or "gpt-4o-mini"
    if base and key:
        return base.rstrip("/"), key, model

    # PG config 폴백 — 0번째 openai endpoint
    try:
        import psycopg2

        conn_str = os.environ.get(
            "DATABASE_URL",
            "postgresql://admin:sprint26%21@localhost:5432/customui",
        )
        # SQLAlchemy URL → psycopg2 인자
        conn = psycopg2.connect(conn_str)
        cur = conn.cursor()
        cur.execute("SELECT data FROM config ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return None
        cfg = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        o = cfg.get("openai", {})
        urls = o.get("api_base_urls") or []
        keys = o.get("api_keys") or []
        if not urls or not keys or not keys[0]:
            return None
        return urls[0].rstrip("/"), keys[0], model
    except Exception as e:
        log.debug(f"_get_llm_config DB fallback failed: {e}")
        return None


_EXTRACT_PROMPT = """다음은 전북도청 또는 직속기관 홈페이지에서 크롤된 본문 일부입니다. 도민에게 유용한 정보 검색용 지식그래프를 만들기 위해 핵심 엔티티와 관계를 JSON으로 추출하세요.

지침:
1) entities 배열에 5~25개의 엔티티만 골라. 의미 있는 명사구만.
   type 은 다음 중 하나: ORG(기관/부서/회사), PERSON(사람), LOCATION(주소/장소),
   PHONE(전화번호), EMAIL, URL, POLICY(정책/사업/제도/공고), DATE(날짜),
   MONEY(금액), QUALIFICATION(자격조건), OTHER.
2) relations 배열에 명시적으로 본문에 등장한 관계만 5~30개. 추측 금지.
   predicate 는 자연스러운 한국어 짧은 술어 (예: "담당", "대표전화", "위치",
   "신청기간", "예산", "자격요건", "주관기관", "관련사업").
3) 출력은 반드시 다음 형식의 JSON 만:
   {"entities":[{"name":"...","type":"..."}],"relations":[{"subject":"...","predicate":"...","object":"..."}]}
   entities 내 name 은 unique. relations 의 subject/object 는 entities 의 name 과 정확히 일치.

# 제목
{title}

# 본문
{text}
"""


async def extract_entities_llm(
    title: str,
    text: str,
    timeout: float = 30.0,
) -> Optional[dict]:
    """LLM 호출 → {entities:[...], relations:[...]} 반환. 실패 시 None.

    호출자: crawler.py 의 페이지 적재 직후.
    """
    if not text or not text.strip():
        return None
    cfg = _get_llm_config()
    if cfg is None:
        log.debug("GraphRAG LLM 미설정 — entity extraction 스킵")
        return None
    base_url, api_key, model = cfg

    prompt = _EXTRACT_PROMPT.replace("{title}", title or "")
    prompt = prompt.replace("{text}", text[:MAX_EXTRACT_CHARS])

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "max_tokens": 1200,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{base_url}/chat/completions", headers=headers, json=payload
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
        obj = json.loads(content)
        entities = obj.get("entities") or []
        relations = obj.get("relations") or []
        # name 정규화 — 빈 string / 너무 긴 것 제외
        entities = [
            {"name": (e.get("name") or "").strip(), "type": (e.get("type") or "OTHER").strip()}
            for e in entities
            if isinstance(e, dict) and (e.get("name") or "").strip()
            and len((e.get("name") or "")) <= 200
        ]
        valid_names = {e["name"] for e in entities}
        relations = [
            {
                "subject": (r.get("subject") or "").strip(),
                "predicate": (r.get("predicate") or "").strip(),
                "object": (r.get("object") or "").strip(),
            }
            for r in relations
            if isinstance(r, dict)
            and (r.get("subject") or "").strip() in valid_names
            and (r.get("object") or "").strip() in valid_names
            and (r.get("predicate") or "").strip()
        ]
        return {"entities": entities, "relations": relations}
    except Exception as e:
        log.debug(f"extract_entities_llm failed: {type(e).__name__}: {e}")
        return None
