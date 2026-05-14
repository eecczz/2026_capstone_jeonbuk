# 13.7b Plan — Multi-Section Analysis 확장

작성: 2026-05-13
patch: 2026-05-14 (10 항목 + 5 nuance 반영)

---

## Patch 요약 (2026-05-14)

기존 설계에 다음 13개 변경 반영:

1. **section_type enum 폐기** → `structural_relationship` + `placement_recommendation` 두 free-form 필드 분리. enum은 `_debug.reference_label`로만.
2. **section4 abstract flow 4 case** (Case A/B/C/D) 추가.
3. **13.4b multi-section 확장**: B3.1/B3.2 sub-step (chapter intent merge + document-level seed regenerate).
4. **13.7c 완전 re-run** 정책. mapping table 폐기.
5. **Cache bump 시점**: B1 진입 직전으로 이동 (기존 step 14 → step 5).
6. **B5 invariant + XML/HWPX diff + shallow regression** 검증 조건 추가.
7. **Large-region replacement policy** 카테고리 + 보수적 default 명시.
8. **Review decision artifact 3개**: `13_7b_b0a_observation.json` / `13_7b_b0b_observation.json` / `13_7b_merge_decision.json`.
9. **Lineage 핵심 필드** (`source_section_id`, `source_section_local_idx_range`, `target_region_id`, `origin`) B3 merge 출력 schema에 추가.
10. **source_diagnostic_key 확장**: B3 출력 + 13.7c chapter object `_debug.reference_metrics`에 `section_id` 키 추가.

추가 nuance:

11. **code preserve fallback 조건** 명시: AI ambiguity_flags / confidence="low" / 호출·parse 실패 / 명백한 구조 계약 위반. code는 "충돌 크기"를 자체 판단 X.
12. **`origin` enum**은 code processing action의 fact 기록이며 AI 의미 분류 아님 — 명시.
13. **Cross-section parent 3단계**로 patch: (a) out-of-range fail / (b) ambiguous preserve fallback / (c) legitimate continuation preserve fallback.

---

## 1. 성격

13.7b는 1a 파이프라인을 multi-section으로 확장하고, section별 분석 결과를 document-level 구조로 안전하게 merge하는 stage. **양식 골격 완성의 마지막 큰 단계**.

| stage | 책임 | 상태 |
|-------|------|------|
| 13.7a | Chapter-grouped assembly (chapter boundary 보존) | done |
| 13.7c | Source-to-template adaptation planning (template-first) | done |
| **13.7b** | **Multi-Section Analysis (모든 section 분석 + document-level merge)** | **이 단계** |
| 14 | KB/RAG + user_request (13.7c 확장 동반) | 13.7b 이후 |

### 1.1 진단된 문제

13.7a 완료 후:
- 민원인은 section 5개 (section0~4). 현재 section0만 1a 분석. section1~4 (86+4+1+193 paragraphs, 전체 47%)는 13.5 unanalyzed_section preserve 안전장치로 보존.
- 안전장치는 **임시**. section1~4 generation/update 안 됨.
- 특히 section4 "제2장 - 반복민원 대응" 193p는 본문성 후보 — 단 본문성 여부 자체는 13.7b 분석 (section_role_proposal AI sub-step) 결과로 판단. 현재는 paragraph count + heading 후보 기반 raw evidence만 있고, top-level/nested/attachment 어느 카테고리인지 사전 판정 X.

13.7b 목표: 임시 안전장치 → 분석 기반 판단으로 전환.

### 1.2 핵심 위험 (B3 document-level merge)

13.7b의 가장 어려운 부분은 section별 분석 결과를 한 document-level 구조로 통합하는 것이다.

- section-local paragraph idx vs document-global idx
- parent_idx가 section 경계를 넘는 경우 (cross-section parent)
- section별 role_cluster 의미가 다를 수 있음 (layout/style 차이)
- section별 chapter_types / marker_policy / format_rules 충돌
- section4가 top-level chapter인지 attachment인지 nested인지 판단

이 위험을 줄이기 위해 **B0 (measurement) 단계를 1a 호출 전/후로 분리**하고, evidence 기반으로 B3 설계를 finalize.

### 1.3 13.7b 동작 시나리오 — abstract flow 4 case

section4 (또는 multi-section의 새 chapter 후보)가 어떻게 처리되는지의 흐름. **구체 수치 X, 흐름과 책임만**. 양식 detail은 양식 evidence + 사용자 review에 맡김.

#### Case A — 독립 본문 chapter group

AI section_role_proposal:
- `structural_relationship`: "section0 본문 chapter Ⅰ~Ⅷ 다음에 오는 독립 chapter group처럼 보임"
- `placement_recommendation`: "독립 chapter list 항목으로 추가"
- `confidence`: high/medium

흐름:
- B3.1: section_role_proposal 결과 + chapter intent merge → document-level chapter list에 새 chapter 추가
- B3.2: 13.4b multi-section regenerate에서 새 chapter의 local_pattern/local_catalog 추출
- B4: target_unit_plan에 새 chapter region 추가 (section_id = section4)
- B5: chapter object placement → section4 영역에 배치
- 13.7c re-run: 새 chapter에 adaptation_decision 추가 (mapping table 없이 완전 re-run)
- 2b generation: §B5.1 replacement granularity policy 적용

**위험 + default 동작**:
- 193p 같은 large region 전체 generate replace는 원본 손실 위험
- §B5.1 보수적 default: `paragraph_count > threshold` AND "독립 chapter group"이면 **title only adapt + body preserve**
- detail granularity (slot fill / subtree mix)는 양식 evidence 누적 후

#### Case B — section0 chapter의 nested/continuation

AI section_role_proposal:
- `structural_relationship`: "section0 ChX의 sub-chapter 또는 continuation처럼 보임"
- `placement_recommendation`: "section0 ChX 하위 nested 배치"

흐름:
- Cross-section parent 정책 (§10) 적용:
  - (a) 명백한 out-of-range parent reference → Level 3 validation fail (B4)
  - (b) section break ambiguous → preserve fallback (section-local root reassignment)
  - (c) legitimate continuation → preserve fallback (13.7b 초기 보수적, evidence 후 nested 확장 검토)
- 13.7b 초기는 (a)만 fail, (b)/(c)는 preserve fallback로 처리 (양식 evidence 부족)
- 13.7c re-run: ambiguity_flags 표기, 보수적 동작

#### Case C — attachment / 별첨 / 부록

AI section_role_proposal:
- `structural_relationship`: "별첨 또는 부록 형태"
- `placement_recommendation`: "preserve only"

흐름:
- B3.1: document-level chapter list에 추가 X
- B5: 해당 section 전체 preserve (변경 없음)
- 13.7c re-run: 미진입 (preserve 자동)
- 2b generation: 미실행

#### Case D — fallback (AI 불확실)

AI section_role_proposal:
- `confidence`: "low"
- `ambiguity_flags`: 채워짐 (예: ["unable_to_classify", "evidence_too_sparse"])

흐름:
- code preserve fallback (§9.5 조건 매칭)
- 해당 section 전체 preserve
- 13.7c re-run: 미진입
- debug에 AI evidence + 보수적 동작 이유 기록

이 4 case가 §9 AI section_role_proposal의 `placement_recommendation`과 자연 매핑. **단 code는 placement_recommendation 텍스트를 직접 enum 매핑 X** — §9.5 조건 (ambiguity / low confidence / 구조 위반)에서 preserve fallback, 그 외에는 AI 추천에 따라 진행하되 §10 cross-section parent 정책 + §B5.1 replacement granularity policy로 보호.

---

## 2. 설계 원칙

