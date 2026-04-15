"""
GraphRAG 엔티티·관계 추출 파이프라인.

각 청크 텍스트를 LLM에게 넣어 구조화된 JSON(엔티티 + 트리플)을 받고,
정규화한 뒤 entity/entity_mention/entity_relation 테이블에 upsert 한다.

사용:
    await extract_and_store_entities(
        chunk_text, chunk_id, url, metadata, request
    )

특징:
- 기관 alias 시드 (`SEED_INSTITUTIONS`)로 정규화 오류 완화
- content_extraction용 LLM은 generate_chat_completion 재사용 (기존 파이프라인과 동일)
- graceful degradation: 실패 시 로그만 남기고 raise 안 함 (크롤링 진행은 계속)
- 동기 버전 (`extract_and_store_entities_sync`)도 제공 → asyncio.to_thread 호출에 적합
"""

import json
import logging
import re
from typing import Any, Optional

from open_webui.models.entity import Entities, Mentions, Relations

log = logging.getLogger(__name__)


####################
# 기관 별칭 시드 (정규화 오류 완화)
####################


SEED_INSTITUTIONS: dict[str, list[str]] = {
    "전북특별자치도": [
        "전북도",
        "전북",
        "전라북도",
        "전북특별자치도청",
        "전북도청",
    ],
    "전북특별자치도 농업기술원": [
        "농업기술원",
        "농기원",
        "전북 농업기술원",
        "전북농업기술원",
        "JBARES",
    ],
    "전북특별자치도 인재개발원": [
        "인재개발원",
        "전북 인재개발원",
        "전북인재개발원",
    ],
    "전북특별자치도 보건환경연구원": [
        "보건환경연구원",
        "전북 보건환경연구원",
    ],
    "전북특별자치도 산림환경연구원": [
        "산림환경연구원",
        "전북 산림환경연구원",
    ],
    "전북특별자치도립국악원": ["국악원", "도립국악원"],
    "전북특별자치도립미술관": ["미술관", "도립미술관", "JMA"],
    "전북특별자치도 어린이창의체험관": [
        "어린이창의체험관",
        "창의체험관",
    ],
    "전북특별자치도 농식품인력개발원": [
        "농식품인력개발원",
        "농식품 인력개발원",
    ],
    "전북특별자치도 경제통상진흥원": ["경제통상진흥원"],
    "투어전북": ["관광전북", "전북관광"],
}


####################
# LLM 프롬프트
####################


EXTRACTION_SYSTEM_PROMPT = """당신은 전북특별자치도청 및 직속기관 홈페이지 콘텐츠에서 구조화된 엔티티와 관계 트리플을 추출하는 전문가입니다.

입력된 텍스트를 읽고 다음 JSON 스키마로만 응답하세요 (앞뒤 설명 없이 JSON만):

{
  "entities": [
    {"type": "institution|program|person|audience|period|contact|location|category", "name": "표기된 이름", "aliases": []}
  ],
  "relations": [
    {"head": "엔티티 이름", "relation": "operates|staffs|targets|scheduled|contact_of|located_at|categorized|child_of", "tail": "엔티티 이름"}
  ]
}

관계 타입 가이드:
- operates      : 기관이 프로그램을 운영
- staffs        : 프로그램의 담당자
- targets       : 프로그램의 대상 (도민/공무원/농업인 등)
- scheduled     : 프로그램의 기간/일정
- contact_of    : 기관 또는 사람의 연락처 (전화/이메일)
- located_at    : 기관 또는 프로그램의 장소
- categorized   : 프로그램/기관의 카테고리 분류
- child_of      : 하위 기관이 상위 기관에 소속

주의:
- 명확하지 않으면 그 관계는 생략
- 엔티티가 본문에 없으면 추출하지 말 것
- name은 본문에 등장한 표기 그대로 (정규화는 이후 단계)
- 중복은 제거"""


