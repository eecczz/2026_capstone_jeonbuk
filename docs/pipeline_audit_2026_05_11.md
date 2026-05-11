# HWPX Pipeline Audit (2026-05-11)

13.3b-1 완료 후, 13.5 attachment preserve 설계 전 전체 파이프라인 점검.

---

## 한 줄 결론

파이프라인은 다음 단계로 갈 수 있는 상태이나, **chapter route에서 preserve_indices 미적용 (attachment 삭제)** 이 가장 큰 구조적 결함이며, 이를 해결하면 양 route의 assemble 정책이 통일되고 table/section 문제 해결의 기반이 된다.

---

## 1. Pipeline 단계별 흐름

### 1.1 Template Analysis (캐시 대상, AI 5~6회)

| 단계 | 함수 | AI | AI input | AI output | code facts | validation | debug file | 다음 전달 | known issue |
|------|------|-----|---------|-----------|-----------|------------|-----------|----------|-------------|
| 1a 문단분석 | `build_structure_analysis_prompt` | O | compact XML | `[{idx, description, marker, table_info}]` | paragraph_styles (paraPr/charPr) | 없음 | 01 | structure.paragraphs | - |
| 1b 역할후보 | `build_role_candidate_prompt` | O | 1a paragraphs | role_candidates per paragraph | - | 없음 | 02 | structure.paragraphs에 병합 | - |
| 1c 레벨/부모 | `build_level_hybrid_prompt` | O | 1a+1b paragraphs + features | `{decisions: {idx: {level, parent_idx}}}` | same_paraPr_run, marker_family | 없음 | 03 | structure.paragraphs에 병합 | - |
| 1e 클러스터링 | `build_canonical_clustering_prompt` | O | paragraphs table (14 columns) | `{clusters: {role: [idx_list]}}` | - | no missing/duplicate idx | 04 | structure.paragraphs.role 확정 | repair 호출 가능 |
| 1f 마커정책 | `build_marker_policy_prompt` | O | role별 text samples | `{roles: [{role, policy_type, markers}]}` | - | `verify_marker_policy_evidence` | 05c | structure.marker_policy_1f | - |

### 1.2 Code-Driven Extraction (캐시 대상, AI 없음)

| 단계 | 함수 | code facts | validation | debug file | 다음 전달 | known issue |
|------|------|-----------|------------|-----------|----------|-------------|
| grammar | `extract_template_grammar` | global/per_type grammar, singleton/repeatable/optional | - | (structure에 포함) | template_grammar | c8 parent ambiguity (grammar 허용) |
| text_type | `classify_role_text_types` | heading/body/supporting/summary per role | - | (structure에 포함) | role_text_types | CC7 c2는 "summary" (keyword "요약") |
| chapter_types | `_build_chapter_types` | type별 pattern tree, title_role | - | (structure에 포함) | chapter_types | level 0/1 heuristic |
| semantics | `build_per_type_role_semantics` | per-type description aggregation | - | (structure에 포함) | per_type_role_semantics | debug-only |

### 1.3 Cache Write + Validation

| 단계 | 함수 | 역할 | debug file |
|------|------|------|-----------|
| cache gate | `validate_structure_for_cache` | SC1~SC5 blocker, SC6~SC9 watch | 05b_cache_validation.json |
| cache save | `save_template_cache` | structure + signals + idx_texts + idx_full_texts + chapter_types + marker_policy_1f | /tmp/hwpx_cache/{hash}.json |

### 1.4 Template Observation (12.0, 캐시 대상)

| 단계 | 함수 | AI | AI input | AI output | code facts | debug file | 다음 전달 |
|------|------|-----|---------|-----------|-----------|-----------|----------|
| unit observation | `extract_template_unit_features` | code | paragraphs + cache | features dict | paragraph_count, table_signal, structural_signals | - | AI prompt 입력 |
| unit observation | `build_template_unit_prompt` | O | features + paragraphs | `{unit_observations, derived_mode_label}` | - | 13_template_unit_observation.json | structure.template_unit_observation |

### 1.5 Target Unit Planning (12.2, 캐시 대상)