13.7a/13.7c 원칙을 유지하면서 multi-section에 특화된 원칙을 추가.

### 2.1 13.7a/13.7c와 일관된 원칙

1. **의미 판단은 AI**. code는 의미 판단 안 함.
2. **코드는 JSON/schema, 필수 필드, 명백한 계약 위반만 본다.**
3. **heuristic은 hard fail 금지.**
4. **evidence 관련 수치는 참고 metric으로만**. `_debug.reference_metrics`에 보관. warning 아님.
5. **preserve 강등은 명백한 경우만**: AI 호출 실패, parse 실패, schema 위반, 필수 evidence 없음, action 모순.
6. **Template-first 흐름**. section 분석 후에도 template chapter need가 frame, source는 도구.
7. **13.7b는 generation 품질 보장이 아니라 multi-section 정확 이해**. 의미 적합성의 final gate는 사용자 눈검증.

### 2.2 13.7b 특수 원칙

8. **Numeric thresholds are reference metrics, not automatic gates.**

   cluster similarity, paragraph count, heading density, body density, layout difference 같은 모든 code numeric metric은 reference metric으로만 기록 (`_debug.reference_metrics`).

   다음 모든 사용 금지:
   - 자동 section 성격 결정
   - 자동 chapter 분류
   - 자동 role clustering 정책 결정
   - preserve 강등
   - validation fail

   **단 명백한 구조 계약 위반(예: cross-section parent out-of-range)은 예외** — Level 3 validation fail.

9. **Section 성격 정보는 AI evidence-driven proposal. enum 폐기.**

   - `structural_relationship` (free-form): section의 다른 section과의 구조 관계 기술 (양식 layout 사실)
   - `placement_recommendation` (free-form): AI의 처리 추천 (code 자동 매핑 X)
   - `supporting_evidence` / `counter_evidence` / `ambiguity_flags` / `confidence` 강제
   - enum (top_level/nested/attachment/empty/unknown/other)은 **`_debug.reference_label`로만**. code 정책 매핑에 사용 X.

   이는 13.7c가 `intent_role` enum을 폐기하고 evidence-driven schema로 간 원칙(원칙 14 — 양식 3개 evidence로 분류 체계 확정 X)과 일관.

10. **Section-aware schema 도입 시 backward compatibility.** chapter object `section_id` 필드 (13.7a에서 0 기본값)에 실 값 채움. 기존 schema 그대로, 값만 갱신.

11. **code preserve fallback 조건 — 명시.**

    code는 다음 경우에만 preserve fallback으로 보낸다:
    - AI가 `ambiguity_flags`를 채웠거나 `confidence="low"`로 표시한 경우
    - AI 호출/parse 실패 (network, rate limit, JSON parse 실패)
    - AI 추천이 schema 위반 또는 명백한 구조 계약 위반:
      - 존재하지 않는 region_id 참조
      - section 범위 초과
      - invalid parent reference
      - 필수 필드 누락
      - enum 값 외

    **code는 "충돌의 크기"를 자체 판단하지 않는다.** AI가 ambiguity로 flag 안 했고 schema도 통과하면 추천대로 진행. 13.7c의 reference_metrics가 정책 영향 X로 두는 패턴과 일관.

12. **13.7c 완전 re-run 정책.** 13.7b 후 chapter/region 구조 변경 → 13.7c 완전 re-run. mapping table 폐기 (복잡도 > 재호출 비용).

13. **Cache schema bump 시점은 B1 진입 직전.** multi-section 1a 구조 변경 후 기존 cache 신뢰 X. B1~B5 구현 중 cache hit/miss 섞이는 위험 차단.

---

## 3. 범위 (포함/제외)

### 3.1 포함

- 모든 section 추출 (B1)
- section별 1a 분석 (B2, full baseline)
- section-local / document-global index 정리
- section별 chapter/region 판단
- section-aware target_unit_plan (B4)
- chapter object에 실제 section_id 반영 (B5)
- document-level merge (B3) + chapter intent merge + document-level seed regenerate (B3.1/B3.2)
- section1~4 preserve를 분석 기반 판단으로 전환
- Large-region replacement policy 카테고리 + 보수적 default (B5.1)
- Review decision artifact 3개 (B0a / B0b / merge_decision)
- Lineage 핵심 필드 + source_diagnostic_key 확장

### 3.2 제외 (별도 stage)

- user_request-aware planning → 14단계 진입 시 13.7c 확장
- KB/RAG → 14단계
- source slice (chapter별 source 추출) → 14단계 또는 별도
- coverage validation → 15
- 13.7c prompt 재설계 → 호환 유지
- super-planner 통합 (13.4b + 2a + 13.7c) → 15+ 이후
- significance-driven / lightweight 1a → baseline 후 최적화 검토
- table cell filling → 14-table
- 부분 adaptation_decision mapping table → 13.7c 완전 re-run로 폐기
- Large-region replacement granularity detail (slot fill / subtree mix) → 양식 evidence 누적 후 별도 stage

---

## 4. Schema

### 4.1 chapter object (13.7a + 13.7c 변경)

13.7b에서 변경:
- `section_id`: **실 값 채움** (13.7a에서 0 기본값)
- 나머지 schema 동일

### 4.2 target_unit_plan region

13.7b에서 확장:
```python
{
  "region_id": int,
  "section_id": int,                       # 단일 section (cross-section은 validation fail)
  "section_local_idx_range": [int, int],   # section 내 paragraph idx
  "global_idx_range": [int, int],          # document-global paragraph idx
  "paragraph_indices": [int, ...],         # document-global (기존 호환)
  "section_span": [int],                   # 기존 호환 — 단일 section만
  ...
}
```

### 4.3 chapter_template_plan_seed

13.7b에서 확장 (B3.2 document-level seed regenerate 결과):
```python
{
  "chapters": [
    {
      "idx": int,
      "region_id": int,
      "section_id": int,              # 신규
      "first_paragraph_idx": int,     # document-global
      "section_local_first_idx": int, # 신규
      "template_title": str,
      "description": str,
      "local_pattern": {...},
      "local_catalog": {...},
      "local_title_role": str,
      ...
    }, ...
  ]
}
```

### 4.4 section analysis result (B2 출력)

각 section별 1a 결과:
```python
{
  "section_id": int,
  "paragraph_count": int,
  "paragraphs": [...],                  # section-local idx 기준
  "chapter_types": {...},               # section-local
  "marker_policy_1f": {...},            # section-local
  "template_grammar": {...},            # section-local
  "role_text_types": {...},             # section-local
  "format_rules": {...},                # section-local
  "blank_rules": [...],                  # section-local
  "_section_analysis_meta": {
    "section_xml_size": int,
    "ai_calls": int,
    "errors": [],
  }
}
```

### 4.5 section_role_proposal (AI sub-step output)

**§9.1 AI sub-step**의 output. 이전 `section_classification` 대체. enum 폐기.

```python
{
  "section_id": int,
  "structural_relationship": str,
  # free-form. section의 다른 section과의 구조 관계 기술 (양식 layout 사실)
  # 예: "section0 본문 Ⅰ~Ⅷ 다음에 오는 독립 chapter group처럼 보임"
  # 예: "section0 ChX의 continuation 또는 sub-chapter 후보"
  # 예: "별첨/부록 형태"

  "placement_recommendation": str,
  # free-form. AI의 처리 추천 (code 자동 매핑 X — §9.5 조건만 매핑)
  # 예: "독립 chapter list 항목으로 추가"
  # 예: "section0 ChX 하위 nested 배치"
  # 예: "preserve only"

  "supporting_evidence": [str],         # section content/structure 인용
  "counter_evidence": [str],            # 분류 위험 신호
  "ambiguity_flags": [str],
  "confidence": "high" | "medium" | "low",

  "_debug": {
    "reference_label": str,
    # 자유 enum hint (예: top_level / nested / attachment / empty / unknown / other)
    # 통계/측정용. **code 정책 매핑 X.** 양식 추가 시 새 label 생기면 enum 확장 없이 그대로 기록.
  }
}
```

