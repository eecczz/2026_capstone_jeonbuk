"""GraphRAG 모듈 — Semantica + Neo4j 기반 지식그래프.

문서처리 파이프라인과 동일 스택 (Semantica → Neo4j) 으로 크롤 데이터를 graph
인덱스에 적재한다. Qdrant vector RAG 와 dual-track 으로 동작:

  크롤 markdown → 청크 → BGE M3 임베딩 → Qdrant (vector 검색)
                       ↘
                         Semantica 엔티티/관계 추출 → Neo4j (그래프 검색)

  질의 시: vector top-k + graph 멀티홉 결과 합쳐 LLM 컨텍스트.

연결 정보:
- bolt://host.docker.internal:7687
- user/password 는 PersistentConfig (NEO4J_*) 또는 환경변수에서 로드.
"""

from .neo4j_client import get_neo4j_store, close_neo4j_store

__all__ = ["get_neo4j_store", "close_neo4j_store"]