| 단계 | 함수 | AI | AI input | AI output | code facts | debug file | 다음 전달 |
|------|------|-----|---------|-----------|-----------|-----------|----------|
| region proposal | `propose_template_regions` | code | paragraphs + chapter_types | chapter_title_candidates, attachment_candidates | evidence per candidate | - | AI prompt 입력 |
| planning | `build_target_unit_planning_prompt` | O | proposal + paragraphs + unit_obs | `{regions: [{region_id, unit_type, paragraph_indices, internal_structure, table_handling}]}` | - | 15_target_unit_planning.json | structure.target_unit_plan |

### 1.6 Source Processing (캐시 대상 아님, 매 실행)

| 단계 | 함수 | AI | code facts | debug file | 다음 전달 | known issue |
|------|------|-----|-----------|-----------|----------|-------------|
| source_blocks | `text_blob_to_source_blocks` | code | heading split → block list | 16_source_blocks.json | debug-only (generation 미사용) | imbalance (sb_009=52%) |

### 1.7 Route Selection (매 실행)

| 단계 | 함수 | 조건 | 결과 |
|------|------|------|------|
| route decision | `should_use_shallow_route` | no chapter regions + shallow_block primary (>50%) | shallow=True → shallow route, False → chapter route |

### 1.8a Chapter Route (매 실행)

| 단계 | 함수 | AI | AI input | AI output | code facts | validation | debug file | 다음 전달 |
|------|------|-----|---------|-----------|-----------|------------|-----------|----------|
| 2a planning | `build_chapter_classify_prompt` | O | chapter_types catalog + header_roles + source | `{chapters: [{title, type}], header: {role: value}}` | header_roles (`extract_header_roles`) | - | 06_chapter_classify.json | chapters list, header_data |
| source split | `split_source_by_chapters` | code | chapters titles + source text | per-chapter source sections | decision_log | - | 07b_source_split_decision.json | per-chapter source text |
| 2b generation x N | `build_section_fill_prompt` | O | pattern + role_catalog + per-chapter source | `[{id, role, text, parent_id}]` | - | - | 08_section_fill.json | raw items per chapter |
| normalize | `normalize_section_items` | code | raw items | title node injection, id remap | normalize_diff | - | 09_grammar_validation.json | normalized items |
| validate | `validate_ai_parent_ids` | code | normalized items + grammar | invalid markers + stats | parent_id_stats | - | 09_grammar_validation.json | validated items |
| fallback | `apply_parent_id_fallback` | code | invalid items + grammar | corrected parent_ids | recovered_by_fallback | - | - | corrected items |
| grammar check | `reconstruct_tree_from_flat` | code | body items + grammar | violations list | ReconstructionResult | `validate_reconstruction` | 09b_grammar_validation.json | grammar_passed |
| **assemble** | `assemble_hwpx_hybrid` | code | body_items + header_data + chapter_trees | HwpxResult (bytes) | section_info, marker_rewrite_log | - | 10_assemble_result.json | output HWPX |
| | | | **preserve_indices=None** | | | | | **attachment 삭제됨** |

### 1.8b Shallow Route (매 실행)

| 단계 | 함수 | AI | AI input | AI output | code facts | validation | debug file | 다음 전달 |
|------|------|-----|---------|-----------|-----------|------------|-----------|----------|
| 2a (header only) | `build_chapter_classify_prompt` | O | (동일) | header_data만 사용, chapters 무시 | - | - | 06 | header_data |
| seed 추출 | `extract_shallow_section_plan_seed` | code | target_unit_plan + structure + idx_full_texts | seed dict or None | evidence scoring | threshold >= 2 | shallow_section_plan_seed | seed → 2b prompt |
| 2b single-call | `build_section_fill_prompt(shallow_mode=True, section_plan_seed=...)` | O | pattern + role_catalog + broad source + seed | `[{id, role, text, parent_id}]` | - | - | 08b | raw items |
| normalize~grammar | (동일) | code | (동일) | (동일) | (동일) | (동일) | 09 | body_items |
| compliance | `observe_section_plan_compliance` | code | body_items + seed | observation dict | count/order/thin/repetition | debug-only | shallow_section_plan_compliance | - |
| preserve calc | `compute_preserve_indices` | code | target_unit_plan + idx_map | slot + attachment indices | preserve_debug | - | - | preserve_indices |
| **assemble** | `assemble_hwpx_hybrid` | code | body_items + header_data + **preserve_indices** | HwpxResult | section_info | - | 10 | output HWPX |
| | | | **preserve_indices={slot+attachment}** | | | | | **slot/attachment 보존됨** |