**code 매핑 정책 (§9.5)**:
- `placement_recommendation` 텍스트 → code 자동 enum 매핑 X
- ambiguity_flags 채워짐 / confidence="low" / schema 위반 → preserve fallback
- 그 외에는 §1.3 Case A/B/C와 placement_recommendation 의미를 사용자+claude review에서 매핑 (B0b review point)
- 즉 13.7b 초기는 "AI 추천 + review 합의" 흐름, code automation X

### 4.6 document-level merge 결과

B3 merge 출력:
```python
{
  "paragraphs": [
    {
      "global_idx": int,
      "section_id": int,
      "section_local_idx": int,
      "role": str,
      "level": int,
      "parent_idx": int | None,   # document-global

      # Lineage 핵심 필드 (B3 merge에서 attach)
      "source_section_id": int,
      "source_section_local_idx_range": [int, int],
      "target_region_id": int | None,
      "origin": "template_preserve" | "generated" | "adapted_title" | "fallback_preserve",
      # code processing action의 fact 기록. AI 의미 분류 아님.
      # 양식 추가 시 새 action 생기면 enum 확장.
      ...
    }, ...
  ],
  "section_info": {
    "section_count": int,
    "section_ranges": [{section_id, global_idx_range, paragraph_count}, ...],
    "section_role_proposals": {section_id: section_role_proposal},  # 4.5 결과
  },
  "chapter_types": {...},                # merged (정책에 따라 document-level 또는 section-local mapping)
  "marker_policy_1f": {...},             # merged
  "template_grammar": {...},             # merged
  "source_diagnostic_keys": [
    # (section_id, region_id, chapter_idx) 단위 확장 — B7 활용
    {"section_id": int, "region_id": int, "chapter_idx": int, ...}
  ],
  ...
  "_merge_meta": {
    "cross_section_parent_violations": [...],  # Level 1 detection (debug)
    "cross_section_parent_classifications": [...],  # Level 2 분류 (a/b/c)
    "role_cluster_merge_strategy": "document_level_reclustering" | "section_local_mapping",
    "merge_warnings": [...],
  }
}
```

### 4.7 13.7c chapter object `_debug.reference_metrics` schema 변경

13.7c가 만든 chapter object `_debug.reference_metrics`에 `section_id` 키 추가. measurement logic 자체는 변경 없음.

```python
{
  "chapter_idx": int,
  "section_id": int,  # 13.7b 신규
  "supporting_evidence_substring_match_ratio": float | None,
  "generated_body_evidence_overlap_ratio": float | None,
  ...
}
```

B3 merge 후 chapter object의 section_id 실 값에서 자연 채워짐. 13.7c re-run 시 자동.

---

## 5. B0a — Pre-1a Section Census (AI 호출 없음, debug-only)

### 5.1 목적

1a 호출 비용 전에 section별 basic property를 측정. B2 (full 1a baseline) 진입 비용 추정 + B3 설계 evidence.

### 5.2 항목

- section count
- section별:
  - paragraph count
  - table count
  - layout (orientation, page size, margin) — 13.6-A에서 가져옴
  - section_xml size (bytes)
  - first/last paragraph text preview (heuristic)
  - secPr 존재 여부
  - idx range (document-global)

### 5.3 구현

13.6-A `diagnose_multi_section` 결과를 그대로 활용 + section_xml_size 추가.

새 함수 또는 기존 함수 확장:
- `extract_section_census()` (hwpx_analyzer.py)

DB tool에서 호출 → `_debug_payload["section_census"]`.

### 5.4 사용처

- B2 진입 비용 추정 (section별 xml_size로 token 추정)
- B3 설계 evidence (section 수, content significance signal)
- 사용자 + claude review point: B0a 결과 보고 B2 진입 결정

### 5.5 Review artifact: `13_7b_b0a_observation.json`

B0a 결과 + review 합의 기록. B2 진입 전 사용자+claude review point에서 작성.

```python
{
  "section_count": int,
  "sections": [
    {
      "section_id": int,
      "paragraph_count": int,
      "table_count": int,
      "section_xml_size": int,
      "layout": {...},
      "first_paragraph_preview": str,
      "last_paragraph_preview": str,
      "secpr_present": bool,
      "idx_range": [int, int],
    }, ...
  ],
  "reference_metrics": {
    # section significance scan (참고 metric only, hard rule X)
    "total_paragraphs": int,
    "section_paragraph_share": {section_id: float},  # 비율
    "estimated_b2_token_cost": {section_id: int},
  },
  "review_decisions": {
    # B0a review point 후 사용자+claude 합의 기록
    "sections_to_analyze_in_b2": [int, ...],
    "sections_to_skip_in_b2": [int, ...],
    "skip_reason": {section_id: str},  # free text
    "reviewer": "user+claude",
    "b2_entry_authorized": bool,
    "reasoning": str,
  }
}
```

skip 결정은 review에서 사용자 + claude 합의로 결정. paragraph 1개뿐인 section 등 명백한 case만 skip 권고, 모호하면 분석 포함 (보수적).

---

## 6. B0b — Post-1a Merge Feasibility (B2 결과 분석, debug-only)

### 6.1 목적

B2 (section별 1a baseline) 결과를 분석하여 B3 merge 정책 결정 evidence 확보.

### 6.2 항목

- section별 role_cluster 비교
  - cluster 수
  - cluster fingerprint (paragraph 분포, level/parent 패턴)
  - 다른 section과의 유사도 (reference metric)
- section별 chapter_types 비교
  - chapter_type 수
  - title_role 동일성
- section별 marker_policy_1f 비교
- section별 format_rules 비교
- **Cross-section parent detection (Level 1)** — 자세히 §10
- section별 paragraph_count 정합성 (1a 분석 paragraph 수 vs section_xml paragraph 수)
- 13.7c re-run feasibility:
  - 새 chapter 수 (section1~4에서 추가될 가능성)
  - chapter_template_plan_seed schema 호환

### 6.3 구현

- `measure_merge_feasibility()` (hwpx_analyzer.py)
- DB tool에서 호출 → `_debug_payload["merge_feasibility"]`

### 6.4 사용처

- B3 설계 finalize evidence
- 사용자 + claude review point: B3 진입 전 설계 합의

### 6.5 Review artifact: `13_7b_b0b_observation.json`

B0b 결과 + 4개 정책 합의 기록. B3 진입 전 사용자+claude review point에서 작성.

```python
{
  "merge_feasibility_metrics": {
    "section_role_cluster_comparison": {...},
    "section_chapter_types_comparison": {...},
    "section_marker_policy_comparison": {...},
    "section_format_rules_comparison": {...},
    "section_paragraph_count_consistency": {...},
  },
  "cross_section_parent": {
    "violations": [...],  # Level 1 detection
    "classifications": {  # Level 2 분류
      "a_out_of_range": [...],
      "b_section_break_ambiguous": [...],
      "c_legitimate_continuation_candidate": [...],
    }
  },
  "section_role_proposals": [...],  # 4.5 schema list
  "review_decisions": {
    # 4개 정책 합의 기록
    "role_clustering_strategy": "document_level_reclustering" | "section_local_mapping",
    "chapter_types_merge_strategy": "X" | "Y" | "Z",
    "marker_policy_merge_strategy": "section_aware" | "document_level",
    "section_placement_strategy_per_proposal": [
      # 각 section의 placement_recommendation → 13.7b 매핑 (Case A/B/C/D)
      {
        "section_id": int,
        "ai_placement_recommendation": str,
        "applied_case": "A" | "B" | "C" | "D",
        "reasoning": str,
      }
    ],
    "reasoning": str,
    "counter_evidence": [str],
    "unresolved_ambiguity": [str],
    "reviewer": "user+claude",
    "b3_entry_authorized": bool,
  }
}
```

