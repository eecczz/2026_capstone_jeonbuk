# 12.2 Target Unit Planning Debug — Design (v2)

## 목적

template의 paragraph들을 의미 있는 region으로 묶고, 각 region에 target_unit_type을 할당하는 debug-only planning.
기존 chapter-only 2a/2b에서 unit-aware generation으로 전환하기 위한 planning contract 확정.

**아직 generation pipeline 변경 없음. debug-only.**

---

## 1. 12.0과의 관계 (명확화)

| | 12.0 | 12.2 |
|-|------|------|
| 수준 | template-level | paragraph-level |
| 질문 | "이 template에서 어떤 unit이 관측되는가" | "각 paragraph가 어떤 region/unit에 속하는가" |
| output | unit_observations (signal 강도) | target_unit_plan (region mapping) |
| derived_mode_label | 사용 | **debug context로만 참조. planning routing으로 사용 금지.** |

### derived_mode_label 사용 원칙 (12.2에서도 유지)

- `derived_mode_label`은 debug summary일 뿐
- 12.2에서 `if label == "shallow_report": ...` 분기 금지
- 실제 planning은 `unit_observations`, paragraph facts, code proposal, AI evidence를 기준으로 수행
- label은 `template_context`에 참고 정보로만 포함

---

## 2. Target Unit Types

| unit_type | 정의 | generation 방식 (13단계 예상) |
|-----------|------|-------------------------------|
| `chapter` | 독립 content tree (deep hierarchy) | tree-based generation (현재 2b 방식) |
| `shallow_block` | 얕은 block/bullet (depth ≤ 2~3) | flat list generation |
| `table` | 독립 표 채우기 region | cell/row-based fill |
| `slot` | 고정 위치 (제목, 날짜, 기관명 등) | direct mapping (AI 불필요 가능) |
| `attachment` | 붙임/첨부 영역 | 별도 처리 또는 skip |

### Section Boundary

section은 target unit이 아니라 physical layout container.
region metadata에 `section_span`으로 참고 정보만 기록.

---

## 3. Region Schema

```json
{
  "region_id": 0,
  "unit_type": "slot | shallow_block | chapter | table | attachment",
  "paragraph_indices": [0, 1],
  "role_ids": ["role_cluster_0", "role_cluster_1"],
  "description": "header: 결재/배포 정보 + 문서 제목",
  "section_span": [0],
  "generation_strategy_hint": "direct_mapping | flat_block | tree_generation | table_fill | skip",
  "confidence": "high | medium | low",
  "evidence": ["사용한 signal/field"],

  "internal_structure": {
    "has_substructure": false,
    "child_roles": [],
    "subregion_candidates": [],
    "depth_range": [0, 0],
    "repeatable_roles_in_region": []
  },

  "table_handling": {
    "contains_table": false,
    "table_role_ids": [],
    "content_table_candidate_count": 0,
    "table_handling_hint": "independent_region | embedded_in_region | layout_only | not_applicable"
  },

  "proposal_action": "accepted | adjusted | split | merged | rejected",
  "adjustment_reason": null,
  "supporting_evidence": [],
  "counter_evidence": []
}
```

### internal_structure (shallow_block 세분화 준비)

13단계에서 shallow_block generation을 하려면 내부 구조가 필요.
12.2에서 완전 generation은 안 하지만, 아래 힌트는 남김:

- `child_roles`: region 안에 존재하는 role 목록
- `subregion_candidates`: 반복 pattern이나 heading으로 더 나눌 수 있는 후보
- `depth_range`: region 내 paragraph depth 범위
- `repeatable_roles_in_region`: repeatable=true인 role (flat generation 시 확장 가능)

### table_handling (table 역할 구분)

| hint | 의미 |
|------|------|
| `independent_region` | table 자체가 독립 target_unit region |
| `embedded_in_region` | 다른 region (shallow_block 등) 안에 table이 포함됨 |
| `layout_only` | 텍스트 박스/서식용 table, content filling 불필요 |
| `not_applicable` | table 없음 |

---

## 4. Implementation Approach

### Code Proposal → AI Confirm → Code Validation