### 1.9 Final Debug

| 단계 | 함수 | debug file |
|------|------|-----------|
| validation summary | `build_validation_summary` | 11_validation_summary.json |
| debug summary | `write_stage_debug_files` | 99_debug_summary.json |
| unified | (DB tool) | /tmp/hwpx_debug_last.json |

---

## 2. AI 호출별 Input/Output

| # | 호출 이름 | prompt builder | template facts 입력 | source 입력 | output schema | 정책 반영 | fallback | hallucination 방지 |
|---|----------|---------------|-------------------|------------|---------------|----------|----------|-------------------|
| 1 | 1a structure | `build_structure_analysis_prompt` | compact XML | 없음 | `[{idx, description, marker}]` | 즉시 (structure) | 없음 | schema 강제 |
| 2 | 1b roles | (1b prompt) | 1a paragraphs | 없음 | role_candidates | 즉시 | 없음 | candidate list |
| 3 | 1c levels | `build_level_hybrid_prompt` | 1a+1b + features | 없음 | `{decisions: {idx: {level, parent_idx}}}` | 즉시 | 없음 | feature evidence |
| 4 | 1e clustering | `build_canonical_clustering_prompt` | 14-column paragraph table | 없음 | `{clusters: {role: [idx]}}` | 즉시 (role 확정) | repair prompt | no missing/duplicate validation |
| 5 | 1f marker | `build_marker_policy_prompt` | role별 text samples | 없음 | `{roles: [{policy_type, markers}]}` | 즉시 (marker_policy) | 없음 | `verify_marker_policy_evidence` 교차검증 |
| 6 | 12.0 observation | `build_template_unit_prompt` | features + paragraphs | 없음 | `{unit_observations, derived_mode_label}` | **debug-only** | retry x2 | schema + evidence 요구 |
| 7 | 12.2 planning | `build_target_unit_planning_prompt` | proposal + paragraphs + obs | 없음 | `{regions: [{unit_type, paragraph_indices, internal_structure}]}` | **target_unit_plan** (route decision) | retry x2 | code proposal → AI 최종 판단 |
| 8 | 2a planning | `build_chapter_classify_prompt` | chapter_types + header_roles | PDF text/images | `{chapters: [{title, type}], header: {}}` | 즉시 (chapters, header_data) | 없음 | type catalog 제한 |
| 9 | 2b fill x N | `build_section_fill_prompt` | pattern + role_catalog + rules | per-chapter source | `[{id, role, text, parent_id}]` | 즉시 (body_items) | parent_id fallback | grammar validation + pattern 강제 |
| 10 | 2b shallow | `build_section_fill_prompt(shallow)` | pattern + role_catalog + seed | broad source | `[{id, role, text, parent_id}]` | 즉시 (body_items) | seed=None → 기존 prompt | grammar + seed compliance |

### AI 연결 관계

```
12.0 observation ──→ 12.2 planning input (unit_observations)
12.2 planning ──→ should_use_shallow_route (regions)
12.2 planning ──→ extract_shallow_section_plan_seed (subregion_candidates)
12.2 planning ──→ compute_preserve_indices (slot/attachment regions)

2a planning ──→ split_source_by_chapters (chapter titles)
2a planning ──→ 2b fill (per-chapter source + chapter_type pattern)

section_plan_seed (code) ──→ 2b shallow prompt (heading list)
```

### 현재 generation에 실제 사용되는 AI/observation 정리

| artifact | 생성 주체 | generation에 사용? | 용도 |
|----------|----------|-----------------|------|
| template_unit_observation (12.0) | AI | **간접** — 12.2 planning의 입력 | target_unit_plan이 routing에 사용 |
| target_unit_plan (12.2) | AI | **직접** — route selection + preserve_indices + seed 추출 | shallow/chapter 분기 |
| semantic_tag (11.1) | code heuristic | **미사용** | debug-only observation |
| style_profile (11.2) | AI | **미사용** (비활성화됨) | latency 절감으로 off |
| per_type_role_semantics | code | **간접** — 2b prompt의 role description 보강 | text_type, length_hint |
| section_plan_seed (13.3b-1) | code | **직접** — shallow 2b prompt에 heading list 삽입 | template flow 보존 |

---

## 3. Code vs AI 책임 분리 확인