---

## 7. 구현 항목 (B1~B7)

### B1. `extract_all_sections_xml()` 신규 함수 (backward compat 패턴)

기존 `extract_section_xml` signature 유지 (`-> str`, section0만 반환).
신규 함수 `extract_all_sections_xml()`이 모든 section의 `(name, xml)`
tuple list 반환 (sorted by section name, document-global 순서).

기존 함수는 `extract_all_sections_xml()[0][1]`을 반환하는 backward
compat wrapper로 변경. `analyze_hwpx`, legacy `files.py` endpoint 등
single-section 호출자는 영향 0.

**trade-off**: 계획서 원안 (signature 변경)은 `analyze_hwpx` →
`files.py` 3곳 (line 1055/1518/1714) legacy endpoint 영향. backward
compat 패턴이 regression 위험 작음, 13.7c가 chapter route를 신규
path로 추가한 패턴과 일관.

영향:
- `hwpx_analyzer.py`: 새 함수 추가 + 기존 함수 wrapper화
- DB tool: 영향 없음 (B2에서 새 함수 호출자 추가)
- `files.py` legacy endpoint: 영향 없음
- 1a~1f entry 함수들: B2에서 시그니처 multi-section 수용 (현 stage 무관)

### B2. Section별 Full 1a Baseline

각 section 독립적으로 1a~1f 호출. **A baseline (정확도 우선)**.

호출:
- section별 paragraph 분석 (1a)
- section별 role clustering (1b/1c)
- section별 chapter_types (1d)
- section별 marker_policy (1f)

토큰 비용: section 수만큼 증가. 민원인 5배.

추가: **section_role_proposal AI sub-step** (§9)
- 각 section별 또는 batch로 AI 호출
- output: §4.5 schema (structural_relationship + placement_recommendation + evidence)

### B3. Document-level Structure Merge

가장 위험. §8 세부 설계. 다음 sub-step으로 분리:

- **B3.0**: parent_idx offset 변환 + cross-section parent detection + classification (Level 1/2)
- **B3.1**: chapter intent merge — section_role_proposals 결과 + B0b 합의 (Case A/B/C/D 매핑) 적용
- **B3.2**: document-level chapter_template_plan_seed regenerate (13.4b multi-section 확장)
  - section별 13.4b 결과 통합 (cross-section view)
  - 새 chapter의 local_pattern/local_catalog 추출
  - 기존 section0 chapter + section1~4의 새 chapter 통합 seed
- **B3.3**: merge 결과 schema 출력 (§4.6 lineage 필드 포함, source_diagnostic_keys 포함)
- **B3.4**: review artifact 작성 — `13_7b_merge_decision.json` (§B3.4)

#### B3.4 Review artifact: `13_7b_merge_decision.json`

B3 진입 시점 + B3 완료 후 합의 결과. B4 진입 권한 명시.

```python
{
  "merge_strategy_applied": {
    "role_clustering_strategy": str,
    "chapter_types_merge_strategy": str,
    "marker_policy_merge_strategy": str,
    "section_placement_strategy_per_proposal": [...],
  },
  "merge_output_summary": {
    "document_global_paragraph_count": int,
    "section_ranges": [...],
    "merged_chapter_count": int,
    "new_chapters_from_multi_section": [...],
  },
  "cross_section_parent_handling": {
    "a_out_of_range_count": int,
    "b_ambiguous_preserve_count": int,
    "c_legitimate_continuation_preserve_count": int,
    "total_validation_fail": int,
  },
  "document_level_seed": {
    "chapter_count": int,
    "new_chapters": [...],
  },
  "lineage_summary": {
    "origin_distribution": {origin: count},
    "section_distribution": {section_id: paragraph_count},
  },
  "validation_results": {
    "schema_violations": int,
    "invariant_violations": int,
  },
  "b4_entry_authorized": bool,
  "reviewer": "user+claude",
}
```

### B4. Section-aware target_unit_plan

- region에 `section_id`, `section_local_idx_range`, `global_idx_range` 추가
- **cross-section region 감지 시 validation fail (Level 3, §10.3)** — 단 §10.2 (a) case만 fail. (b)/(c)는 preserve fallback
- chapter object section_id 실 값

### B5. Section-aware chapter object / placement

- chapter object의 `section_id`에 실 값
- assemble에서 chapter object를 해당 section에 placement
- 민원인 section4 chapter는 section4에 append (현재 section0에 몰림)
- section1~4 preserve 정책 전환: 분석 기반 판단 (Case A/B/C/D 적용)

#### B5.1 Large-region replacement policy

**카테고리 5개**:
- region 전체 replace
- title only adapt + body preserve
- slot fill (특정 paragraph만)
- subtree mix (chapter object 내 일부 generate + 일부 preserve)
- 전체 preserve

**13.7b 초기 default (보수적)**:
- `region_paragraph_count > threshold` (reference metric, 예시값 50p — 자동 결정 X, B0b review에서 합의)
- AND `structural_relationship`이 "독립 chapter group" 또는 유사한 large content section
- → **title only adapt + body preserve** (region 전체 replace 금지)

근거: 13.7c가 source_gap에서 보수적 preserve로 간 패턴과 일관. section4 193p 전체 generate replace는 원본 손실 위험. evidence 누적 후 더 정밀한 granularity 결정.

threshold는 reference metric으로 hard rule X. B0b review point에서 사용자+claude 합의로 결정.

**detail granularity (slot fill / subtree mix)는 양식 evidence 누적 후 별도 stage.** 13.7b 초기는 default 보수적 동작만.

### B6. Cache schema bump — **B1 진입 직전**

- `cache_schema_version` bump (예: v4 → v5)
- 기존 3개 양식 cache invalidate (migration 없음)
- 첫 양식 실행 시 자동 재분석

**시점**: 이전 §14 step 14 → **step 5 (B1 진입 직전)** 으로 이동. multi-section 1a 구조 변경 후 기존 cache 신뢰 X. B1~B5 구현 중 cache hit/miss 섞이는 위험 차단.

### B7. Source diagnostic schema 확장

- chapter 단위 → (section, chapter) 단위
- "source 있음 + generated 비어있음" 패턴 카운트
- source allocation blocker 승격 evidence (13.7c reference_metrics와 연동)
- B3 출력 schema에 이미 `source_diagnostic_keys` 추가 (§4.6) — B7은 이를 활용
- 13.7c chapter object `_debug.reference_metrics`에도 `section_id` 키 추가 (§4.7)

B7 step 자체는 step 15 그대로. key 확장은 B3에서 자연 따라옴.

---

## 8. B3 Document-level Merge 세부 설계