```
1. Code: propose_template_regions(structure, cache_data, unit_observations)
   - header detection (first_chapter_idx 이전)
   - attachment detection (keyword: 붙임, 첨부, 별첨)
   - table region detection (content_table_candidate가 독립 block일 때)
   - body = remaining paragraphs
   - body를 chapter vs shallow_block으로 분류: unit_observations 참고

2. AI: code proposal + paragraph descriptions + 12.0 observations → 확정/조정
   - region boundary 조정
   - unit_type 변경
   - internal_structure 힌트 추가
   - table_handling 판단
   - generation_strategy_hint 부여

3. Code: validate_target_unit_plan(plan, paragraphs)
   - coverage check: 모든 paragraph가 region에 할당됐는지
   - overlap check: 동일 paragraph가 2개+ region에 있는지
   - granularity check: CC7에서 chapter 강제 분할 감지
   - schema validation
```

### AI 활용 범위 (적극 활용, 캐시 가능)

12.2는 structure/planning 단계이므로 같은 template은 cache 재사용.
따라서 AI를 아끼지 않고 충분히 정확한 판단을 남기는 것이 중요.

AI가 판단해야 하는 것:
- 애매한 region boundary (code proposal이 제안하지만 AI가 확정)
- shallow_block 내부 구조 (heading/bullet/note 구분)
- table이 독립 region인지 embedded인지
- slot/header/attachment 경계
- section boundary가 단순 layout인지 의미 있는 container인지
- 기존 2a chapter split과 target unit plan이 왜 다른지 (planning_notes)
- generation_strategy_hint 결정

---

## 5. AI Prompt 구조

```python
TARGET_UNIT_PLANNING_PROMPT = """당신은 template 구조를 분석하여 target unit region을 확정하는 planner입니다.

아래에:
1. template paragraphs (idx, role, level, description)
2. code proposal (1차 region 분할안)
3. 12.0 unit observations (template-level signal)

이 주어집니다. 이것을 바탕으로 최종 target_unit_plan을 확정하세요.

## 핵심 규칙

1. **code proposal은 suggestion이지 확정이 아닙니다.** boundary를 조정하거나 unit_type을 바꿀 수 있습니다.
2. **proposal을 바꾸면 반드시 evidence와 reason을 남기세요.**
3. **모든 paragraph가 정확히 1개 region에 포함되어야 합니다.** 누락/중복 금지.
4. **chapter 강제 분할을 하지 마세요.** depth가 얕고 구조가 flat하면 shallow_block으로 두세요.
5. **table handling을 명확히 하세요.** content table이면 independent_region 또는 embedded_in_region, layout table이면 layout_only.
6. **internal_structure 힌트를 남기세요.** 특히 shallow_block region에서 heading/bullet/note 등의 하위 구조.
7. **숫자 threshold로 판단하지 마세요.** paragraph facts와 12.0 evidence를 종합 판단.
8. **generation_strategy_hint는 확정이 아니라 hint입니다.** 13단계에서 독립적으로 결정.
9. **confidence와 ambiguity_flags를 정직하게 남기세요.**

## 출력 형식

반드시 아래 JSON만 출력하세요.

```json
{
  "regions": [
    {
      "region_id": 0,
      "unit_type": "slot | shallow_block | chapter | table | attachment",
      "paragraph_indices": [0, 1, ...],
      "role_ids": ["role_cluster_0", ...],
      "description": "이 region의 역할 요약",
      "generation_strategy_hint": "direct_mapping | flat_block | tree_generation | table_fill | skip",
      "confidence": "high | medium | low",
      "evidence": ["근거 fields/signals"],
      "internal_structure": {
        "has_substructure": true/false,
        "child_roles": [...],
        "subregion_candidates": [...],
        "depth_range": [min, max],
        "repeatable_roles_in_region": [...]
      },
      "table_handling": {
        "contains_table": true/false,
        "table_role_ids": [...],
        "content_table_candidate_count": 0,
        "table_handling_hint": "independent_region | embedded_in_region | layout_only | not_applicable"
      },
      "proposal_action": "accepted | adjusted | split | merged | rejected",
      "adjustment_reason": "바꾼 이유 (있으면)",
      "supporting_evidence": [...],
      "counter_evidence": [...]
    }
  ],
  "planning_notes": ["전체 planning에 대한 메모"],
  "ambiguity_flags": ["판단이 모호한 지점"]
}
```
"""
```