| 책임 | 담당 | 현재 상태 | 문제점 |
|------|------|----------|--------|
| factual tree extraction | code (1a parser + 1c level) + AI (1a/1b/1c) | **혼합** — AI가 description/marker 추출, code가 style/level 보정 | 정상. AI는 semantic, code는 mechanical |
| role/marker facts | AI (1e clustering, 1f marker) + code (verify) | **분리됨** | 정상. 1f verify가 교차검증 |
| grammar constraints | code (`extract_template_grammar`) | **code only** | 정상 |
| section/region planning | AI (12.2 target_unit_plan) + code (route selection) | **분리됨** | 정상. AI가 region 결정, code가 route 선택 |
| source allocation | code (`split_source_by_chapters`) | **code only** | 2a title 의존. source_blocks 미연결 |
| content generation | AI (2b section fill) | **AI only** | 정상. grammar constraint 내에서 생성 |
| marker rendering | code (marker_separator + assemble rewrite) | **code only** | 정상. Phase 2 content-only + reattach |
| validation | code (grammar validation, parent_id, cache gate) | **code only** | 정상 |
| table preservation | code (assemble: is_tbl_box + preserve_indices) | **code only** | shallow에서만 동작 |
| attachment preservation | code (compute_preserve_indices + assemble) | **code only** | **chapter route 미적용 (blocker)** |
| HWPX assemble | code (assemble_hwpx_hybrid) | **code only** | clone+text swap 병목 |

### 애매하게 섞인 곳

1. **source allocation**: `split_source_by_chapters`는 code이지만 2a AI의 chapter title에 의존. title이 source에 없으면 split 실패. → 2a AI output이 source allocation 품질을 결정하는 구조.
2. **section planning (shallow)**: seed는 code-driven이지만, seed의 입력인 `subregion_candidates`는 12.2 AI output. AI가 heading을 잘못 식별하면 seed가 틀림. → evidence scoring으로 완화되었으나 근본적 의존은 존재.

---

## 4. Dataflow Map

### 4.1 주요 Artifact 추적

| artifact | 생성 | 저장 위치 | 소비자 | 비고 |
|----------|------|----------|--------|------|
| `idx_full_texts` | 1a (`_extract_texts_by_idx(max_chars=None)`) | **cache top-level** | `extract_shallow_section_plan_seed` (13.3), style_profile (11.2, 비활성화) | **structure 안이 아님** |
| `target_unit_plan` | 12.2 AI + parse | **structure 안** (`structure["target_unit_plan"]`) | `should_use_shallow_route`, `extract_shallow_section_plan_seed`, `compute_preserve_indices` | - |
| `source_blocks` | `text_blob_to_source_blocks` | **debug-only** (`_debug_payload["source_blocks"]`) | 없음 (generation 미사용) | 13.1 deferred |
| `role_text_types` | `classify_role_text_types` | **structure 안** | 2b prompt (text_type, length_hint), seed (evidence) | - |
| `marker_policy_1f` | 1f AI + verify | **cache top-level AND structure 안** (중복) | assemble marker rewrite, format_rules, seed marker strip | 중복 저장 |
| `template_grammar` | `extract_template_grammar` | **structure 안** | 2b grammar validation, seed (has_children) | - |
| `chapter_types` | `_build_chapter_types` | **cache top-level AND structure 안** (중복) | 2a prompt, 2b pattern selection | 중복 저장 |
| `section_fill` result | `process_section_fill_result` | debug (`_section_fill_debug[]`) | assemble (body_items), debug | - |
| `shallow_section_plan_seed` | `extract_shallow_section_plan_seed` | debug (`_debug_payload["shallow_section_plan_seed"]`) | 2b prompt (section_plan_seed param) | template_only |
| `shallow_section_plan_compliance` | `observe_section_plan_compliance` | debug (`_debug_payload["shallow_section_plan_compliance"]`) | 없음 (debug-only) | - |
| `preserve_indices` | `compute_preserve_indices` | 변수 (assemble 호출 시 전달) | `assemble_hwpx_hybrid` | **shallow route에서만 계산** |
| `table_text_skipped` | assemble 내부 counter | debug (`10_assemble_result.json`) | 없음 | shallow에서만 발생 |
| `10_assemble_result.json` | `write_stage_debug_files` | /tmp/hwpx_debug/10 | 수동 확인 | marker_rewrite_log 포함 |
| `99_debug_summary.json` | `write_stage_debug_files` | /tmp/hwpx_debug/99 | 수동 확인 | 요약 지표 |