> ⚠️ **B0b 결과 확인 전 B3 구현 금지.**
>
> B3는 13.7b에서 가장 위험한 단계. B0b measurement 결과 없이 B3 정책을 추정으로 결정하면 양식별 over-fit 또는 잘못된 merge 정책 도입 위험.
>
> **B3 구현 진입 전 필수 review point**:
> 1. B0b 결과 보고 (사용자 + claude)
> 2. B0b evidence 기반 4개 정책 결정:
>    - **role clustering 전략** (옵션 A document-level reclustering vs 옵션 B section-local + mapping) — §8.3
>    - **chapter_types merge** (옵션 X document-level / Y per-section / Z section-local + seed에 section_id) — §8.4
>    - **marker_policy_1f merge** (section-aware vs document-level) — §8.5
>    - **section_role_proposal placement_recommendation 매핑** (각 section을 Case A/B/C/D 중 어디로 처리할지) — §9.5
> 3. 4개 정책 합의 → `13_7b_b0b_observation.json`의 `review_decisions` 채워짐 + `b3_entry_authorized=true`
> 4. 합의 없이 B3 구현 시작 시 stage fail로 간주

### 8.1 Parent_idx offset 변환

각 section의 paragraph_idx는 section-local (0부터 시작). document-global로 변환:

```
section_offset[0] = 0
section_offset[i] = section_offset[i-1] + paragraph_count(section_{i-1})

global_idx(s, local_idx) = section_offset[s] + local_idx
global_parent_idx(s, local_parent_idx) = section_offset[s] + local_parent_idx
```

단 `local_parent_idx == None` (root) → `global_parent_idx == None` 유지.

### 8.2 Cross-section parent (3단계 정책 §10 참조)

- Level 1 (B0b detection): debug 기록
- Level 2 (B0b classification): (a) out-of-range / (b) ambiguous / (c) legitimate continuation
- Level 3 (B4 validation): (a)만 fail. (b)/(c)는 preserve fallback

### 8.3 Role clustering 정책 — 측정 후 결정

**두 옵션**:

#### 옵션 A: Document-level reclustering
- 모든 section의 paragraph를 합쳐서 role clustering 재실행
- 결과: 일관된 document-level cluster
- 비용: 추가 AI 호출 (1c-style)
- 위험: layout 다른 section의 paragraph가 다른 cluster로 분리될 가능성

#### 옵션 B: Section-local cluster + mapping table
- section별 cluster 유지
- document-level에 mapping table (예: `{section_0_cluster_3 → doc_cluster_X, section_4_cluster_1 → doc_cluster_X}`)
- 비용: 추가 AI 호출 없음
- 위험: 같은 의미의 role이 다른 cluster로 분리 → assemble에서 다른 exemplar 사용

**결정 시점**: B0b 측정 후. **reference metric을 보고 사용자 + claude review에서 합의** (자동 hard rule X — §2.8).

측정할 reference metric:
- section별 cluster fingerprint 유사도 (numeric, debug-only)
- 같은 layout section 그룹의 cluster 일관성
- 양식별 차이

### 8.4 Chapter_types merge

section별 chapter_types가 다르면:

- 옵션 X: document-level chapter_types (단일 통합)
- 옵션 Y: section-local chapter_types + section_id 키 (per-section)
- 옵션 Z: section-local 유지 + chapter_template_plan_seed에서 section별 분리

B0b 측정 결과:
- section별 chapter_types 의미 중복도
- title_role 동일성

13.7c는 chapter_template_plan_seed를 통해 chapter intent를 받으므로, chapter_types가 section-local이어도 seed가 multi-section을 표현하면 호환.

**prior (B0b 합의 전 검토 시작점)**: Z (section-local 유지 + seed가 section_id 포함). B0b evidence가 다른 결과를 가리키면 재고. **review gate에서 합의 후 finalize.**

### 8.5 Marker_policy_1f merge

section별 marker_policy가 다를 수 있음 (layout 차이로). assemble의 marker rewrite가 section-aware 동작해야:

- 같은 chapter title role이 section마다 다른 marker policy?
  - 가능성 낮음 (chapter title은 보통 일관)
  - 단 section2 NARROWLY layout처럼 다른 marker 가능

- 정책:
  - section-aware marker_policy (chapter object의 section_id 보고 적용)
  - 또는 document-level merge (가장 흔한 policy 우선, 충돌 시 warning)

B0b 측정 후 결정.

### 8.6 Format_rules / blank_rules merge

대부분 section 무관 (role 전환 관계). 단 검증 필요.

- B0b에서 section별 차이 측정
- 차이 없으면 document-level 통합
- 차이 있으면 section-aware (section_id 키)

### 8.7 idx_map section-aware 변환

`truncate_xml`의 ai_idx → real_idx 매핑. multi-section이면:

- 옵션 1: section별 truncate. idx_map은 section별. 합칠 때 offset 변환.
- 옵션 2: 합쳐서 truncate. idx_map은 document-global.

옵션 1이 더 명확. B1/B2 구현 시 결정.

### 8.8 region_id 안정성

cache invalidation 후 새 region_id 부여 시 chapter_object.target_region_id 정합성. cache invalidate → 양식 재분석 → 새 region_id → 13.7c **완전 re-run** (§12) → 새 chapter_object.

이건 cache invalidation 정책 + 13.7c 완전 re-run 정책과 일관. 한 번 invalidation 후 양식별로 새 region_id로 재구성. 호환.

### 8.9 chapter_template_plan_seed 호환성 (B3.2)

B3.2 document-level seed regenerate:
- 기존 13.4b는 section0만 보고 chapter intent 추출
- B3.2는 multi-section view (section_role_proposals + cross-section parent classification 결과 + section별 1a 결과)를 받아 통합된 seed 1회 생성
- chapter intent의 일관성 (양식 전체 view)
- 새 chapter (section4의 "제2장" 등)의 local_pattern/local_catalog 추출
- seed schema: §4.3 (section_id, section_local_first_idx 신규 필드)

13.4b 자체 코드는 multi-section input을 받도록 확장. 단 single section input도 호환 (조달청 single-section regression 0).

### 8.10 merge 결과 schema 안정성

B3 출력은 §4.6의 schema. 이게 cache에 저장됨. cache_schema_version bump 필요 (§B6, B1 진입 직전).

---

## 9. AI section_role_proposal sub-step (§4.5 schema)

이전 `section_classification` 대체. enum 폐기, evidence-driven free-form 두 필드 + evidence + ambiguity.

### 9.1 위치

B2 후 (section별 1a 결과 받은 후) 또는 B3 sub-step.

권고: B2 안의 마지막 sub-step. 1a 결과를 input으로 받아 batch로 호출.

### 9.2 호출 전략 — batch 권고

batch (consistency 좋음, 비용 낮음). section 수 많으면 split (입력 크기 기준).

**batch input schema** (token 폭증 방지 — summary만):
- 각 section의 B0a measurement (paragraph count, layout, table presence, heading density)
- 각 section의 1a 분석 결과 **summary** (전체 paragraphs X):
  - chapter_types compact summary
  - role_cluster summary
  - heading paragraph 후보 (3~5개 sample)
  - first/last paragraph text preview (80자)
- 양식 전체 context (template type, document title)

section detail (paragraph 전체)는 input에 X.

### 9.3 Prompt 구조 (Template-first 일관)

system:
```
당신은 양식 문서의 section 구조 관계와 처리 추천을 제안하는 도구입니다.
숫자 기준이나 자동 분류로 판단하지 마시고, section의 content/structure evidence를 보고 판단하세요.
양식에 고정된 카테고리 (top_level/nested/attachment 등) 에 끼워 맞추지 마시고, 양식 layout 사실을 자유 텍스트로 기술하세요.
JSON 객체로만 응답하세요.
```

user input:
- 각 section의 B0a/B0b measurement summary
- 각 section의 1a 분석 결과 summary
- 양식 전체 context