def build_extraction_messages(chunk_text: str) -> list[dict]:
    return [
        {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"다음 텍스트에서 엔티티와 관계를 JSON으로 추출하세요.\n\n---\n{chunk_text[:3000]}\n---",
        },
    ]


####################
# 정규화 유틸
####################


def normalize_institution(raw: str) -> tuple[str, list[str]]:
    """기관명을 canonical + aliases 페어로 정규화.

    SEED_INSTITUTIONS의 각 canonical에 대해 alias 또는 canonical substring이
    포함되면 해당 canonical로 매핑한다.
    """
    raw_clean = raw.strip()
    if not raw_clean:
        return raw_clean, []
    # 완전 일치
    if raw_clean in SEED_INSTITUTIONS:
        return raw_clean, list(SEED_INSTITUTIONS[raw_clean])
    # alias 일치 또는 부분 매칭
    for canon, aliases in SEED_INSTITUTIONS.items():
        if raw_clean in aliases:
            return canon, [raw_clean]
        if raw_clean in canon or canon in raw_clean:
            return canon, [raw_clean]
        for a in aliases:
            if a and a in raw_clean:
                return canon, [raw_clean]
    return raw_clean, []


def normalize_category(raw: str) -> str:
    return raw.strip().replace(" ", "").lower() if raw else raw


####################
# 저장 헬퍼
####################


def _store_entity(
    type: str, raw_name: str, extra_meta: Optional[dict] = None
) -> Optional[str]:
    """엔티티를 upsert하고 id를 반환."""
    if not raw_name or not raw_name.strip():
        return None
    if type == "institution":
        canon, aliases = normalize_institution(raw_name)
    elif type == "category":
        canon = normalize_category(raw_name)
        aliases = [raw_name] if raw_name != canon else []
    else:
        canon = raw_name.strip()
        aliases = []
    ent = Entities.upsert_by_name(
        type=type,
        canonical_name=canon,
        aliases=aliases,
        meta=extra_meta,
    )
    return ent.id if ent else None


def _parse_llm_json(raw: str) -> Optional[dict]:
    """LLM 응답에서 JSON 블록을 추출. 앞뒤 마크다운 펜스를 제거."""
    if not raw:
        return None
    text = raw.strip()
    # ```json … ``` 펜스 제거
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        text = match.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # fallback: 첫 { … 마지막 } 추출
        first = text.find("{")
        last = text.rfind("}")
        if first >= 0 and last > first:
            try:
                return json.loads(text[first : last + 1])
            except Exception:
                return None
        return None


####################
# 메인 진입점 (LLM 호출 없는 pure-Python 버전)
####################