### 4.2 Data Path Mismatch 확인

| field | cache 위치 | DB tool 접근 | 일치? | 비고 |
|-------|----------|-------------|-------|------|
| `idx_full_texts` | top-level | `_cached.get("idx_full_texts")` → `_idx_full_texts` | O | **이번 세션에서 버그 수정** (structure.get → _idx_full_texts) |
| `target_unit_plan` | structure 안 | `structure.get("target_unit_plan")` | O | - |
| `marker_policy_1f` | **양쪽 다** | cache hit: top-level → structure에 merge | O | 중복이지만 일관 |
| `chapter_types` | **양쪽 다** | cache hit: top-level에서 로드, structure에도 있음 | O | 중복이지만 일관 |
| `template_grammar` | structure 안 | `structure.get("template_grammar")` | O | - |
| `role_text_types` | structure 안 | `structure.get("role_text_types")` | O | - |
| `template_unit_observation` | structure 안 | `structure.get("template_unit_observation")` | O | - |

**추가 mismatch 없음.** `idx_full_texts` 버그는 이번 세션에서 해결됨.

---

## 5. Route별 비교

### Chapter Route

| 항목 | 상태 |
|------|------|
| AI planning | 2a: chapter title + type 결정 |
| target_unit_plan 사용 | route selection에만 사용 (False → chapter route 진입) |
| source allocation | `split_source_by_chapters` (2a title 기반) |
| generation | per-chapter 2b x N (N=chapters 수) |
| tree structure | chapter_trees 수집 → assemble에 전달 |
| marker rewrite | chapter_trees 기반 sibling counter |
| **preserve_indices** | **None (미적용)** |
| **attachment 삭제** | target_unit_plan에서 attachment region으로 분류된 문단이 body remove 시 삭제됨. preserve 없음. |
| **삭제 원인** | `removed_indices`가 모든 body paragraph를 포함. attachment도 body로 취급됨. preserve가 없어서 보존 안 됨. |

### Shallow Route

| 항목 | 상태 |
|------|------|
| AI planning | 2a: header_data만 사용, chapters 무시 |
| target_unit_plan 사용 | route selection + seed 추출 + preserve_indices 계산 |
| source allocation | broad source 전체 사용 |
| generation | single 2b call (shallow_mode=True, section_plan_seed) |
| tree structure | None (flat list) |
| marker rewrite | fallback sibling counter (tree 없음) |
| **preserve_indices** | **{slot + attachment indices}** |
| **table policy** | `is_tbl_box and preserve_indices` → table text replacement skip |
| **CC7 template flow 보존** | section_plan_seed가 4 headings 전달 → AI가 template 구조를 따름 |

### 핵심 차이의 근본 원인

chapter route는 **13.3 이전에 설계**되었다. target_unit_plan이 존재하기 전에 만들어진 경로이므로, target_unit_plan의 region 정보 (slot/attachment/chapter)를 활용하지 않는다. `removed_indices`는 1a의 body paragraph 전체를 포함하며, preserve_indices가 없으므로 attachment도 삭제 대상이다.

shallow route는 **13.3에서 새로 만들어졌다.** target_unit_plan이 이미 있는 상태에서 설계되었으므로, `compute_preserve_indices`로 slot/attachment를 보존한다.

---

## 6. Debug/Log Completeness

| 실패 원인 | 분리 가능? | 확인 방법 | 부족한 debug |
|----------|----------|----------|-------------|
| source input 문제 | O | source_blocks (16), section_fill의 section_pdf_text_len | - |
| template parsing 문제 | O | 01~04 debug files, cache validation (05b) | - |
| role clustering 문제 | O | 04_canonical_clustering, repair log | - |
| target_unit_plan 문제 | O | 15_target_unit_planning, validation field | - |
| route selection 문제 | O | shallow_generation.route_reason | - |
| planning (2a) 문제 | O | 06_chapter_classify, chapters list | - |
| 2b generation 문제 | O | 08_section_fill, items, raw_items | - |
| grammar validation 문제 | O | 09_grammar_validation, violations | - |
| preserve policy 문제 | **부분** | shallow: preserve_debug 있음. **chapter: preserve 자체가 없으므로 debug도 없음** | chapter route에 preserve 관련 debug 추가 필요 |
| table policy 문제 | O | table_text_skipped counter, table_generation_policy | - |
| assemble 문제 | O | 10_assemble_result, success/fail count, errors | - |
| section handling 문제 | O | section_info (section_count, remove_per_section, append_target) | - |
| **attachment 삭제 문제** | **불충분** | section_info에 remove_per_section이 있지만, "이 삭제가 attachment인지 body인지" 구분 없음 | **추가 제안: remove_per_section에 region_type(body/attachment) 구분 필드** |
| marker rewrite 문제 | O | marker_rewrite_log (4-way), rewrite_alignment | - |
| seed compliance 문제 | O | shallow_section_plan_compliance (count, thin, repetition) | - |