user output schema (4.5):
```python
{
  "section_role_proposals": [
    {
      "section_id": int,
      "structural_relationship": str,    # free-form
      "placement_recommendation": str,   # free-form
      "supporting_evidence": [str],
      "counter_evidence": [str],
      "ambiguity_flags": [str],
      "confidence": "high|medium|low",
      "_debug": {
        "reference_label": str,  # 자유 hint, code 매핑 X
      }
    }, ...
  ]
}
```

### 9.4 Validation

13.7c와 같은 패턴:
- `structural_relationship` 빈 string 또는 누락 → validation_failed
- `placement_recommendation` 빈 string 또는 누락 → validation_failed
- `supporting_evidence` 키 누락 → validation_failed
- `counter_evidence` 키 누락 → validation_failed
- `ambiguity_flags` 키 누락 → validation_failed
- `confidence` enum 외 → validation_failed
- AI 호출 fail → 전체 section `ambiguity_flags=["call_failed"]`, `confidence="low"`, `placement_recommendation="preserve only"` (보수적 fallback)

### 9.5 Code 매핑 정책 — preserve fallback 조건만

**code는 `placement_recommendation` 텍스트를 자동 enum 매핑 X.** 대신 다음 조건에서 preserve fallback (§2.2 원칙 11):

- `ambiguity_flags` 채워짐
- `confidence="low"`
- AI 호출/parse 실패
- `placement_recommendation`이 명백한 구조 계약 위반 참조:
  - 존재하지 않는 region_id
  - section 범위 초과
  - invalid parent reference
- schema 위반

위 조건 외에는 **B0b review point에서 사용자+claude가 각 section의 placement_recommendation을 §1.3 Case A/B/C/D 중 하나로 매핑** (review_decisions.section_placement_strategy_per_proposal에 기록). code는 그 매핑 결과를 schema 검증 후 적용.

**즉 code는 "충돌의 크기"를 판단하지 않는다.** AI가 ambiguity로 flag 안 했고 schema도 통과하면 review 합의대로 진행. 13.7c의 reference_metrics가 정책 영향 X로 두는 패턴과 일관.

**Debug field**:
- AI evidence (structural_relationship/placement_recommendation/supporting/counter/ambiguity)는 chapter object 또는 section 결과 `_debug.section_role_proposal`에 attach
- code action 결과 (preserve fallback / Case A/B/C/D 적용)는 `_debug.applied_case`에 기록
- 양쪽 다 debug에 보존 (production 동작에 반영 + debug trace)

---

## 10. Cross-section Parent 3단계 정책

### 10.1 Level 1 — Detection (B0b)

section별 1a 결과를 보고 cross-section parent 참조 검사:

```python
for section_s in sections:
    for paragraph in section_s.paragraphs:
        if paragraph.parent_idx is None:
            continue
        # parent_idx는 section-local
        if not (0 <= paragraph.parent_idx < len(section_s.paragraphs)):
            # parent_idx out of section bounds — cross-section 가능성
            violations.append({...})
```

debug 항목 (§10.4):
- section_id (paragraph가 속한 section)
- section_local_paragraph_idx
- document_global_paragraph_idx
- reported_parent_idx (1a output)
- expected_parent_section (section_id of paragraph)
- actual_parent_section (parent_idx가 가리키는 section, 추정)
- text_preview (paragraph 앞 80자)

Level 1은 **debug-only**. validation fail X.

### 10.2 Level 2 — Classification (B0b)

Detection 결과를 3 카테고리로 분류:

**(a) 명백한 out-of-range parent**:
- parent_idx가 어느 section의 valid idx에도 매칭 안 됨
- 또는 schema 위반 (음수, 비정수 등)
- → Level 3 validation fail 후보

**(b) section break ambiguous case**:
- parent_idx가 인접 section의 valid range 안에 있어 보임
- section break (보통 페이지 break, layout 전환) 때문에 끊겼을 가능성
- 양식 layout view에서는 section이 분리되지만 논리적으로 chapter 연속 가능
- → preserve fallback (section-local root reassignment)

**(c) legitimate cross-section continuation**:
- 양식 표지 + 본문처럼 정당한 cross-section sequence 후보
- 예: cover section의 chapter title이 본문 section의 paragraph parent
- → preserve fallback (13.7b 초기 보수적, evidence 후 nested 정책 확장 검토)

**13.7b 초기 정책**:
- (a)만 hard fail (Level 3)
- (b)/(c)는 둘 다 preserve fallback로 처리 (양식 evidence 부족 — 구분 자체가 양식 evidence 누적 후)
- evidence 누적 후 (c)의 nested 정책 확장 검토 (별도 stage)

분류 자체는 AI 또는 heuristic + AI 둘 다 가능. **13.7b 초기는 보수적: heuristic으로 (a) 명백 case만 fail, 그 외는 (b)/(c) 구분 없이 preserve fallback**. AI 분류 도입은 evidence 후.

### 10.3 Level 3 — Validation Fail (B4, (a)만)

B4 target_unit_plan validation 단계:
- Level 2의 (a) 카테고리 > 0 → **validation fail**
- fail 결과: 해당 양식은 13.7b validation fail로 처리. assemble 진행 X.
- debug에 violation detail 남김 (§10.4 schema)

**cache invalidate의 위치**:
- cache invalidate는 **해결책이 아닌 보조 조치**. 1a 분석이 동일한 결과를 주면 cache invalidate 후 재분석해도 같은 violation 발생.
- 실제 해결은 1a 정확도 개선 또는 cross-section parent fallback policy 도입 (later).
- 사용자에게 cache invalidate를 자동 권고 X. 디버깅 시 옵션으로만.

### 10.4 Debug schema

```python
{
  "cross_section_parent_violations": [
    {
      "section_id": int,
      "section_local_paragraph_idx": int,
      "document_global_paragraph_idx": int,
      "reported_parent_idx": int,
      "expected_parent_section": int,
      "actual_parent_section": int | "out_of_range",
      "text_preview": str,
    }, ...
  ],
  "cross_section_parent_violation_count": int,
  "cross_section_parent_classifications": {
    "a_out_of_range": [int, ...],   # violation indices
    "b_section_break_ambiguous": [int, ...],
    "c_legitimate_continuation_candidate": [int, ...],
  }
}
```

---

## 11. Cache Invalidation 정책

### 11.1 Bump 시점 — B1 진입 직전

`cache_schema_version` bump (예: v4 → v5)를 **B1 (extract_section_xml 확장) 구현 직전** 실행.

이전 §14 step 14 → step 5 (B1 직전)로 이동.

이유: multi-section 1a 구조 변경 후 기존 cache 신뢰 X. B1~B5 구현 중 cache hit/miss 섞이는 위험 차단.

### 11.2 Migration 안 함

이유:
- 양식 수 적음 (3개)
- multi-section schema 변경이라 기존 cache 끌고 가는 게 더 위험
- 재분석 비용 < migration 복잡도

### 11.3 Cache 영향

13.7b 진입 후:
- 3개 양식 모두 재분석 1회 필요
- 토큰 비용 증가 (section별 full 1a — 민원인 5배)
- 사용자 양식 실행 시 자동

---

## 12. 13.7c 재실행 정책 — 완전 re-run

13.7b로 chapter 수 확장 시 (특히 민원인 section4 본문성 chapter 추가) 13.7c는 **완전 re-run**. 부분 재실행 폐기.

### 12.1 정책

- 기존 chapter (예: 민원인 Ch0~Ch7)의 `adaptation_decision` 유지 X
- 13.7b 후 chapter/region 구조 변경 → 13.7c 완전 re-run
- chapter_idx mapping table 폐기 (복잡도 > 재호출 비용)

### 12.2 Implementation