---

## 6. Validation

### coverage / overlap 체크 (기본)

```python
def validate_target_unit_plan(plan: dict, paragraphs: list) -> dict:
    all_indices = set(p.get("idx") for p in paragraphs)
    covered = set()
    overlaps = []

    for region in plan["regions"]:
        for idx in region["paragraph_indices"]:
            if idx in covered:
                overlaps.append(idx)
            covered.add(idx)

    uncovered = all_indices - covered
    ...
```

### granularity 체크

| check | 조건 | severity |
|-------|------|----------|
| `shallow_body_over_split_into_chapters` | unit_obs가 shallow_block=strong인데 plan에 chapter region 있음 | blocker |
| `chapter_body_under_split` | unit_obs가 chapter=strong인데 plan에 chapter 없음 | blocker |
| `too_many_regions` | region > paragraph_count * 0.5 | warning |
| `too_few_regions` | region = 1 (전체가 하나) | warning |
| `table_region_over_promoted` | layout table이 independent region으로 승격 | warning |
| `slot_region_over_promoted` | body paragraph가 slot으로 잘못 분류 | warning |

---

## 7. Expected Results (3 Templates)

### CC7 (shallow_report)

| region | unit_type | paragraphs | description |
|--------|-----------|-----------|-------------|
| 0 | slot | idx 0~1 | header (결재/배포 + 제목) |
| 1 | shallow_block | idx 2~20 | main body (대분류→항목→보충, depth 1~3) |
| 2 | attachment | idx 21~22 | 첨부 통계표 |

internal_structure (region 1):
- has_substructure: true
- child_roles: [c2, c3, c4, c5, c6, c7, c8]
- subregion_candidates: c2가 heading이므로 c2 기준으로 sub-block 분리 가능

table_handling (region 1):
- contains_table: true (c6가 표 위치)
- table_handling_hint: embedded_in_region

table_handling (region 2):
- contains_table: true (c10이 통계표)
- table_handling_hint: embedded_in_region (attachment 내)

### 조달청 (chapter_generation)

| region | unit_type | paragraphs | description |
|--------|-----------|-----------|-------------|
| 0 | slot | idx 0~3 | header (제목, 날짜, 목차) |
| 1+ | chapter | 각 chapter body | 기존 2a/2b와 호환 |

### 민원인 (chapter_generation)

조달청과 유사. slot + chapter regions.

---

## 8. Legacy Chapter Comparison

```json
{
  "legacy_chapter_comparison": {
    "legacy_2a_chapter_count": 3,
    "plan_region_count": 3,
    "plan_content_regions": 1,
    "unit_type_match": false,
    "mismatch_type": "unit_type_mismatch",
    "detail": "2a는 3 chapters, plan은 slot+shallow_block+attachment",
    "source_allocation_impact": "high",
    "recommendation": "shallow_block 기반 단일 generation이 적합"
  }
}
```

---

## 9. Cache Strategy

- `structure["target_unit_plan"]`에 저장
- `target_unit_planner_version = "v0.1"`
- version 불일치 시 AI 재호출
- CACHE_SCHEMA_VERSION 미변경

```python
CURRENT_PLANNER_VERSION = "v0.1"

cached_plan = structure.get("target_unit_plan")
plan_valid = (
    cached_plan
    and cached_plan.get("planner_version") == CURRENT_PLANNER_VERSION
    and cached_plan.get("validation", {}).get("valid", False)
)
```

---

## 10. Debug Output: `15_target_unit_planning.json`

