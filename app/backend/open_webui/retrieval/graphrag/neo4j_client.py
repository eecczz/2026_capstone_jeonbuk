"""Neo4j 연결 관리 — 도청 GraphRAG 의 진입점.

도청 운영 인프라:
- bolt://host.docker.internal:7687 (Neo4j 5.26 community)
- user/password 는 환경변수 또는 PersistentConfig 에서 로드

설계:
- 워커당 단일 Neo4jStore 인스턴스 (lazy 초기화 + 재사용)
- crawler worker / voice_ws / 검색 routine 모두 같은 store 활용
- 종료 시 (앱 shutdown) close

스키마 (단순):
- (Page {url, title, site_code, crawled_at})
- (Chunk {id, text, source_url}) -[:OF_PAGE]-> (Page)
- (Entity {name, kind}) <-[:MENTIONS]- (Chunk)
- (Entity)-[:RELATED_TO {type, source_chunk}]-(Entity)

Semantica 의 semantic_extract 가 LLM 으로 엔티티/관계를 뽑아오면 위 schema 에
저장. 본격 통합은 단계적 — 이 모듈은 우선 안정적인 연결 + 기본 CRUD 만.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

log = logging.getLogger(__name__)


def _get_creds() -> tuple[str, str, str]:
    """Neo4j 연결 정보 — 환경변수 우선, 디폴트는 도청 인프라."""
    uri = os.environ.get("NEO4J_URI") or "bolt://host.docker.internal:7687"
    user = os.environ.get("NEO4J_USER") or "neo4j"
    password = os.environ.get("NEO4J_PASSWORD") or "semantica2026!"
    return uri, user, password


_neo4j_store_singleton: Optional[Any] = None


def get_neo4j_store():
    """단일 Neo4jStore 인스턴스를 lazy 생성 + 재사용.

    Semantica.graph_store.Neo4jStore 활용. 처음 호출 시 connect() 까지 시도.
    실패하면 None 반환 (호출자가 graceful 처리).
    """
    global _neo4j_store_singleton
    if _neo4j_store_singleton is not None:
        return _neo4j_store_singleton

    try:
        from semantica.graph_store import Neo4jStore
    except ImportError as e:
        log.warning(f"semantica 미설치 — GraphRAG 비활성: {e}")
        return None

    uri, user, password = _get_creds()
    try:
        store = Neo4jStore(uri=uri, user=user, password=password)
        ok = store.connect(uri=uri, user=user, password=password)
        if not ok:
            log.warning(f"Neo4j connect 반환 False: {uri}")
            return None
        _neo4j_store_singleton = store
        log.info(f"Neo4jStore 연결 OK: {uri}")
        return store
    except Exception as e:
        log.exception(f"Neo4jStore 초기화 실패 {uri}: {e}")
        return None


def close_neo4j_store():
    """앱 종료 시 정리. lifespan shutdown hook 에서 호출."""
    global _neo4j_store_singleton
    if _neo4j_store_singleton is not None:
        try:
            _neo4j_store_singleton.close()
        except Exception as e:
            log.debug(f"Neo4jStore close error: {e}")
        _neo4j_store_singleton = None


def ensure_schema():
    """Page / Chunk / Entity 노드의 자주 쓰는 인덱스 + 한국어 fulltext 인덱스.

    이미 있으면 Neo4j 가 무시 — 매 worker 시작 시 호출 가능 (idempotent).
    """
    store = get_neo4j_store()
    if store is None:
        return False
    try:
        store.create_index("Page", "url", index_type="btree")
        store.create_index("Chunk", "id", index_type="btree")
        store.create_index("Entity", "name", index_type="btree")
    except Exception as e:
        log.debug(f"Neo4j btree index warning: {e}")
    # 한국어 fulltext 인덱스 — Page.title + content_preview 키워드 검색용
    try:
        store.execute_query(
            "CREATE FULLTEXT INDEX page_text IF NOT EXISTS "
            "FOR (n:Page) ON EACH [n.title, n.content_preview]"
        )
    except Exception as e:
        log.debug(f"Neo4j fulltext index warning (might already exist): {e}")
    return True


def search_pages_by_text(query: str, limit: int = 5) -> list[dict]:
    """fulltext query 로 관련 Page 검색. RAG 단에서 graph 컨텍스트 추가용.

    Returns: [{url, title, content_preview, score, institution, category}, ...]
    """
    store = get_neo4j_store()
    if store is None or not query.strip():
        return []
    try:
        res = store.execute_query(
            """
            CALL db.index.fulltext.queryNodes('page_text', $q) YIELD node, score
            RETURN node.url AS url, node.title AS title,
                   node.content_preview AS content_preview,
                   node.institution AS institution, node.category AS category,
                   score
            ORDER BY score DESC LIMIT $limit
            """,
            parameters={"q": query, "limit": limit},
        )
        # Semantica Neo4jStore.execute_query 반환 형태에 따라 정규화
        records = []
        if isinstance(res, dict):
            records = res.get("records") or res.get("data") or []
        elif isinstance(res, list):
            records = res
        out = []
        for r in records:
            if isinstance(r, dict):
                out.append(r)
            elif hasattr(r, "data"):
                out.append(r.data())
            elif hasattr(r, "__getitem__"):
                try:
                    out.append({k: r[k] for k in ("url", "title", "content_preview", "institution", "category", "score")})
                except Exception:
                    pass
        return out
    except Exception as e:
        log.debug(f"Neo4j search_pages_by_text failed: {e}")
        return []


def upsert_page(
    url: str,
    title: str,
    site_code: str,
    crawled_at: int,
    extra: Optional[dict] = None,
) -> Optional[dict]:
    """Page 노드 upsert (url 키 기준).

    Returns: 노드 dict 또는 None (Neo4j 미연결).
    """
    store = get_neo4j_store()
    if store is None:
        return None
    props = {
        "url": url,
        "title": title or "",
        "site_code": site_code,
        "crawled_at": int(crawled_at),
    }
    if extra:
        props.update(extra)
    try:
        # MERGE 패턴 — execute_query 로 직접 Cypher
        res = store.execute_query(
            """
            MERGE (p:Page {url: $url})
            SET p += $props
            RETURN p
            """,
            parameters={"url": url, "props": props},
        )
        return res
    except Exception as e:
        log.warning(f"upsert_page failed {url}: {e}")
        return None