13.7c 코드 변경 없음. DB tool에서 chapter list가 multi-section seed 결과를 그대로 받아 13.7c 호출. 양식 1회 추가 호출 (AI 2회 batch).

### 12.3 비용

양식 3개 기준:
- 민원인: 1회 (chapter 확장 가능)
- 조달청: 변화 0 (cache hit으로 0회 가능, multi-section 효과 없음)
- CC7: 미진입 (shallow)

### 12.4 Watch

양식 5+ 또는 chapter 30+ 시점에서 mapping table 도입 재검토. 현재는 완전 re-run이 단순 + 안전.

### 12.5 reference_metrics schema 변경

13.7c chapter object `_debug.reference_metrics`에 `section_id` 키 추가 (§4.7):
```python
{
  "chapter_idx": int,
  "section_id": int,  # 13.7b 신규
  ...
}
```

B3 merge 후 chapter object section_id 실 값에서 자연 채워짐. 13.7c reference_metrics 측정 logic 자체 변경 X.

### 12.6 검증

- 13.7b 후 민원인 chapter 수 (예: 8 → 8+N)
- 13.7c가 모든 chapter (기존 + 새) 대해 adaptation_decision 생성
- chapter_object.target_region_id 호환 (region_id 재부여 후 13.7c re-run)
- reference_metrics에 section_id 키 채워짐

---

## 13. 검증 조건

### 13.1 기능적

- B0a/B0b measurement 작동 + debug 채워짐
- B0a/B0b/merge_decision review artifact 3개 작성됨
- B1: 모든 section XML 반환
- B2: section별 1a 결과 채워짐 + section_role_proposal AI sub-step 작동
- B3: document-level merge 결과 schema (§4.6) 채워짐, cross_section_parent_violations 기록, lineage 필드 채워짐, source_diagnostic_keys 채워짐, B3.1/B3.2 sub-step 실행
- B4: section-aware target_unit_plan, cross-section region validation fail (§10.3 (a)) 작동
- B5: chapter object section_id 실 값, 해당 section에 placement, B5.1 default 적용
- B6: cache invalidate + 재분석 작동 (B1 진입 직전 bump)
- B7: source diagnostic (section, chapter) 단위 + 13.7c reference_metrics section_id 키

### 13.2 구조적 (양식 3개)

#### 13.2.1 양식별 결과

- 민원인 (multi-section, 5 sections):
  - section1~4 분석 결과 채워짐
  - section_role_proposals 4개 (section1~4) 생성
  - section4 "제2장" 처리 결과 (Case A/B/C/D 중 어느 case 적용됐는지 명시)
  - cross_section_parent_violations: (a)는 0 OR validation fail, (b)/(c)는 preserve fallback
  - invariant_violations 0
- 조달청 (single-section, 1 section):
  - regression 없음
  - section_id 0 그대로
  - section_role_proposals 1개 (section0)
- CC7 (single-section, 1 section, shallow):
  - shallow path 그대로 (13.7c 미진입과 동일)
  - regression 없음
- cache invalidate 후 재분석 정상 (3개 양식)

#### 13.2.2 B5 placement invariant — 절대 깨지면 안 됨

- section count before/after 동일
- section 순서 유지
- secPr carrier 문단 보존
- section별 orientation/margin/page layout 유지
- remove/append가 자기 section 범위 밖으로 나가지 않음
- generated content가 section0으로만 몰리지 않음 (section_id별 분포 검증)
- preserve 영역과 generated 영역 중복 삽입 없음
- chapter object section_id가 실제 placement section과 일치
- empty chapter region 전체 preserve 보장 (13.7a 안전장치 유지 — 단 Case A/B/C/D로 정밀화)

#### 13.2.3 XML/HWPX 구조 diff — debug 성공 ≠ HWPX 정상의 최후 안전망

- section 개수 before/after 동일
- secPr 개수 before/after 동일
- section별 paragraph count 변화 합리적:
  - preserve section: 변화 0
  - generate section: 예측 변화 (chapter object 기반)
- section별 table count 변화
- generated insertion 분포 (section_id별)
- removed paragraph 분포 (section_id별)
- preserved section paragraph 수
- duplicated text 후보 0
- empty generated chapter가 원본 body 삭제 X

#### 13.2.4 Shallow route regression 기준 — CC7

- 여전히 `flat_legacy` path
- chapter object 생성 0
- 13.7c adaptation_plan 미진입 (chapter_template_plan_seed=None)
- section-aware 변경 후 shallow `preserve_indices` 동일
- marker rewrite/helper가 chapter route 전용으로만 적용 (shallow에 누출 X)
- B1 변경으로 인한 section_xml extraction 영향 0 (single section은 그대로)

### 13.3 의미적 (사용자 눈검증)

- 민원인 section4 처리 결과 (Case A/B/C/D 중 어느 case 적용됐는지) 합리성
- section1~4 preserve 정책이 "안전장치"에서 "분석 기반"으로 전환됨이 debug에 명시
- section_role_proposals 결과 합리성 (양식 의미상)
- 13.7c가 새 chapter 포함해서 adaptation_plan 생성
- B5.1 default (large region 전체 replace 안 됨) 적용 여부

---

## 14. 구현 순서

| # | 작업 | 위험 | 비용 |
|---|------|------|------|
| 1 | docs/13_7b_plan.md patch (이 작업) | 작음 | - |
| 2 | B0a (pre-1a measurement + review artifact schema) 구현 (debug-only) | 작음 | AI 호출 0 |
| 3 | B0a 양식 실행 (3개) | - | AI 호출 0 |
| 4 | **B0a 결과 검토 + `13_7b_b0a_observation.json` review_decisions 기록 (사용자 + claude review point)** | - | - |
| 5 | **B6 Cache schema bump (B1 진입 직전)** | 작음 | - |
| 6 | B1 (extract_all_sections_xml 신규, backward compat) | 작음 | - |
| 7 | B2 (section별 full 1a baseline + section_role_proposal AI sub-step) | 중 | **민원인 5배** |
| 8 | B2 양식 실행 (3개) | - | 토큰 비용 |
| 9 | B0b (post-1a measurement + review artifact schema) | 작음 | AI 호출 0 |
| 10 | **B0b 결과 검토 + 4개 정책 합의 + `13_7b_b0b_observation.json` review_decisions 기록 + b3_entry_authorized=true (사용자 + claude review gate)** | - | - |
| 11 | B3 설계 finalize commit (B0b evidence 기반) | 작음 (설계) | - |
| 12 | B3 구현 (B3.0 cross-section detection + B3.1 chapter intent merge + B3.2 13.4b multi-section regenerate + B3.3 output schema with lineage + B3.4 merge_decision.json) — **step 10 review 없이 진입 금지** | **가장 큼** | - |
| 13 | B4 (section-aware target_unit_plan + Level 3 validation) | 중 | - |
| 14 | B5 (chapter object placement + B5.1 large-region replacement default) | 중 | - |
| 15 | B7 (source diagnostic 확장 + 13.7c reference_metrics section_id) | 작음 | - |
| 16 | 검증 (3개 양식, 13.7c 완전 re-run, B5 invariant, XML/HWPX diff, shallow regression) | - | 토큰 비용 |
| 17 | ROADMAP/handoff/13.7c 호환 patch | - | - |

각 review point에서 사용자 + claude 합의. 진행 안전성 확보.

**Cache bump (step 5)는 step 4 review 통과 후, step 6 (B1) 직전에 실행.** 양식 실행 후 자동 invalidate, 다음 양식 실행 시 재분석.

---

## 15. 하지 않을 것