```json
{
  "schema_version": 1,
  "planner_version": "v0.1",
  "debug_only": true,

  "template_context": {
    "paragraph_count": 23,
    "derived_mode_label": "shallow_report",
    "unit_observations_summary": [
      {"unit_type": "table", "observed_role": "strong_candidate"},
      {"unit_type": "chapter", "observed_role": "moderate_candidate"},
      {"unit_type": "attachment", "observed_role": "background_signal"}
    ]
  },

  "code_proposal": {
    "regions": [
      {"region_id": 0, "unit_type": "slot", "paragraph_indices": [0, 1], "method": "header_before_first_chapter"},
      {"region_id": 1, "unit_type": "body_undetermined", "paragraph_indices": [2, "...", 20], "method": "remaining_body"},
      {"region_id": 2, "unit_type": "attachment", "paragraph_indices": [21, 22], "method": "attachment_keyword_detection"}
    ]
  },

  "ai_plan": {
    "regions": ["... full region objects with all fields ..."],
    "planning_notes": [],
    "ambiguity_flags": []
  },

  "validation": {
    "all_paragraphs_covered": true,
    "no_overlap": true,
    "granularity_checks": {
      "shallow_body_over_split_into_chapters": false,
      "chapter_body_under_split": false,
      "too_many_regions": false,
      "too_few_regions": false
    },
    "valid": true,
    "blockers": [],
    "warnings": []
  },

  "legacy_chapter_comparison": {"..."},

  "cache_status": {
    "plan_cache_hit": false,
    "plan_cache_written": true,
    "planner_version_matched": false
  },

  "ai_call_info": {
    "attempts": 1,
    "success": true,
    "error": null
  }
}
```

---

## 11. Blocker / Watch / Later

### Blocker

| 조건 | 의미 |
|------|------|
| paragraph 누락 (uncovered) | planning 불완전 |
| paragraph overlap | 구조 오류 |
| CC7에서 body가 chapter로 계획됨 | 12.0 observation 무시 |
| 조달청/민원인에서 chapter 사라짐 | 기존 pipeline regression |
| AI output parse 실패 + retry 실패 | planning 불가 |

### Watch

| 조건 | 의미 |
|------|------|
| table_handling_hint ambiguous | content vs layout 미분리 |
| attachment confidence low | 감지 heuristic 한계 |
| shallow_block internal_structure 빈약 | 13단계에서 추가 분석 필요 |
| too_many_regions warning | region 과분할 |

### Later

| 항목 | 시점 |
|------|------|
| source-to-region allocation | 13/14 |
| region별 generation 실행 | 13 |
| 2a prompt 변경 | 13 |
| slot direct mapping 구현 | 13 |
| table cell fill 구현 | 13 |

---

## 12. Implementation Scope

### Do

1. `template_observer.py`에 추가 (또는 별도 `target_unit_planner.py`):
   - `propose_template_regions(structure, cache_data, unit_observations)` — code proposal
   - `build_target_unit_planning_prompt(proposal, paragraphs, unit_observations)` — AI prompt
   - `parse_target_unit_plan_from_llm(raw)` — parse
   - `validate_target_unit_plan(plan, paragraphs, unit_observations)` — validation
   - `compute_legacy_comparison(plan, pipeline_context)` — 2a 비교

2. Cache: `structure["target_unit_plan"]` + `target_unit_planner_version`

3. Debug output: `15_target_unit_planning.json`

4. DB tool: planning 호출 (12.0 observation 이후, 2a 이후)

### Do Not

- 2a prompt 변경
- 2b generation 변경
- assemble 변경
- source allocation
- generation 실행
- CACHE_SCHEMA_VERSION 변경
- validation hard gate
- `derived_mode_label` 기반 pipeline 분기

### Module Location

`template_observer.py`에 넣을지 별도 `target_unit_planner.py`로 분리할지:

**별도 `target_unit_planner.py` 추천.**
- template_observer는 12.0 observation 담당 (template-level)
- target_unit_planner는 12.2 planning 담당 (paragraph-level)
- 책임이 다름. 한 파일에 모두 넣으면 비대

---

## 13. 검증 기준

- CC7: slot + shallow_block + attachment (chapter 아님)
- 조달청: slot + chapter (기존 호환)
- 민원인: slot + chapter (기존 호환)
- 모든 paragraph covered, overlap 없음
- granularity blocker 없음
- legacy_comparison에서 CC7 mismatch 감지
- cache HIT 재사용 확인

---

## 14. Test Strategy

```
1. 구현: target_unit_planner.py + debug output + DB tool
2. 3개 양식 한 번에 검증
3. 결과 분석:
   - region coverage / overlap
   - unit_type 기대 일치
   - internal_structure / table_handling 의미 있는 값 존재
   - legacy_comparison mismatch 감지 (CC7)
   - cache HIT 재사용
4. blocker 있으면 수정, 없으면 12.2 완료
```