def store_extracted_payload(
    payload: dict,
    chunk_id: str,
    url: Optional[str] = None,
    confidence: float = 0.8,
    seed_institution: Optional[str] = None,
    seed_category: Optional[str] = None,
) -> dict:
    """
    이미 LLM이 반환한 JSON 페이로드를 받아서 DB에 저장.

    LLM 호출 자체는 호출자(크롤러)가 generate_chat_completion으로 직접 수행하고,
    결과만 이 함수로 넘긴다. 그래야 request/user 체인을 이 모듈에서 들고
    있을 필요가 없다.

    seed_institution / seed_category: 크롤러 metadata에서 알고 있는 기관/카테고리를
    무조건 엔티티로 등록하고, LLM이 추출한 program 엔티티와 자동 연결해 관계(operates/categorized)를 만든다.
    이 장치가 없으면 institution은 자주 누락되어 그래프가 파편화된다.
    """
    stats = {"entities": 0, "mentions": 0, "relations": 0}

    # 엔티티 → name → entity_id 맵
    name_to_id: dict[str, str] = {}

    # 1) 시드 institution/category 주입 (크롤러 metadata 출처)
    seed_inst_id: Optional[str] = None
    seed_cat_id: Optional[str] = None
    if seed_institution:
        seed_inst_id = _store_entity("institution", seed_institution)
        if seed_inst_id:
            Mentions.insert(
                entity_id=seed_inst_id,
                chunk_id=chunk_id,
                url=url,
                span=seed_institution,
                confidence=1.0,  # 메타데이터 기반이라 최고 신뢰
            )
            stats["entities"] += 1
            stats["mentions"] += 1
            name_to_id[seed_institution] = seed_inst_id
    if seed_category:
        seed_cat_id = _store_entity("category", seed_category)
        if seed_cat_id:
            Mentions.insert(
                entity_id=seed_cat_id,
                chunk_id=chunk_id,
                url=url,
                span=seed_category,
                confidence=1.0,
            )
            stats["entities"] += 1
            stats["mentions"] += 1
            name_to_id[seed_category] = seed_cat_id

    program_ids: list[str] = []  # 자동 연결용

    for ent in payload.get("entities", []):
        etype = (ent.get("type") or "").strip()
        name = (ent.get("name") or "").strip()
        if not etype or not name:
            continue
        entity_id = _store_entity(etype, name)
        if entity_id:
            name_to_id[name] = entity_id
            stats["entities"] += 1
            if etype == "program":
                program_ids.append(entity_id)
            # mention 기록
            m = Mentions.insert(
                entity_id=entity_id,
                chunk_id=chunk_id,
                url=url,
                span=name,
                confidence=confidence,
            )
            if m:
                stats["mentions"] += 1

    # 자동 관계 주입: seed institution이 있으면 모든 program을 operates 관계로 연결
    # seed category가 있으면 모든 program을 categorized 관계로 연결
    if seed_inst_id:
        for pid in program_ids:
            r = Relations.insert(
                head_id=seed_inst_id,
                relation_type="operates",
                tail_id=pid,
                source_chunk_id=chunk_id,
                confidence=1.0,
            )
            if r:
                stats["relations"] += 1
    if seed_cat_id:
        for pid in program_ids:
            r = Relations.insert(
                head_id=pid,
                relation_type="categorized",
                tail_id=seed_cat_id,
                source_chunk_id=chunk_id,
                confidence=1.0,
            )
            if r:
                stats["relations"] += 1

    for rel in payload.get("relations", []):
        head = (rel.get("head") or "").strip()
        tail = (rel.get("tail") or "").strip()
        rtype = (rel.get("relation") or "").strip()
        if not (head and tail and rtype):
            continue
        head_id = name_to_id.get(head)
        tail_id = name_to_id.get(tail)
        # 관계에 등장했지만 entities 배열에 안 들어온 이름은 기본 institution으로 저장 시도
        if not head_id:
            head_id = _store_entity("institution", head)
            if head_id:
                name_to_id[head] = head_id
        if not tail_id:
            tail_id = _store_entity("institution", tail)
            if tail_id:
                name_to_id[tail] = tail_id
        if head_id and tail_id:
            r = Relations.insert(
                head_id=head_id,
                relation_type=rtype,
                tail_id=tail_id,
                source_chunk_id=chunk_id,
                confidence=confidence,
            )
            if r:
                stats["relations"] += 1

    return stats


def parse_and_store(
    llm_raw_output: str,
    chunk_id: str,
    url: Optional[str] = None,
    confidence: float = 0.8,
    seed_institution: Optional[str] = None,
    seed_category: Optional[str] = None,
) -> dict:
    """LLM raw 문자열 출력을 parse 하고 저장."""
    payload = _parse_llm_json(llm_raw_output)
    if payload is None:
        log.warning(
            f"extractor: failed to parse LLM output for chunk={chunk_id}"
        )
        # 시드 institution/category 는 LLM 실패해도 등록하도록 빈 payload 라도 넘김
        if seed_institution or seed_category:
            return store_extracted_payload(
                {"entities": [], "relations": []},
                chunk_id,
                url,
                confidence,
                seed_institution=seed_institution,
                seed_category=seed_category,
            )
        return {"entities": 0, "mentions": 0, "relations": 0, "parse_failed": True}
    return store_extracted_payload(
        payload,
        chunk_id,
        url,
        confidence,
        seed_institution=seed_institution,
        seed_category=seed_category,
    )