| 항목 | 이유 |
|------|------|
| Section significance-driven / lightweight 1a (initial) | A baseline 우선. 비용 최적화는 baseline 후 |
| Numeric threshold 자동 결정 | 원칙 §2.8 |
| Section role 자동 분류 (heuristic enum 기반) | 원칙 §2.9. AI sub-step + free-form 두 필드만 |
| section_type enum 기반 code 정책 자동 매핑 | 원칙 §2.9. enum은 `_debug.reference_label`로만 |
| Cross-section region 자동 split | 13.7b 초기 보수적. (a)만 validation fail, (b)/(c)는 preserve fallback |
| Cross-section parent (b)/(c) 구분 자동 분류 | 양식 evidence 부족. 13.7b 초기는 둘 다 preserve fallback. evidence 후 확장 |
| Cache migration | 양식 수 적음, schema 변경 크면 invalidation이 안전 |
| 13.7c prompt 재설계 | 13.7c는 호환 유지 (chapter list 확장만) |
| 부분 adaptation_decision mapping table | 13.7c 완전 re-run 정책. 양식 5+ 시점 재검토 |
| Super-planner 통합 (13.4b + 2a + 13.7c) | 15+ 이후 |
| User_request input 추가 | 14단계 진입 시 13.7c 확장 |
| Source slice (chapter별 source 추출) | 14단계 또는 별도 stage |
| Coverage validation | 15 |
| Large-region replacement detail granularity (slot fill / subtree mix) | 13.7b 초기는 default 보수적 동작만. detail은 양식 evidence 누적 후 |
| section index 또는 role_cluster 번호 하드코딩 | 원칙 2 |
| code의 "충돌 크기" 자체 판단 | 원칙 §2.2 원칙 11. AI ambiguity / low confidence / 구조 위반만 preserve fallback |

---

## 16. 원칙 준수 확인

| 원칙 | 13.7b |
|------|-------|
| 1. 임시 땜질 X, 최종 구조 우선 | O — section preserve 안전장치 → 분석 기반 판단 전환 |
| 2. 하드코딩 X | O — section_type enum 폐기 (free-form 두 필드 + evidence), fallback unknown/other 폐기, numeric threshold hard rule X |
| 3. Template-first | O — section 분석 후에도 template intent flow가 frame |
| 4. Chapter-local pattern preservation | O — section별 local_pattern 유지, B3.2 document-level seed regenerate |
| 5. Multi-section section-aware | O — 본 stage의 핵심 |
| 6. 책임 분리 | O — B0a/B0b (measurement), B1~B2 (analysis), B3 (merge), B4~B5 (assembly), B6 (cache), B7 (diagnostic). 13.7c는 완전 re-run로 책임 명확 |
| 7. 로그/검증 가능성 | O — B0a/B0b/B3.4 review artifact 3개, lineage 핵심 필드, section_role_proposal evidence, cross_section_parent_classifications |
| 8. blocker/watch/later | O — coverage/source slice/user_request/large-region detail은 별도 stage |
| 9. AI 자유도 줄이기 | O — section_role_proposal schema-constrained (free-form 두 필드 + evidence + ambiguity 강제) |
| 10. AI 호출 수 < 품질 | O — section별 full 1a baseline. 비용 부담 감수 |
| 11. 출력 schema 안정화 | O — chapter object section_id 실 값, region multi-id field, lineage 핵심 필드 |
| 13. validation hard gate 신중 | O — cross-section parent 3단계 정책, (a)만 hard fail, 그 외 preserve fallback |
| 14. 근거 있는 일반화 | O — B0a/B0b evidence 기반 B3 설계, section_role_proposal enum 폐기로 양식 evidence 누적 가능 |
| 16. 측정 후 구현 | O — B0a/B0b가 핵심, review artifact로 합의 기록 |
| 18. 측정은 결정 미루기 X | O — B0a/B0b 결과로 B3 설계 finalize, 진행 |
| 19. 설계 검토와 구현 분리 | O — 본 설계서 + 각 review point + 3개 review artifact |
| 20. 관측용 코드도 최종 구조 | O — B0a/B0b/lineage/review_decision schema 명시 |
| 21. AI는 제한된 schema 적극 활용 | O — section_role_proposal schema (free-form + evidence 강제) |
| 22. AI/code 역할 분리 | O — AI는 의미 판단 (structural_relationship/placement_recommendation), code는 구조 검증 (cross-section parent, schema, ambiguity 기반 fallback). code는 "충돌 크기" 판단 X (원칙 11) |
| 23. allocation/coverage 분리 | O — B7 evidence만, allocation redesign X |
| 25. production / debug 구분 | O — B0a/B0b/section_role_proposal/lineage는 debug + 일부 production 영향, production HWPX 본문에 메모 X |
| 26. Route별 검증 경로 존중 | O — shallow route (CC7) 불변 (§13.2.4), single-section (조달청) regression 0 |
| 27. 명시적 완료 조건 | O — §13 검증 조건 명시 (B5 invariant + XML diff + shallow regression 포함) |

---

## 17. 비용 추정

### 17.1 토큰 비용 (민원인 기준)

13.7a/13.7c 후 현재 (1회 양식 실행):
- 1a~1f (section0): 약 5회 AI 호출
- 13.6-A (multi_section_diagnostic): 1회
- chapter_template_plan_seed (13.4b): code only
- 13.7c (source_inventory + chapter_mapping batch): 2회
- 2b (chapter loop, 민원인 8 chapters, 5 ok in 13.7c result): 5회 (preserve 3는 skip)
- 합계: 약 13회 AI 호출

13.7b 진입 후:
- 1a~1f × 5 sections: 약 25회 (B0a review에서 skip 결정된 section은 제외)
- 13.6-A: 1회 (또는 B0a 통합)
- B2 section_role_proposal AI sub-step: 1회 (batch)
- B0a/B0b: 0회 (debug-only, AI 호출 없음)
- B3.2 document-level seed regenerate: 1회 (또는 code-only로 진행 — B3 설계에서 finalize)
- 13.7c 완전 re-run (chapter 수 확장): 2회
- 2b (chapter loop, 예: 8 → 8+N chapters, ok 수 증가 가능): 6~10회
- 합계: 약 40~45회 AI 호출

**약 3~3.5배 증가**. cache hit 시 1a~1f skip 가능.

### 17.2 양식별

- 민원인 (5 sections): 5배 1a 비용 + section_role_proposal + 13.7c 완전 re-run
- 조달청 (1 section): 거의 동일 (single section에서는 multi-section overhead 없음, 13.7c cache hit으로 0회 가능)
- CC7 (1 section, shallow): 동일 (shallow는 13.7b 효과 적음, 13.7c 미진입)

### 17.3 사용자 결정

원칙 10 (AI 호출 < 품질). 비용 감수. 단 baseline 후 최적화 검토 (significance-driven, lightweight 등).

---

## 18. 13.7b 한계 (명시)

- AI section_role_proposal 의존 — hallucination 100% 차단 불가
- B3 merge 정책 (role clustering / chapter_types) 결정에 사용자 + claude review 필요 (자동 X)
- 양식 3개 evidence로 일반화 — 양식 추가 시 정책 재검토 가능
- cross-section parent (b)/(c) 구분은 13.7b 초기 미적용 — 둘 다 preserve fallback. evidence 후 확장
- Large-region replacement granularity detail (slot fill / subtree mix) — 13.7b 초기 default 동작만. evidence 후 별도 stage
- user_request 미반영 — 14단계 진입 시 13.7c 확장

→ 13.7b는 **multi-section 정확 이해의 baseline stage**. user_request 또는 generation 품질 final 책임은 13.7b 외부.

---

작성: 2026-05-13
patch: 2026-05-14
