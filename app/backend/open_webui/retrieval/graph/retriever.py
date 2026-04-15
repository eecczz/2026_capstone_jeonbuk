"""
GraphRAG 증강 검색 — 쿼리에서 엔티티를 인식해 그래프 이웃을 확장하고
그 이웃이 언급된 청크 id를 벡터 검색 결과에 bonus로 주입한다.

동작 흐름:
1. 쿼리 텍스트에서 등장 가능한 엔티티를 LLM 또는 rule-based로 추출
2. entity 테이블에서 canonical_name/aliases로 매칭
3. entity_relation을 따라 1-2 hop 이웃 확장
4. entity_mention으로 각 이웃이 언급된 chunk_id 수집
5. 이 chunk_id들을 bonus score와 함께 반환

공개 챗봇은 기존 벡터 RAG 결과와 이 bonus chunk를 합쳐서 LLM에 컨텍스트로 전달.

의존성:
- open_webui.models.entity.Entities / Mentions / Relations
- SEED_INSTITUTIONS (extractor.py와 공유) → 기관명 fast path
"""

import logging
import re
from typing import Any, Optional

from open_webui.models.entity import Entities, Mentions, Relations
from open_webui.retrieval.graph.extractor import (
    SEED_INSTITUTIONS,
    normalize_institution,
)

log = logging.getLogger(__name__)


####################
# 쿼리 엔티티 인식 (rule-based fast path)
####################


def extract_query_entities(query: str) -> list[tuple[str, str]]:
    """
    쿼리에서 등장 가능한 엔티티를 rule-based 로 빠르게 추출.

    반환: [(entity_type, matched_name), ...]

    LLM 호출 없이 seed alias 사전 매칭으로 기관을 찾고,
    단어 정규화로 카테고리 후보를 찾는다.
    """
    results: list[tuple[str, str]] = []
    q = query.strip()
    if not q:
        return results

    # 1) 기관 seed alias 매칭
    for canon, aliases in SEED_INSTITUTIONS.items():
        if canon in q:
            results.append(("institution", canon))
            continue
        for a in aliases:
            if a and a in q:
                results.append(("institution", canon))
                break

    # 2) 카테고리 후보 (간단 키워드)
    category_keywords = [
        "교육",
        "농업",
        "보건환경",
        "산림",
        "문화예술",
        "관광",
        "행정",
        "일자리",
        "지원사업",
        "체험",
    ]
    for cat in category_keywords:
        if cat in q:
            results.append(("category", cat))

    return results


####################
# 그래프 확장 → 청크 수집
####################


def expand_to_chunk_ids(
    seed_entities: list[tuple[str, str]],
    max_hops: int = 2,
    max_chunks: int = 40,
) -> dict[str, float]:
    """
    시드 엔티티 이름을 entity 테이블에서 찾고, 1~max_hops hop까지 이웃을 확장,
    해당 이웃 엔티티들이 언급된 청크 id를 수집.

    반환: {chunk_id: bonus_score} — 홉 수에 반비례하는 점수
    """
    chunk_scores: dict[str, float] = {}
    visited_entities: set[str] = set()

    # hop 0: 시드 엔티티 본인 매칭
    frontier: list[str] = []  # entity_id
    for etype, name in seed_entities:
        matched = Entities.find_by_name_or_alias(etype, name, limit=3)
        for ent in matched:
            if ent.id not in visited_entities:
                frontier.append(ent.id)
                visited_entities.add(ent.id)

    if not frontier:
        return chunk_scores

    # 시드 자체의 mention 먼저 수집 (bonus 1.0)
    for eid in frontier:
        for cid in Mentions.get_chunks_by_entity(eid, limit=max_chunks):
            chunk_scores[cid] = max(chunk_scores.get(cid, 0), 1.0)
            if len(chunk_scores) >= max_chunks:
                return chunk_scores

    # 1..max_hops hop 확장
    for hop in range(1, max_hops + 1):
        next_frontier: list[str] = []
        decay = 1.0 / (hop + 1)  # hop1 → 0.5, hop2 → 0.33
        for eid in frontier:
            neighbors = Relations.neighbors(eid, direction="both", limit=20)
            for rel in neighbors:
                nb = rel.tail_id if rel.head_id == eid else rel.head_id
                if nb in visited_entities:
                    continue
                visited_entities.add(nb)
                next_frontier.append(nb)
                for cid in Mentions.get_chunks_by_entity(nb, limit=20):
                    chunk_scores[cid] = max(chunk_scores.get(cid, 0), decay)
                    if len(chunk_scores) >= max_chunks:
                        return chunk_scores
        frontier = next_frontier
        if not frontier:
            break

    return chunk_scores


####################
# 공개 API
####################


def graph_bonus_chunks(
    query: str, max_chunks: int = 20
) -> dict[str, float]:
    """
    쿼리로부터 그래프 추론으로 추가로 고려할 chunk_id + bonus score dict 반환.
    빈 dict면 그래프에서 매칭된 엔티티가 없거나 확장이 공전.
    호출자(공개 챗봇)는 이 결과를 벡터 검색 결과와 merge 해서 최종 컨텍스트를
    구성한다.
    """
    entities = extract_query_entities(query)
    if not entities:
        return {}
    log.info(
        f"graph retriever: query entities = {entities[:10]} "
        f"(total {len(entities)})"
    )
    return expand_to_chunk_ids(entities, max_hops=2, max_chunks=max_chunks)