### 추가하면 좋은 debug field (구현하지 않음)

1. **chapter route preserve 관련**: `preserve_policy: "none"` + 이유 (`"chapter_route_no_preserve_indices"`)
2. **remove에서 region type 구분**: `removed_paragraphs_by_region_type: {body: N, attachment: N, slot: N}`
3. **source_blocks → generation 연결**: source_block_id가 어떤 chapter에 할당됐는지 (13.1 deferred)

---

## 7. Blocker / Watch / Later 재분류

### Blocker

| 항목 | 근거 | 관련 단계 |
|------|------|----------|
| **chapter route attachment 삭제** | 민원인 101p attachment가 삭제됨. 최종 HWP 불완전. | **13.5** |
| **chapter route preserve_indices 미적용** | 위와 동일 원인. compute_preserve_indices를 chapter route에도 호출해야 함. | **13.5** |

### Watch (관측은 됐지만 현재 실패 없음)

| 항목 | 근거 | 관련 단계 |
|------|------|----------|
| multi-section append (section[0] 몰림) | 민원인 body_sections=[0,1,2,4]이지만 append_target=0. section[1,2,4]가 attachment라면 정상. body가 여러 section에 걸치는 양식이 나오면 blocker. | 9.2b / 13.6 |
| source_blocks generation 미연결 | 13.0에서 debug-only로 생성. generation에 사용 안 됨. | 13.1 deferred |
| source_blocks imbalance (sb_009=52%) | allocation 단계에서 해결 예정. | 13.1 |
| shallow/chapter 중복 로직 | 두 route가 점점 유사해지지만 구현은 별도. | 14 이후 통합 검토 |
| assembly clone+text swap 병목 | tree→indentation 미반영, inline emphasis 미보존. | assembly 고도화 (별도) |
| 2a output stability | 같은 양식에서 3 chapter vs 5 chapter 편차. | 10.5 conditional |
| table cell filling deferred | shallow route table text skip, chapter route table opaque clone. | 14-table |
| style_profile 비활성화 | latency 절감으로 off. generation 품질 영향 미측정. | later |
| marker_policy_1f / chapter_types 중복 저장 | cache top-level과 structure에 양쪽 다 있음. 불일치 위험은 낮지만 정리 대상. | later |

### Completed (이번 세션)

| 항목 | 상태 |
|------|------|
| 13.3b-1 shallow section plan seed | 완료. CC7 template flow 보존 확인. |
| CC7 template flow 보존 | planned=4, generated=4, count_match=True |
| 조달청 regression | 통과. chapter route 불변, assembly fail=0 |
| idx_full_texts data path bug | 수정됨 (structure.get → _idx_full_texts) |

---

## 8. 다음 작업 추천 순서

1. **13.5 Attachment Preserve** (blocker)
   - `compute_preserve_indices`를 chapter route에서도 호출
   - target_unit_plan의 slot/attachment region → preserve 대상
   - 기존 shallow route 메커니즘 재사용 가능
   - **assemble 변경 최소**: preserve_indices 파라미터만 추가로 전달
   - remove_per_section에 region_type 구분 debug 추가

2. **Assembly 개선** (watch → 가치 높음)
   - multi-section append 정책 (body content가 올바른 section에 들어가는지)
   - 단, 현재 양식에서 observable failure 없으면 defer 가능

3. **14-table** (table cell filling)
   - 표가 비어 있으면 문서 실용성 제한
   - 13.5 이후 or 병렬 가능

4. **14 KB 연동**
   - source 입력 경로 확장
   - 파이프라인 기능적으로 차단되지 않으므로 우선순위 낮음

---

최종 수정: 2026-05-11
