# HWPX Pipeline — Full I/O Audit (2026-05-18)

## 목적

현재 구현 기준으로 전체 파이프라인을 실제 실행 순서대로 정리한 문서. 이상적 설계 X — `DB tool generate_document_hwp_local` + `hwpx_analyzer.py` + `hwp_generator.py` + 관련 utils가 **실제로** 어떤 순서로 실행되고, 각 단계가 무엇을 입력받아 무엇을 출력하며, 그 출력이 **실제로 어디서 소비되는지** 정리.

본 문서로 발견 가능한 것:
- 생성됐지만 consumer 없는 output
- 읽지만 producer 모호한 input
- 같은 정보를 여러 단계가 set/override (drift 위험)
- 코드는 살아있지만 호출 0인 dead-code / legacy / disabled

기준 commit: `f5f49d0` (2026-05-18). 변경 시점에 따라 line number는 drift 가능.

---

## 상태 태그 범례

| 태그 | 의미 |
|---|---|
| `[active]` | production 결정 경로. 출력이 다음 단계 hard logic에 영향 |
| `[debug-only]` | 출력이 `_debug_payload` / 로그 전용. policy 영향 0 |
| `[dead-code]` | 코드 존재. 호출처 0. 또는 명시적으로 `pass` 처리 |
| `[legacy]` | 정의 살아있고 다른 endpoint에서 사용. 현 HWPX 생성 path에서는 사용 X |
| `[uncertain]` | 코드 직접 확인 못 한 상태. follow-up 필요 |

---

## 데이터 통화 (downstream에서 가장 자주 읽는 컨테이너)

| 변수 | 용도 | shape (현재 cache schema v6 기준) |
|---|---|---|
| `structure` | 1a~1f 결과 + Phase E/Track C/12.0/12.2가 mutate | `{paragraphs, tables, validator_issues, exclusive_rules, format_rules, blank_rules, marker_policy_1f, chapter_types, template_grammar, role_text_types, per_type_role_semantics, target_unit_plan, template_unit_observation, _marker_rewrite_log, _rewrite_alignment, _phase2_reattach_result, _section_info, _dirty_marking, _final_id_reassignment}` |
| `section_results` | section-local 1a~1f (B2.1.1) | `{sid: {structure, chapter_types, marker_policy_1f, signals, idx_texts, idx_full_texts}}` |
| `_chapter_objects` | 13.7a chapter route 결과 (A13/A16 collect) | `list[chapter_object]` (각 ch에 `source_chapter_idx, target_region_id, section_id, first_paragraph_idx, paragraph_indices, section_local_first_idx, title_item, body_items, status, _debug.adaptation_decision`) |
| `_debug_payload` | 누적 debug + 단계 결과 dump | `dict[stage_key → block]` (`/tmp/hwpx_debug_last.json`) |
| cache 파일 | template 단위 persistence | `/tmp/hwpx_cache/<hash16>.json` namespace='full' (Part C1) |

---

# Part A — Happy Path (main pipeline 실행 순서)

## A1 — setup / cache load (`dbtool:343~520`)

### A1.1 `analyze_hwpx` [active]
- 위치: `hwpx_analyzer.py:193` (호출 `dbtool:355`)
- 호출 시점: 가장 먼저. template_file 해석 직후
- 목적: section0.xml만 zipfile에서 읽어 lighten + metadata 반환
- 입력: `template_path` (str) ← `Storage.get_file(template_file.path)`
- 출력:
  - `analysis["light_xml"]` (str) — actually consumed: yes (`dbtool:356 → light_xml → truncate_xml`)
  - `analysis["original_xml"]` (str) — actually consumed: **no** in audited path
  - `analysis["paragraph_count"]`, `analysis["table_count"]` (int) — actually consumed: yes (cache save + log)
- audit note: **section0만 처리** (`extract_section_xml`). multi-section은 A2 loop가 처리. metadata count는 section0 한정.
- confidence: high

### A1.2 `truncate_xml` [active]
- 위치: `hwpx_analyzer.py:884` (호출 `dbtool:357`)
- 목적: light XML을 ~100kB 이하로 축소 + `_idx` 재할당
- 입력: `light_xml`, `max_chars=100000`
- 출력:
  - `truncated_xml` (str) — outer 변수는 cache-hit branch debug용 외에 dead. per-section `_section_truncated_xml`(A2.0)이 실제로 사용
  - `removed_indices` (list) — actually consumed: **no** (log + `_debug_payload["xml"]` 크기만 사용)
  - `idx_map` (dict) — outer 변수는 dead. per-section `_section_idx_map`이 `compute_format_observations`에서 사용
- audit note: 🚩 outer return 3개 (`truncated_xml`/`removed_indices`/`idx_map`)가 cache-miss path에서 dead (A2.0이 re-run). cache-hit path와 debug 출력만을 위해 컴퓨트.
- confidence: high

### A1.3 PDF / content load [active]
- 위치: `dbtool:376-398`, `pdf_to_text`는 `hwpx_analyzer.py:1120`
- 입력: `content_file_id`, `content_text` (function param)
- 출력:
  - `pdf_text_content` (str, ≤50000자) — actually consumed: yes (A6 split_source, A11 _broad_source)
  - `content_text` (str) — same
  - `content_images` — **항상 None으로 강제** (`dbtool:392-393` "이미지 추출 제거 — 토큰 절약"). 이후 multimodal branch는 dead
- failure: 3개 source 전부 비어있으면 `raise ValueError("작성할 내용이 없습니다.")`
- audit note: 🚩 `pdf_to_text` 50000자 silent truncate. 13.7c는 task 1 fix로 `_broad_source[:50000]`로 명시 활용 — 두 경계가 동일.
- confidence: high

### A1.4 `compute_template_hash` → `_cache_key` [active]
- 위치: `hwpx_analyzer.py:5052` (호출 `dbtool:410`)
- 출력: `_cache_key` (16 hex chars) — actually consumed: yes (cache load/save 다수)
- failure: 예외 시 `_cache_key = template_file_id` fallback
- audit note: 🚩 `_TEMPLATE_CORE_CASES = {"34fce805c7cbccc0": {...}}` 하드코딩 (`dbtool:761-773`) — 측정용 answer key, 특정 양식만 작동, 다른 양식에는 dead. CLAUDE.md "특정 문서명 하드코딩 X" 원칙 점검 필요.
- confidence: high

### A1.5 `_step_cache_path` / `_load_step_cache` / `_save_step_cache` [dead-code]
- 위치: `dbtool:417-431` (closure 정의)
- 호출처: **0** (grep zero hits)
- 목적: 과거 step1ab namespace 실험용 per-step JSON cache helper. 현재 hybrid_mode flag + full namespace cache로 대체
- audit note: 🚩 dead. step1ab namespace 자체도 A1/A2에서 write path 없음.
- confidence: high

### A1.6 hybrid_mode / canonical_mode valves [active — but hybrid path is debug-only]
- 위치: `dbtool:433-437`
- 출력:
  - `hybrid_mode` (bool) — gates A2.3 (parent_hint_measurement)
  - `canonical_mode` (str) — passed to `merge_levels_into_structure(canonical_mode=...)`
- audit note: 🚩 MEMORY.md "HYBRID_MEASUREMENT valve: off로 변경됨" → 현재 production OFF. 이름은 "measurement"이지만 ON일 때 production path 교체(A2.3 mutation). name vs behavior 괴리.
- confidence: high

### A1.7 `extract_all_sections_xml` pre-check [active]
- 위치: `hwpx_analyzer.py:89` (호출 `dbtool:445`)
- 출력: `_actual_section_count` (int) — cache section_count 정합성 비교용
- audit note: cache miss 시 같은 함수가 `dbtool:631`에서 재호출 (다른 import alias) — 한 run에 두 번. confidence: high.

### A1.8 `load_template_cache(namespace='full')` [active]
- 위치: `hwpx_analyzer.py:5099` (호출 `dbtool:454`)
- 출력: `_cached` (dict | None) — `cache_schema_version < 6` 자동 invalidate
- audit note: hybrid_mode ON이면 cache load skip (1c isolation 실험용)
- confidence: high

### A1.9 section_count mismatch invalidation [active]
- 위치: `dbtool:460-468`
- 동작: `_cached["section_count"] != _actual_section_count` → `_cached=None, _from_cache=False` (stale cache 방지)
- confidence: high

### A1.10 `section_results` dict init [active]
- 위치: `dbtool:477`
- 채우기: cache hit(`dbtool:492`) OR A2 loop(`dbtool:1095`) OR Phase E overwrite(`1616-1618`) OR target_unit_plan sync(`1843-1888`)
- audit note: 🚩 outer `structure`는 backward-compat alias to `section_results[0]["structure"]` (`dbtool:1273-1283`). multi-section data는 `section_results`에 살아있지만 downstream은 section 0만 사용.
- confidence: high

### A1.11 cache-hit shortcut [active]
- 위치: `dbtool:481-502`
- 동작: `_cached`에서 structure/chapter_types/signals/idx_texts/idx_full_texts/marker_policy_1f/section_results/phase_e_chapter_planner/chapter_pattern_family 모두 로드. 1a~1f + Phase E AI 호출 전부 skip.
- placeholder 변수 set (debug payload 통일성): `messages_1=[], llm_content_1="[FROM CACHE]"`, 등
- audit note: `_section_cache_validations`는 cache-hit branch에서 정의 안 됨 → `dbtool:1315`가 `dir()`로 방어
- confidence: high

---

## A2 — per-section 1a~1f loop (`dbtool:471~1334`)

cache miss branch. 모든 section iterate하지만 `section_results[0]`만 downstream에서 사용 (Phase E policy `dbtool:1350-1357`).

### A2.0 section enumeration setup [active]
- 위치: `dbtool:627-655`
- 출력: `_all_sections`, `sections_to_analyze`, per-iter `_section_light_xml`/`_section_truncated_xml`/`_section_removed_indices`/`_section_idx_map`
- audit note: per-section light XML이 A2.7(`compute_format_observations`) 입력. outer A1.1 light_xml(section0 only)은 A2에서 unused.

### A2.1 `_do_step1a_1b(_section_truncated_xml)` closure [active]
- 위치: closure 정의 `dbtool:516-614`, 호출 `dbtool:658`

#### A2.1a 1a — `build_structure_analysis_prompt` + `parse_structure_from_llm`
- AI tag: `hwpx_structure_analysis`
- 위치: `hwpx_analyzer.py:1967` (build) / `:4995` (parse)
- 출력:
  - `structure_l` (dict) — consumed yes, downstream 전체
  - `llm_content_1` — consumed yes (`_debug_payload["llm_raw_response"]`)
  - `_paragraph_styles` — consumed yes (코드가 paraPrIDRef/charPrIDRef setdefault, `dbtool:531-537`)
- mutation: AI 출력에 없는 paraPrIDRef/charPrIDRef를 compact 직렬화기 부산물에서 복원

#### A2.1b post-1a 코드 작업
- 위치: `dbtool:531-592`
- 출력 (closure return dict 키):
  - `paragraphs_before` — actually consumed: debug only (`structure_before_split`)
  - `split_log` — debug only
  - `marker_norm` — actually consumed: 메인 로직에서는 unknown (debug payload만). 🚩 confidence: medium
  - `idx_texts` (≤80자), `idx_full_texts` (unlimited) — consumed yes (section_results, signals, marker_policy 등 다수)
  - `signals_pre` — consumed yes (1c prompt)
- mutation: `structure_l["paragraphs"] = compute_paragraph_features(...)` (marker_family/prev_marker 등 enrichment)

#### A2.1c 1b — `build_role_classification_prompt` + `parse_role_classification_from_llm`
- AI tag: `hwpx_1b_role_candidates`
- 위치: `:4044` / `:4116` / merge `:4186`
- 출력: `role_candidates_l` (dict {idx: [{role, score, reason}]}) — consumed yes (debug + 1e prompt)
- audit note: 🚩 analyzer 함수 docstring은 "1c (AI 1)"라 표기, DB tool은 "1b" 표기 — 두 시스템 numbering 불일치 (역사적). 안정적이지만 혼란스러움.

### A2.2 1c — `build_level_analysis_prompt` + `parse_level_from_llm` + `merge_levels_into_structure` [active]
- AI tag: `hwpx_1c_level` (or `_hybrid`)
- 위치: `:2024` / `:2107` / `:2230`
- 출력 `level_parsed`:
  - `decisions` (dict {idx: {level, parent_idx, sibling_group_id, selected_index, ...}}) — consumed yes
  - `level_map` — consumed yes
- mutation: paragraph에 level/semantic_role/canonical_role/structure_role/role(교체)/parent_idx/sibling_group_id 부여. `_validate_selected_index` 실패 시 index 0 fallback (selection_fallback_reason 기록)
- audit note: 🚩 AI가 `parent_idx` 줘도 코드가 무시 (`hwpx_analyzer.py:2281` 주석 "1c가 줘도 무시"). `compute_parent_and_sibling_from_levels`가 level 기반으로 재계산.
- confidence: high

### A2.3 hybrid_mode block [debug-only when valve=off, mutation when on]
- 위치: `dbtool:729-835`
- 입력: `level_parsed["decisions"]` (parent_hint_idx는 hybrid only), `_TEMPLATE_CORE_CASES[_cache_key]`
- 출력 (debug payload `parent_hint_measurement`):
  - `_hint_tree_paras`, `_tree_diff`, `_pc_hint`, `_excl_hint`, `_chapter_types_hint`, `_stack_inconsistency`, `_pf_inconsistency`, `_pc_stack_by_pidx`, `_excl_stack_by_pidx`, `_core_cases` — 전부 debug only when valve=off
- mutation when ON: 🚨 `dbtool:845-847` `structure["paragraphs"] = _hint_tree_paras` → 1d/chapter_types/2a/2b/assemble 전부 hint_tree 사용
- audit note: 🚩 valve 이름 "HYBRID_MEASUREMENT"가 실제 production override 동작 의미 못 함. 현재 OFF.

### A2.4 `canonicalize_by_data` baseline [debug-only]
- 위치: `dbtool:857-869`
- 출력: `_role_registry_baseline` — `_debug_payload["1e_canonical_clustering"]["role_registry_baseline_code"]` 만. main path 영향 X (주석 명시)

### A2.5 1e — AI structural canonicalization [active]
- AI tag: `hwpx_canonical_clustering` (+ `_repair` retry)
- 위치: `dbtool:871-986`
- 흐름: AI call → parse → issues 있으면 repair AI call → 둘 다 실패 시 `canonicalize_by_data` fallback
- 출력:
  - `_role_registry` — actually consumed: **debug payload 전용**. `apply_structural_clustering`이 이미 `paragraphs[i].role`을 cluster_id로 mutate했기 때문에 `_role_registry` 자체는 main logic에서 안 읽음
  - `_1e_final_source`: "1e_original" / "1e_repaired" / "fallback_baseline"
- mutation: `apply_structural_clustering`이 `structure["paragraphs"][i].role`을 cluster_id로 교체
- audit note: 🚩 **"1e" 레이블 두 번 사용**. 여기는 AI canonical_clustering, A2.7은 code format_rules. 실행 순서: 1a → 1b → 1c → parent-correction → **1e AI canonicalization** → 1d code → **1e code format** → 1f marker policy. 번호와 실행 순서 불일치.
- confidence: high

### A2.6 1d — `compute_exclusivity_rules_code` [active, code only]
- 위치: `hwpx_analyzer.py:7737` (호출 `dbtool:999-1018`)
- 입력: `_pc_data` (`compute_parent_instance_children_by_parent_idx` if hybrid_mode else `compute_parent_instance_children`)
- 출력: `exclusive_rules` — `structure["exclusive_rules"]` set
- audit note: AI 대안 (`build_exclusivity_analysis_prompt`)이 import만 있고 호출 0 (Part B 참조)

### A2.7 1e (코드) — `compute_format_rules_code` [active, code only]
- 위치: `hwpx_analyzer.py:7827` (호출 `dbtool:1024-1041`)
- 입력: `compute_format_observations(structure, _section_light_xml, idx_map=_section_idx_map)`
- 출력: `format_rules`, `blank_rules` → `structure["format_rules"]`, `structure["blank_rules"]`
- audit note: 🚩 "AI 호출 폐기. 결정적·고속·무토큰." 주석. AI 대안 (`build_format_analysis_prompt`)이 import만 있고 호출 0 (Part B 참조)

### A2.8 1f — marker policy induction [active, AI]
- AI tag: `hwpx_1f_marker_policy`
- 위치: `:3731` (build) / `:3776` (parse) / `:3799` (verify). 호출 `dbtool:1042-1064`
- 입력: `paragraphs`, `_idx_texts`
- 출력: `_marker_policy_1f` (dict {roles: [{role, marker_policy_status, evidence, verification}]})
  - actually consumed: `structure["marker_policy_1f"]`, `section_results[sid]["marker_policy_1f"]`, cache, `extract_marker_policies` (12.1 path)
  - 🚩 `_msgs_1f`, `_llm_1f` raw는 `_debug_payload`에 없음 (Agent 1 unresolved). confidence: medium
- audit note: ✅ 1f IS AI confirmed. `extract_marker_policies` (`:7490`)는 별도 함수로 12.1 marker roundtrip path에서만 사용.

### A2.9 `validate_structure_for_cache` + `write_cache_validation_debug` [active gate]
- 위치: `:5142` (validate) / `:5332` (write). 호출 `dbtool:1070-1078`
- 출력 `_cache_validation`:
  - `can_cache`, `should_abort`, `blocker_count`, `watch_count`, `checks: [...]`
  - section0 only: `should_abort=True` → raise. section 1~4 결과는 debug only (`05d_section_cache_validations.json`)
- audit note: 🚩 multi-section에서 section1~4 invalid → silent. cache gate가 section0 기준

### A2.10 `build_chapter_types_from_structure` [active, code only]
- 위치: `:5476` (호출 `dbtool:1066-1067`, validate 직전)
- 출력: `structure["chapter_types"]`, `template_grammar`, `role_text_types`, `per_type_role_semantics`
- audit note: chapter_title_level auto-decision (`:6386-6389`)

### A2.11 `section_results[sid]` population [active]
- 위치: `dbtool:1095-1102`
- shape per `section_results[sid]`: structure / chapter_types / marker_policy_1f / signals / idx_texts / idx_full_texts
- 🚩 cache schema 3계층 중복: `structure["chapter_types"]` ↔ `section_results[sid]["chapter_types"]` ↔ top-level `chapter_types` 동시 존재

### A2.12 incremental cache save [active]
- 위치: `dbtool:1104-1124`
- 동작: `_section0_can_cache=True && 0 in section_results` 이면 누적 dict 저장. section_results dict는 mutable이라 매 iteration 새 section 포함

### A2.13 `_debug_payload` assembly (section 0 only) [active]
- 위치: `dbtool:1129-1244`
- top-level keys: `model, from_cache, cache_path, cache_key, cache_validation, marker_policy_1f, llm_raw_response, structure_before_split, structure_after_split, split_log, marker_normalization, signals, xml, 1b_role_candidates, 1c_structure_global, level_analysis, parent_correction, 1e_canonical_clustering, parent_hint_measurement, exclusivity_analysis, format_analysis`

### A2.14 loop-end outer 변수 복원 [active]
- 위치: `dbtool:1273-1283`
- 동작: `structure/chapter_types/_signals/_idx_texts/_idx_full_texts/_marker_policy_1f`를 `section_results[0]`에서 복원 (multi-section iteration 후 outer가 마지막 section에 가있는 것 fix)
- audit note: 🚩 명시적 backward-compat hack — outer 변수가 사실상 section 0 alias

### A2.15 `05d_section_cache_validations.json` dump [debug-only]
- 위치: `dbtool:1250-1271`

### A2.16 `section_results_debug` attach [debug-only]
- 위치: `dbtool:1311-1332`

---

## A3 — Phase E (TOC chapter planner) (`dbtool:1335-1488`)

### A3.1 `diagnose_1c_non_body_handling` [active]
- 위치: `:16265` (Track D-2)
- 동작: 1c가 non-body paragraph를 어떻게 다뤘는지 측정. CONTAINER(자식 OK) / LEAF(자식 wrong) 분리.
- 출력: `_pe_one_c_diag` → `_debug_payload["phase_e_chapter_planner"]["one_c_diagnostic"]`

### A3.2 multi-section guard [active]
- 위치: `dbtool:1353-1359`
- 2026-05-17 정책: multi-section template은 Phase E에서 section 0만 분석
- 출력: `_section_results_for_phase_e` (narrowed dict)

### A3.3 `has_toc_gate` [active]
- 위치: `:15699`
- 출력: `_pe_gate` (`has_toc, toc_paragraph_hints, detection_method, scanned_*`)
- 동작: role match (`table_of_contents`/`toc`) OR text regex (`차례`/`목차`/`Contents` 등 12개)

### A3.4 `build_toc_based_chapter_plan_prompt` [active]
- AI tag: `hwpx_phase_e_toc_based_chapter_plan`
- 위치: `:15963` (system at `:15790` constant)
- 입력: `_pe_toc` + `_pe_body` + `_pe_tree`

### A3.5 `parse_toc_based_chapter_plan_from_llm` + retry [active]
- 위치: `:16054`. max 1 retry. 실패 시 `status="ai_call_failed"`

### A3.6 `validate_toc_based_chapter_plan` [active]
- 위치: `:16078`. paragraph_ref 존재 체크 + 불일치 시 confidence low 강등

### A3.7 Phase E cache hit branch [active]
- 위치: `dbtool:1361-1371`
- 동작: `_cached_phase_e` 있으면 AI skip. one_c_diagnostic은 매번 fresh recompute
- 출력: `_phase_e_skipped_by_cache=True` flag (A5.1에서 사용)

### A3.8 `run_phase_e_chapter_planner` [dead-code]
- 위치: `:16441`
- 🚩 정의는 있지만 DB tool에서 import 0. orchestration이 inline (has_toc_gate + build + parse + validate). 호출처 grep 결과 zero.

---

## A4 — Track C (chapter pattern family) (`dbtool:1491-1575`)

### A4.1 cache hit + gate [active]
- gate: `_tc_pe_status == "ok"` (Phase E 성공 시만 호출)
- cache hit: `_cached_track_c` replay (schema v6+)

### A4.2 `extract_generation_unit_subtrees` [active]
- 위치: `:16624`
- 동작: unit별 subtree paragraphs (depth, role_distribution, marker_set, direct_children_count). top 50 truncate. 유사도 계산/family 판단 X — pure fact extraction
- 출력: `_tc_subtrees`

### A4.3 `build_chapter_pattern_family_prompt` + AI + parse [active]
- AI tag: `hwpx_track_c_chapter_pattern_family` (+ retry)
- 위치: `:16819` (build) / `:16833` (parse)
- 출력 `_tc_plan`: `pattern_families`, `non_grouped_units`, `ambiguity_flags`

### A4.4 `validate_chapter_pattern_family` [active]
- 위치: `:17087`
- 동작: invalid member idx 제거. `confidence in {"medium","low"}` → `expandable=false` 강제

### A4.5 Track C 소비처 [active, MEMORY와 불일치]
- 🚩 **MEMORY는 "debug-only"라 표기**. 실제로는 `_phase_e_to_chapter_types` (`:17021-17031`)가 `pattern_families` 읽어 `family_map` 구성 → chapter_types topology (`merged_chapter_count`, `_phase_e_family_id`) 결정.
- 결론: Track C는 **production 영향 있음**. debug-only는 부정확.
- confidence: medium (downstream 분기 검증 필요)

---

## A5 — Phase E + Track C cache 통합 + chapter_types PRODUCTION 전환 (`dbtool:1578-1640`)

### A5.1 cache write-back [active]
- 트리거: `not _phase_e_skipped_by_cache`
- 동작: `_pe_for_cache`, `_tc_for_cache` (`loaded_from_cache` 키 strip) → `save_template_cache`
- cache 신규 top-level key: `phase_e_chapter_planner`, `chapter_pattern_family`

### A5.2 `_phase_e_to_chapter_types` PRODUCTION overwrite [active]
- 위치: `:16988` (호출 `dbtool:1609`)
- 트리거: `_pe_final.get("status") == "ok"`
- 동작 (in-memory only — cache 안 update):
  - `chapter_types = _new_chapter_types` (outer rebind)
  - `structure["chapter_types"] = _new_chapter_types`
  - `section_results[0]["chapter_types"] = _new_chapter_types`
  - `section_results[0]["structure"]["chapter_types"] = _new_chapter_types`
- 출력: per-type 메타 `merged_chapter_count, _phase_e_source: True, _phase_e_family_id, _phase_e_member_unit_indices`
- audit note: 🚩 4중 mutation. cache는 legacy chapter_types 유지 → 다음 run cache hit 후 다시 overwrite. dispatcher seam.

---

## A6 — 2a chapter_classify (`dbtool:1690-1753`)

### A6.1 `extract_header_roles` [active, MEMORY와 불일치]
- 위치: `:9375` (analyzer 함수, MEMORY 기준 import + 호출)
- 🚩 dump 실측: `dbtool:1697-1717`에서 **inline 재구현** (`list[str]` 반환). analyzer 함수(`list[dict]` 반환)는 import 안 됨. prompt builder는 양쪽 shape 다 수용.
- 결론: MEMORY 정보 stale. 실제 inline.

### A6.2 `build_chapter_classify_prompt` [active]
- AI tag: `hwpx_chapter_classify`
- 위치: `:9421` (system at `:8259`)
- 입력: `chapter_types` (Phase E 결과), `header_roles`, content_text, content_images=None, pdf_text_content, template_grammar, paragraphs

### A6.3 `parse_chapter_classify_from_llm` [active]
- 위치: `:9550`
- 출력 `classify_result`:
  - `chapters` (list) — consumed yes (A7 split, A10 shallow, A13 2a-driven loop)
  - `header` (dict) → `header_data` — consumed yes (A17 content_data["header"])
- `_debug_payload["chapter_classify"]`: `prompt_messages, llm_raw_response, header_roles, chapters, header_data`

---

## A7 — split + source_blocks + role catalog (`dbtool:1755-1805`)

### A7.1 `split_source_by_chapters` [active]
- 위치: `:1152`
- 동작: 3-stage matching (exact → fuzzy regex → core-keyword)
- 출력:
  - `source_sections` (list[str]) — consumed yes (A13 2a-driven)
  - `_source_split_log` (decision dict) — `_debug_payload["source_split_decision"]`

### A7.2 `text_blob_to_source_blocks` [debug-only]
- 위치: `source_block_adapter.py:24` (호출 `dbtool:1770`)
- 출력: `_source_blocks` → `_debug_payload["source_blocks"]`
- audit note: 🚩 downstream consumer 없음 (Agent 2 confirmed). 13.7c 도입 후 dead 후보.

### A7.3 `_extract_texts_by_idx` [active]
- 위치: `:1797`
- 출력 `idx_texts` (≤80자) → A7.4 full_role_catalog 입력

### A7.4 `full_role_catalog` 구성 [active]
- 위치: `dbtool:1785-1795`
- 출력: `full_role_catalog` (role → {description, marker, level, sample}) — consumed (A10 shallow, A13 chapter prompt)

### A7.5 `_collect_roles` inner helper [dead-code]
- 위치: `dbtool:1797-1804`. 정의만, 호출 0.

---

## A8 — 13.7e early `target_unit_planning` (`dbtool:1807-1858`)

### A8.1 gate [active]
- 트리거: `structure["target_unit_plan"]` 없거나 `regions` empty
- 위치 이동 사유: 이전엔 chapter route fallback에서만 호출 → shallow route 결정 시점에 빈 상태 → `should_use_shallow_route` always False bug. 이 위치로 옮겨 fix.

### A8.2 `propose_template_regions` [active]
- 위치: `target_unit_planner.py:28`
- 입력: `structure, _cached, _tuo_obs_e`(=`template_unit_observation.unit_observations`)
- 출력: `_proposal_e`

### A8.3 build + parse + validate + payload [active]
- AI tag: `hwpx_target_unit_planning` (+ retry)
- 위치: `target_unit_planner.py:337/376/427/594`
- 출력: `_plan_e` (regions + validation + planner_version)

### A8.4 cache write-back [active]
- 위치: `dbtool:1839-1854`
- 동작: cache load → `structure["target_unit_plan"]=_plan_e`, `section_results[0]["structure"]["target_unit_plan"]=_plan_e` sync → save
- audit note: 🚩 이 path가 `structure["target_unit_plan"]`을 **cache에 저장**하는 유일한 경로. A19 12.2 block은 `is_plan_cache_valid` 통과 시 no-op이라 보통 duplicate aborbed.

### A8.5 위치 정정 사유 [active]
- 코멘트 `dbtool:1807-1815`: A10(shallow decision) 전에 호출하도록 옮긴 fix. CC7 양식 case.

---

## A9 — Phase E → `target_unit_plan` PRODUCTION 전환 (`dbtool:1861-1921`)

### A9.1 `build_target_unit_plan_dispatcher_decision` [active]
- 위치: `:17068`
- 출력: `_tup_decision = {"route": "phase_e" | "legacy_ai", "reason": ...}`
- `phase_e` 조건: Phase E `status=="ok"` AND `toc_plan` truthy

### A9.2 `_phase_e_to_target_unit_plan` [active]
- 위치: `:16857`
- 동작: generation_units → `unit_type="chapter"` regions / out_of_toc_preserve_regions → `unit_type="slot"` / multi-section span → `_multi_section_units_skipped` (13.7b deferred)
- audit note: 🚩 **`shallow_block` unit_type을 절대 emit 안 함** → A10 shallow route 진입 불가 (Phase E 성공한 TOC 양식은 무조건 chapter route)

### A9.3 in-memory override [active]
- 위치: `dbtool:1882-1903`
- 동작: `structure["target_unit_plan"]=_tup_new` (A8의 legacy 결과 덮어씀)
- cache: 🚩 update 안 함 (코멘트 "in-memory만 변경. 매번 실행 시 Phase E 호출 + 변환 + 덮어쓰기")
- _debug_payload["target_unit_plan_phase_e_production"]: diff dump

### A9.4 dual-write 흐름 audit
- A8 cache write (legacy AI) → A9 in-memory overwrite (Phase E shape)
- steady state: cache에는 항상 legacy. 매 run마다 A8 cache hit short-circuit + A9 overwrite. A8 AI call은 첫 cache miss만.
- 🚩 A19 12.2 block (`dbtool:3363+`): `is_plan_cache_valid(_tup_cached)` 체크. Phase E shape는 `planner_version` 없을 가능성 → False → A19가 legacy AI 재호출 가능 (cache write back 가능). [uncertain] — `is_plan_cache_valid` 정확한 동작 follow-up 필요.

---

## A10 — 13.3 Shallow Route decision (`dbtool:1923-2053`)

### A10.1 `should_use_shallow_route` [active]
- 위치: `:9730`
- 출력: `_shallow_route` (bool), `_route_debug` (dict)
- 결정 로직: `not has_chapter && has_shallow && (shallow_para > 50% body)`
- audit note: 🚩 A9.2에서 Phase E가 `shallow_block` 안 emit → Phase E 성공한 양식은 shallow 도달 불가. legacy AI fallback path만 도달 가능.

### A10.2 shallow region lookup [active]
- 동작: 첫 `unit_type="shallow_block"` region 선택 + chapter_type 픽 + pattern walk
- failure: 없으면 `_shallow_done` False 유지 → chapter route fallback

### A10.3 `extract_shallow_section_plan_seed` [active, 13.3b-1]
- 위치: `:14086`
- 출력: `_section_plan_seed_result` (headings + primary_heading_role + ...)

### A10.4 build_shallow_fill_prompt / parse_shallow_fill_from_llm / validate_shallow_output [dead-code]
- 🚩 import는 살아있지만 (`dbtool:224-226`) 호출 0
- 실제로는 `build_section_fill_prompt(..., shallow_mode=True)` + `process_section_fill_result(..., shallow_mode=True)` 사용

### A10.5 shallow 2b LLM call + process_section_fill_result [active]
- AI tag: `hwpx_section_fill_shallow`
- shallow_mode에서 title injection skip, flat body_items

### A10.6 `observe_section_plan_compliance` [debug-only]
- 위치: `:15572`
- 출력: `_debug_payload["shallow_section_plan_compliance"]` — 🚩 downstream consumer 없음

### A10.7 `compute_preserve_indices` + `assemble_hwpx_hybrid` [active]
- 위치: `source_block_adapter.py:99` + `hwp_generator.py:1089`
- shallow 전용 assembly call → result 반환

### A10.8 `_shallow_done` sentinel [active]
- shallow path 성공 시 `_chapter_objects=None`, `_chapter_empty_reasons=None` (chapter route 진입 방지)

---

## A11 — 13.7a-A1 chapter route prep (`dbtool:2055-2083`)

- chapter route 변수 초기화: `_chapter_objects=[]`, `_chapter_empty_reasons=[]`
- `_tup_regions/_tup_region_by_id/_tup_chapter_regions` 인덱싱
- `_chapter_plan_seed = extract_chapter_template_plan_seed(_tup, structure, _idx_full_texts)` (`:9781`) — 13.4b
- `_chapter_loop_driver`: `"template_plan"` if seed valid && confidence != low else `"2a_chapters"`
- `_broad_source = pdf_text_content or content_text or ""` (A12, A13, A14, A16 입력)

---

## A12 — 13.7c Source-to-Template Adaptation Planning (`dbtool:2084-2306`)

### A12.1 source_inventory (AI A) [active]
- AI tag: `hwpx_13_7c_source_inventory` (+ retry)
- 위치: `:11321` (build) / `:11376` (parse)
- 입력: `_broad_source`, `max_source_chars=_si_input_max=50000` (task 2 fix 2026-05-18)
- 출력: `_source_inventory` (`summary, available_topics, main_headings, evidence_samples`)
- `_debug_payload["source_inventory_diag"]` (task 3 fix): `source_length, source_inventory_input_length, source_inventory_input_ratio, source_inventory_call_status, source_inventory_raw_response_preview, source_inventory_parsed`

### A12.2 adaptation_plan (AI B) [active]
- AI tag: `hwpx_13_7c_adaptation_plan` (+ retry, + `_chunk_N` for split)
- 위치: `:11465` (build) / `:11889` (parse)
- 입력: `_source_inventory`, `_ch_inputs_for_plan`, `broad_source_preview=_broad_source[:50000]` (task 1 fix)
- 출력: `_ap_parsed`:
  - `chapter_decisions` (list[decision])
  - `overall_source_focus` (dict | None) ← top-level
  - `_validation` (dict)

### A12.3 split path [active, has bug]
- 위치: `dbtool:2168-2208`
- 트리거: `should_split_adaptation_batch(_ap_prompt_text)` (`:12185`)
- 🚨 **outstanding bug**: split path가 `_ap_parsed = {"chapter_decisions": _all_decisions, "_validation": ...}` 로만 재구성 → `overall_source_focus` 누락. `_osf = _ap_parsed.get("overall_source_focus")` (line 2254)가 None
- 해결책 후보 2026-05-18 인수인계:
  - (a) `should_split_adaptation_batch` 임계값 완화 (60000 → 128000 또는 safety_ratio 0.6→0.95)
  - (b) split path에서 chunk1 focus를 final로 저장 + ambiguity_flag

### A12.4 validation / normalize / fallback [active]
- per decision: `normalize_adaptation_decision` (`:12203`) → `validate_adaptation_decision` (`:11983`) → demote 시 `make_validation_failed_decision` (`:12329`)
- missing idx: `make_unavailable_decision` (`:12282`)

### A12.5 summarize [active]
- `summarize_adaptation_plan(...)` (`:12377`) — `action_distribution` alias 포함 (task 5 fix)
- 출력 `_adaptation_plan_summary` → `_debug_payload["adaptation_plan"]` (A19 path)
- consumed yes (A13 chapter loop lookup `_ch_decisions_by_idx[ch_idx]`, A16 reuse via `_source_inventory`)

### A12.6 outer try/except [active]
- 예외 시 모든 chapter `make_unavailable_decision(supported_as_is)` fallback

---

## A13 — 2b chapter loop (`dbtool:2293-2614`)

### A13.1 template-driven path [active]
- 조건: `not _shallow_done and _chapter_plan_seed`
- 위치: `dbtool:2293-2513`
- per-chapter flow:
  1. `ch_title = adapted_title` (모든 action에서, 13.7c-2phase)
  2. local_pattern from `tpl_ch.get("local_pattern")` else seed_pattern (13.6-B)
  3. `_title_action`, `_content_action`, `_action = _title_action` (debug alias)
  4. **모든 chapter는 2b 호출** (`source_gap` 분기는 placeholder `if False`)
  5. `build_section_fill_prompt(..., pdf_text=_broad_source, ...)` (`:14542`)
  6. **adaptation hint prepend** (13.7e v2): `_title_action in adapt_*` 면 첫 user message 앞에 hint block (adapted_title, original_title, actions, preserved/adapted_aspects[:3], supporting_evidence[:3])
  7. AI call (tag `hwpx_section_fill_{ch_idx}`)
  8. override grammar (13.6-B): `pattern_to_grammar(_ch_local_pattern)` (`:9972`)
  9. `process_section_fill_result(...)` (`:15360`) — 내부 흐름: `parse_section_fill_from_llm` (`:14867`) → `normalize_section_items` (`:14920`) → `validate_ai_parent_ids` (`:15091`) → `apply_parent_id_fallback` (`:15295`) → `reconstruct_tree_from_flat` + `validate_reconstruction`
  10. `_ch_status`: `"filled"` if items>1, else `"insufficient_source"`. `_action=="preserve"` → `"preserved_by_13_7c"`
  11. `_ch_region = _tup_region_by_id.get(tpl_ch.get("region_id"))`
  12. `diagnose_chapter_empty_reason(_sf_result)` (`:11007`)
  13. `compute_reference_metrics(_decision, ...)` (`:12120`) — debug-only
  14. `build_chapter_object(...)` (`:11090`) → `_ch_obj` (section_id default 0)
  15. preserve → `_ch_obj["status"]="empty"`
  16. append `_chapter_objects, _chapter_empty_reasons, _per_ch_status, _section_fill_debug`

### A13.2 2a-driven path [active]
- 조건: `not _shallow_done and not _chapter_plan_seed`
- 위치: `dbtool:2514-2614`
- 차이점: `_chapter_plan_seed["chapters"]` 대신 `chapters` (A6 결과), `_broad_source` 대신 `source_sections[ch_idx]`, **adaptation hint 없음** (no `_decision`), region attach via `_tup_chapter_regions[ch_idx]` positional fallback

### A13.3 outputs / debug
- `_chapter_objects` (list, A11 owned, mutated) — consumed A16 (extend), A17 (content_data["chapters"]), A18 (assembly chapter_anchors matching)
- `_section_fill_debug` (list) — `_debug_payload["section_fill"]` (A17)
- `_chapter_trees` (list) — 🚩 **dead-write**. `assemble_hwpx_hybrid`이 chapter_trees= kwarg drop (13.7a-A1). append만 하고 read 없음
- `_per_ch_status` (list) — A14 + `_debug_payload["chapter_template_plan"]`
- `_chapter_plan_debug` (dict) — `_debug_payload["chapter_template_plan"]` (line 2512)

### A13.4 audit
- 🚩 `_2b_title = ch_title` 이미 adapted_title 상태 + adaptation hint prepend → adapted_title 두 번 prompt에 나옴 (`## 대제목` block + hint preamble)
- per-chapter try/except → 실패 시 fail chapter_object append

---

## A14 — 13.6-C source diagnostic (`dbtool:2490-2511`) [debug-only]

- template-driven path에서만 작동 (2a-driven은 skip)
- 입력: `_broad_source`, `_per_ch_status`, `source_sections`, `_seed_chapters`
- 출력: `_chapter_plan_debug["source_diagnostic"]` — anomaly threshold 하드코딩 (`source>10000 && items==0` / `source<1000 && items>20`)
- 🚩 downstream consumer 없음 (debug only)

---

## A15 — 13.7a-0 A0 parallel measurement (`dbtool:2615-2631`) [debug-only]

- 무조건 try/except로 호출
- `measure_title_role_consistency(structure, {"seed": _chapter_plan_seed})` (`:10880`)
- 출력: `_debug_payload["title_role_consistency"]` (`chapter_types_title_roles_set, local_title_roles_set, mismatch_summary, per_chapter, status`)
- 🚩 "1d-fix stage 우선순위 판단 자료" — policy 영향 X

---

## A16 — 13.7b section-local generation-lite (`dbtool:2834-3232`) [active for section N != 0]

조건: `not _shallow_done and section_results and _chapter_objects is not None`

### A16.1 XML paragraph 텍스트 추출
- `extract_all_sections_xml(template_path)` + `extract_section_xml_paragraph_texts` (`:13663`)
- 출력: `_section_xml_paragraph_texts: dict[sid → list[text]]`

### A16.2 section offset 계산
- `compute_section_offsets(section_results, _section_xml_paragraph_counts)` (`:13428`)
- census count 우선, 없으면 1a paragraph count fallback

### A16.3 1a → xml idx mapping
- `_build_1a_to_xml_p_idx_mapping(idx_texts, _xml_texts)` (`:13469`) — text-normalized substring matching. 13.7b fix

### A16.4 section chapter list
- `extract_section_chapter_list(_sid, _sr, _offset, ai_to_xml_idx_mapping=_1a_to_xml_map)` (`:13696`)
- 출력: `{section_id, section_offset, paragraph_count, title_roles_used, chapters: [...], confidence}`

### A16.5 section decision
- `decide_section_processing(_sid, _b22_p, _scl)` (`:13907`)
- deadline policy: `confidence != low && reference_label in ("top_level", "body", "other")` → generate, else preserve
- section 0은 `{"action": "existing_chapter_route"}` 강제 (여기서 generate skip)

### A16.6 per-section adaptation_plan
- AI tag: `hwpx_13_7b_section_n_adaptation_plan_sec{sid}` (+ inventory tag if needed)
- 🚩 `_section_n_si = _source_inventory` (`dbtool:2920`) — A12에서 정의된 변수를 NameError-guarded read로 재사용. A12 안 돌면 fragile
- 새 AI tag `hwpx_13_7b_section_n_source_inventory` 도 가능 (`_section_n_si` 없을 때)
- `build_adaptation_plan_prompt(..., broad_source_preview=_broad_source[:10000])` (A12와 달리 10000자)

### A16.7 per-section per-chapter 2b
- `preserve` action: **synthetic `_sf_result_n`** 생성 (body_items=[_synthetic_title_item_n]만, chapter_tree_nodes=[stub], items_count=0)
- `generate` action: `build_section_fill_prompt(..., template_chapter_context={...})` → AI tag `hwpx_section_fill_sec{sid}_ch{ci}` → `process_section_fill_result(no override_grammar)`
- `adapted_title_generate` → A13과 동일한 hint prepend

### A16.8 chapter_object 추가
- synthetic region: `{region_id: None, section_id: _sid, section_local_first_idx: <xml idx>, paragraph_indices: <doc-global>, title_role, marker}`
- `_ch_obj_n["section_id"] = _sid` 강제
- 🚩 **section_local_first_idx는 xml idx** (`_resolve_xml_idx`로) → A18 Priority 1 anchor 매칭이 `_section_top_level_paragraphs[sid][idx]`와 정렬

### A16.9 출력
- `_section_local_decisions` (dict) — `_debug_payload["section_local_decisions"]` (`dbtool:3213`)
- `_section_local_chapter_lists` (dict) — A17 `build_chapter_local_exemplars` 입력, `_debug_payload["section_local_chapter_lists"]` (`dbtool:3216`)
- `_analyzed_section_ids` (set, initial `{0}`) — A17 `assemble_hwpx_hybrid(analyzed_sections=...)`
- `_section_xml_paragraph_texts` — A17 chapter_local_exemplars

---

## A17 — Step 5 Assembly orchestration (`dbtool:3233-3361`)

### A17.1 content_data 조립
```python
if _chapter_objects is not None and not _shallow_done:
    content_data = {"header": header_data, "chapters": _chapter_objects}
else:
    content_data = {"header": header_data, "body": body_items}
```

### A17.2 region action plan (13.5)
- `compute_region_action_plan(_tup, structure, idx_map=idx_map)` (`:10295`)
- 출력: `{actions, preserve_indices, summary, warnings}`
- `_chapter_preserve = set(_region_plan["preserve_indices"])` → assembly `preserve_indices=`
- `_debug_payload["region_action_plan"]`

### A17.3 multi-section diagnostic (13.6-A)
- `diagnose_multi_section(template_path)` (`:10489`)
- 🚩 `gate_decision.section_aware_assembly_needed`는 observation only — hard gate X

### A17.4 chapter-local exemplars (13.7b §4)
- `_ai_to_xml_for_local` 구성 (section N each)
- `build_chapter_local_exemplars({sid: scl for sid in lists if sid != 0}, section_results, _ai_to_xml_for_local)` (`:13556`)
- local_ch_idx → global ch_idx remap (`_section_0_ch_count + _local_ch_idx`)
- 🚩 remap이 section 0 chapter count + section N ordering에 의존 — A11 chapter ordering 변경 시 fragile

### A17.5 assembly call [active]
```python
result = assemble_hwpx_hybrid(
    template_path, structure, content_data,
    removed_indices=removed_indices, idx_map=idx_map,
    content_only_mode=True,
    preserve_indices=_chapter_preserve,
    analyzed_sections=_analyzed_section_ids,
    chapter_local_exemplars=_chapter_local_exemplars,
)
```

### A17.6 debug capture
- `_debug_payload["section_fill"]`, `["final_content"]`, `["assembly"]` (success_count, fail_count, errors, output_size, marker_rewrite_log, rewrite_alignment, phase2_reattach_result, section_info)

---

## A18 — `hwp_generator.assemble_hwpx_hybrid` (`hwp_generator.py:1089-2847`) [active]

### A18.1 진입 진단 dump
- `/tmp/hwpx_debug/_d00_assemble_entry.json`

### A18.2 `_process_chapter_objects` (`:744`)
- `content["chapters"]` 있으면 호출. `body`+`chapters` 동시 → warning, chapters wins
- 🚩 `_chapter_proc["adapted_title_deferred"]` (line 802, 981-985)에 chapter별 entry append. 하지만 실제 adapted_title 적용은 line 1689 (chapter_anchors loop)에서 `_ch_obj._debug.adaptation_decision.adapted_title` 직접 read → `adapted_title_deferred` list 실제 consumer 없음

### A18.3 role exemplar map 구성
- 각 role의 first idx (level 0 cover/toc 제외, title_role/has children 있으면 포함)
- `_strip_document_ctrls`/`_strip_linesegarray` on deepcopies
- `blank_exemplars` by paraPrIDRef

### A18.4 header 처리
- 텍스트 교체 via `_set_element_text` (`:2892`)
- `header_indices` 추적

### A18.5 preserve set 구성
- `_is_skip` level 0 + 첫 paragraph + 9.1b secPr carrier + `preserve_indices`(region plan) + `_chapter_proc["empty_preserve_indices"]`

### A18.6 chapter_anchors 매칭 loop (`:1566-1752`) [active]
- **Priority 1**: `section_local_first_idx + section_id` → `_section_top_level_paragraphs[sid][idx]` + `_validate_anchor_signature`
- **Priority 2**: legacy `paragraph_indices[0] + idx_map` (same section만)
- **Priority 3**: text fallback `_find_anchor_in_section_by_text(title_text, sid)` (same section)
- **Priority 4**: `chapter_anchor_failures.append`, `placement_failure`
- **invariant**: anchor owning section ≠ chapter.section_id → CROSS_SECTION_BLEED hard fail (skip)
- **13.7d 2-phase adapted_title**: `_ad_text = _ch_obj._debug.adaptation_decision.adapted_title`. non-empty && ≠ anchor text → `_replace_text_in_paragraph_elem(_anchor_el, _ad_text, NS)` (`:3001`). action 무관 적용 (최근 fix, line 1673-1674 코멘트)
- per-ci diag append → `/tmp/hwpx_debug/_d02_anchor_per_ci.jsonl` (`:1722`)

### A18.7 empty preserve 재계산
- `_chapter_proc["empty_preserve_indices"]` clear → `chapter_anchors[ci]` doc_idx로 재구성 (정확도 ↑)

### A18.8 unanalyzed section preserve
- `analyzed_sections` 외 section의 모든 paragraph preserve

### A18.9 body remove loop
- header_indices 외 paragraph 전부 remove. `_remove_per_section` 추적
- 🚩 `_residual_candidates`, `_preserved_per_section` (lines 1931-1966) — debug 목적 build만, downstream read 없음

### A18.10 body item insertion loop (`:2408-2746`) [active]
- exemplar pick: role + `chapter_local_exemplars[ci]` (13.7b §4) > section N placeholder fallback > legacy fallback
- outer fallback safety: exemplar에 `tbl` 있으면 skip
- **marker rewrite** (content_only_mode=True): `strip_marker` → AI marker residual strip → `generate_expected_marker_normalized` + `reattach_marker` → `_rewrite_marker` safety net
- blank_rules + format_rules indent_parts 적용
- deepcopy + `_reassign_unique_ids(_assembly_id_counter=[3_000_000_000])` (`:1040`)
- `_set_cloned_element_text` (`:2947`)
- **region-aware placement**: `_ci in chapter_anchors` → 그 section 안 anchor 뒤에 insert. 다른 section bleed → error
- fallback append (chapter 컨텍스트 없으면) — orphan body item error

### A18.11 mutation summary
- `structure["_marker_rewrite_log"], ["_rewrite_alignment"], ["_phase2_reattach_result"], ["_dirty_marking"], ["_final_id_reassignment"]` write

### A18.12 final ID reassignment (`:1057`)
- `_reassign_all_section_ids(_all_section_elements, counter_start=4_000_000_000)` — assembly 끝에 모든 section element id unique 재할당 (최근 fix `f5f49d0`)

### A18.13 dead/legacy action handlers
- `_execute_*` 11개 (set_cell, clear_body, set_paragraph_text, add_paragraph, add_table, remove_paragraph, add_row, remove_table, insert_paragraph, clone_paragraph) — 🚩 `generate_hwpx_dynamic` (`:503`)만 사용. dynamic endpoint도 import 0 → **dead-code**
- `_sort_actions` (`:415`), `_clear_unmodified_fields` (`:456`) — 같은 path 종속, dead
- `generate_hwpx_dynamic` (`:503`) — **dead-code**
- `assemble_hwpx` (`:600`) — non-hybrid v2. `routers/files.py:1868` 다른 endpoint에서 사용 → **legacy** (HWPX 생성 path X)

### A18.14 출력
- `HwpxResult(data=bytes, success_count, fail_count, errors)`

---

## A19 — Debug finalization (`dbtool:3363-end`)

### A19.1 12.2 target_unit_planning debug [debug + cache write-back]
- `is_plan_cache_valid(_tup_cached)` (`target_unit_planner.py:584`)
- cache miss 시: `propose_template_regions` + `build_target_unit_planning_prompt` + AI + parse + validate + cache write-back
- `assemble_planning_debug(...)` (`:609`) → `_debug_payload["target_unit_planning"]`
- 🚩 **A8와 duplicate**. A8 이미 cache write → 보통 here는 no-op. 단 A9가 in-memory Phase E shape로 overwrite → `is_plan_cache_valid` False 가능성 → 여기서 legacy AI 재호출 가능 [uncertain]

### A19.2 12.1 marker roundtrip readiness [debug-only]
- `extract_marker_policies(paragraphs, marker_policy_1f=structure.get("marker_policy_1f"))` (`:7490`)
- `content_only_mode=True && structure._phase2_reattach_result` 있으면 inline summary (schema v2)
- else: `build_marker_roundtrip_debug(...)` (`marker_separator.py:380`)
- `_debug_payload["marker_roundtrip_readiness"]`

### A19.3 11.2 style profile [dead-code]
- 🚩 literal `pass`. "Style profile AI calls disabled to reduce latency". `build_style_profile_prompt`/`parse_style_profile_from_llm` import만 살아있음

### A19.4 12.0 template unit observation [debug + cache write-back]
- AI tag: `hwpx_template_unit_observation` (+ retry)
- `is_cache_valid(_tuo_cached)` (`template_observer.py:854`)
- cache miss 시: features → prompt → AI → validate → derive_label → `build_cache_payload` → cache write-back
- 🚩 MEMORY는 "debug-only"라 표기. 실제로는 `unit_observations`가 A8(`propose_template_regions`), A9(legacy comparison) 입력으로 사용 → production 영향 있음

### A19.5 최종 dump
- `/tmp/hwpx_debug_last.json` write
- `write_stage_debug_files(_debug_payload)` (`:8439`) — `/tmp/hwpx_debug/*.json` glob delete 후 재생성
  - 🚨 `_d00_..._d04_assemble_*.json`, `_d02_anchor_per_ci.jsonl` 같은 A18 diag 파일 중 .json 확장자 가진 것 **wipe됨**. `.jsonl`은 glob 매칭 X → 살아남음

### A19.6 Step 5 result message
- success_count / fail_count / output_size + debug XML preview

---

# Part B — Side / Debug-only / Legacy paths

## B1 — hint_tree experiment + canonicalize_by_data baseline [debug-only when valve OFF]

- A2.3 + A2.4 참조
- 🚩 hybrid_mode ON 시 production path 교체 (mutation): `structure["paragraphs"] = _hint_tree_paras`
- 현재 OFF — hint_tree 전체 코드 path는 production dead

## B2 — 13.7b B0a — Pre-1a Section Census [debug-only]

- 위치: `dbtool:2633-2647`
- 호출 시점: A15 직후 (A16 진입 직전)
- `extract_section_census(template_path)` (`:10754`). AI 호출 0
- 출력: `_debug_payload["section_census"]` + `_section_census` (B3 + A16 입력)

## B3 — 13.7b B2.2 — Section Role Proposal [active for A16 driver, AI 1 call + retry 1]

- 위치: `dbtool:2650-2789`
- chapter route only (`_shallow_done=False && section_results`)
- AI tag: `hwpx_13_7b_section_role_proposal` (+ retry)
- 출력: `_debug_payload["section_role_proposals"]` ← `summarize_section_role_proposals(...)`
- 🚩 MEMORY "debug-only"라 표기 + 코멘트 "production HWP 영향 X" 명시. 하지만 Agent 3 확인: A16 `decide_section_processing`이 B2.2 proposal의 `reference_label`을 driver로 사용 → **A16 production decision에 영향**. 즉 active production path
- fallback: `make_fallback_section_role_proposal(call_failed)` 각 section

## B4 — 13.7b B0b — Post-1a Merge Feasibility [debug-only]

- 위치: `dbtool:2791-2823`
- chapter route only
- `measure_merge_feasibility(section_results, _section_census)` (`:13281`)
- `build_b0b_observation_artifact(...)` (`:13361`)
- 출력: `_debug_payload["merge_feasibility"]`, `_debug_payload["b0b_observation_artifact"]`
- 🚩 "정책 결정은 B0b review에서 합의" — 사람이 보는 evidence

## B5 — 12.0 template_unit_observation [active downstream consumer]

- A19.4 참조
- 🚩 "debug-only" 표기 vs 실제 production 입력 — see audit C4

## B6 — 11.2 Style Profile [dead-code]

- A19.3 참조. literal `pass` block

## B7 — Legacy 분석용 AI prompts [legacy, separate endpoint]

- `build_role_interpret_prompt` (`:547`), `parse_role_interpret_from_llm` (`:591`)
- `build_role_content_prompt` (`:658`), `parse_role_content_from_llm` (`:730`)
- `build_content_mapping_prompt` (`:4864`) — 정의만, 호출 0 → **dead**
- 사용처: `routers/files.py:1764, 1766, 1841, 1852` — HWPX 생성 endpoint 아닌 file analysis endpoint
- 🚩 같은 파일(hwpx_analyzer.py)에 HWPX gen + 분석 endpoint 함수 섞임 → cleanup 후보

## B8 — Dead AI imports [dead-code]

- DB tool `dbtool:197-201` import:
  - `build_exclusivity_analysis_prompt` (`:4747`), `parse_exclusivity_from_llm` (`:4809`)
  - `build_format_analysis_prompt` (`:4576`), `parse_format_rules_from_llm` (`:4616`)
- 호출 0. 코드 path는 code 함수 (`compute_exclusivity_rules_code`, `compute_format_rules_code`)로 대체됨 (A2.6, A2.7)
- DB tool 코멘트 (`dbtool:1023`): "AI 호출 폐기. 결정적·고속·무토큰."
- 🚩 cleanup 후보. `parse_format_rules_from_llm` 이름이 살아있어 새 작업자 잘못 import 위험

## B9 — Source block adapter [debug-only]

- A7.2 참조. `text_blob_to_source_blocks` (`source_block_adapter.py:24`)
- downstream consumer 없음
- 13.7c 도입 후 사실상 obsolete — cleanup 후보

## B10 — `compute_preserve_indices` (source_block_adapter)

- A10.7 참조. shallow path 전용. 살아있음

## B11 — `run_phase_e_chapter_planner` [dead-code]

- A3.8 참조. 정의는 있지만 import 0

## B12 — `_collect_roles` inner helper [dead-code]

- A7.5 참조

## B13 — Shallow path 전용 함수 [dead-code]

- A10.4 참조: `build_shallow_fill_prompt`, `parse_shallow_fill_from_llm`, `validate_shallow_output` — import는 살아있지만 호출 0. `build_section_fill_prompt(shallow_mode=True)` + `process_section_fill_result(shallow_mode=True)` 사용

## B14 — `_step_cache_path` / `_load_step_cache` / `_save_step_cache` [dead-code]

- A1.5 참조

## B15 — `_chapter_trees` (A13에서 append, A17/A18에서 read X) [dead-write]

- A13.3 참조. 13.7a-A1에서 `assemble_hwpx_hybrid` kwarg drop. append만 살아있음

## B16 — `_chapter_proc["adapted_title_deferred"]` [dead-list]

- A18.2 참조. apply path가 직접 `_ch_obj._debug.adaptation_decision.adapted_title` read → list는 build만

## B17 — `_residual_candidates`, `_preserved_per_section` [dead-debug]

- A18.9 참조. `assemble_hwpx_hybrid` 내부 build만, downstream read 없음

---

# Part C — Cross-cutting / Audit

## C1 — Cache schema (namespace='full', `CACHE_SCHEMA_VERSION = 6`)

### C1.1 파일 위치 / 경로
- `TEMPLATE_CACHE_DIR = "/tmp/hwpx_cache"` (`hwpx_analyzer.py:5046`)
- namespace='full': `<DIR>/<hash16>.json` (suffix 없음)
- namespace='step1ab': `<DIR>/<hash16>_step1ab.json` — 현재 write path 없음

### C1.2 버전 호환
- `cache_schema_version < 6` → load 시 None 반환 (info log only, warning 아님)
- 자동 invalidate. 영향 없는 변경(assemble logic 등)에서도 v bump 시 강제 재실행

### C1.3 Top-level keys (실측 cache 파일 인스펙션 + 코드 두 source 일치)
```
structure                       — main blob (1a~1f + Phase E mutation + grammar + target_unit_plan + template_unit_observation)
chapter_types                   — outer alias (structure.chapter_types 중복)
signals                         — _signals (compute_role_context_signals)
idx_texts                       — _idx_texts (≤80자)
idx_full_texts                  — _idx_full_texts (unlimited)
marker_policy_1f                — outer alias (structure.marker_policy_1f 중복)
paragraph_count, table_count    — sanity check
template_file_id                — original file_id (hash fallback)
section_count                   — extract_all_sections_xml 길이
section_results                 — {sid: section_local_view}
phase_e_chapter_planner         — A5.1 write (v6+, conditional on phase E success)
chapter_pattern_family          — A5.1 write (v6+, conditional)
cache_schema_version            — int
```

### C1.4 `structure` 내부 keys
```
paragraphs, tables, validator_issues, exclusive_rules, format_rules, blank_rules,
marker_policy_1f, chapter_types, template_grammar, role_text_types,
per_type_role_semantics, target_unit_plan, template_unit_observation
```

### C1.5 `section_results[sid]` keys
```
structure, chapter_types, marker_policy_1f, signals, idx_texts, idx_full_texts
```

### C1.6 cache write 분산 (단일 cache 파일을 mutate하는 위치들)
- A2.12 incremental save (per-section loop)
- A5.1 Phase E + Track C 통합 write-back
- A8.4 target_unit_plan write-back (legacy AI)
- A19.1 target_unit_plan 재write (cache miss 시)
- A19.4 template_unit_observation write-back

🚩 같은 cache 파일을 5개 단계가 partial mutation. consumer가 어느 단계 결과 읽는지 헷갈릴 위험.

### C1.7 중복 키 (3계층 drift 위험)
- `chapter_types`: top-level / `structure["chapter_types"]` / `section_results[sid]["chapter_types"]` / `section_results[sid]["structure"]["chapter_types"]` (Phase E sync) → **4중**
- `marker_policy_1f`: top-level / `structure["marker_policy_1f"]` / `section_results[sid]["marker_policy_1f"]`
- A1.11 cache-hit branch: top-level 우선 read. miss branch: structure가 source. 분기 일관성 ✓ 검증 필요

## C2 — Debug 파일 위치 + write_stage_debug_files 매핑

### C2.1 통합 dump
- `/tmp/hwpx_debug_last.json` — A19.5에서 final write. 단계 진행 중에도 `_debug_add` 호출 시 누적

### C2.2 분리 dump — `write_stage_debug_files` (`hwpx_analyzer.py:8439`)
- A19.5에서 단 한 번 호출
- 동작: `glob("*.json")` → 전부 delete → payload 분리 write
- 🚨 같은 디렉토리에 외부에서 쓰는 `.json` (예: A2.15 `05d_section_cache_validations`, A18 `_d00..._d04`)은 **wipe됨**. 단 `.jsonl` (`_d02_anchor_per_ci.jsonl`)은 glob 매칭 X → 살아남음

### C2.3 파일 매핑 (실제 producer 위치)
```
01_template_paragraph_analysis.json   ← write_stage_debug_files:01 (1a/1b 통합)
02_level_parent_tree.json             ← :02 (1c + parent correction)
03_role_clustering.json               ← :03 (1e canonical + per_type_role_semantics)
04_chapter_types.json                 ← :04
05_template_grammar.json              ← :05
05b_cache_validation.json             ← :05b (재write — A2.9도 별도로 write_cache_validation_debug에서 쓰지만 :05b가 덮어씀)
05c_marker_policy_induction.json      ← :05c
05d_section_cache_validations.json    ← A2.15에서 직접 write (write_stage_debug_files 외부)
                                         🚨 A19.5 write_stage_debug_files 호출되면 wipe — 그 후 안 재write
06_type_catalog.json                  ← :06 (_build_rich_type_catalog)
07_2a_type_selection_result.json      ← :07 (chapter_classify)
07b_source_split_decision.json        ← :07b
08_2b_generation_by_chapter.json      ← :08 (section_fill)
09_grammar_validation_result.json     ← hwpx_analyzer.py:8878 (write_stage_debug_files 안)
10_assemble_result.json               ← :10
11_validation_summary.json            ← hwpx_analyzer.py:8970 (write_stage_debug_files 안)
13_template_unit_observation.json     ← :13 (12.0)
13_7b_b0b_observation.json            ← :13_7b (b0b artifact)
14_marker_roundtrip_readiness.json    ← :14 (12.1)
15_target_unit_planning.json          ← :15 (12.2)
16_source_blocks.json                 ← :16 (13.0 debug)
17_section_role_proposals.json        ← :17 (B2.2)
18_merge_feasibility.json             ← :18 (B0b raw)
19_section_local_decisions.json       ← :19 (A16)
20_section_local_chapter_lists.json   ← :20 (A16)
99_debug_summary.json                 ← :99
_d00_assemble_entry.json              ← hwp_generator.py:A18.1 — 🚨 A19.5 wipe
_d01~_d04_assemble_*.json             ← hwp_generator.py 다른 diag — 🚨 wipe
_d02_anchor_per_ci.jsonl              ← hwp_generator.py:1722 — .jsonl이라 살아남음
17_assembly_anchor_debug.json         ← A18 끝부분 — 🚨 wipe (실측 ls 결과에 없음 — 이미 wipe된 흔적)
```

### C2.4 missing numbers
- 🚩 `12_`가 비어있음. 과거 `12_template_unit_observation`이 `13_`으로 이동된 후 정리 안 됨 — numbering drift

## C3 — Validation gates 모음

| ID | 함수 | 위치 | 실패 동작 |
|---|---|---|---|
| 1a-V | `_validate_selected_index` | `:3842` | invalid index 시 split |
| 1a-V | `_validate_and_split` | `:3876` | 1a 결과 paragraph 분할 |
| 1f-V | `verify_marker_policy_evidence` | `:3799` | role.verification = consistent/inconsistent (drop X, 평가만) |
| Phase E-V | `validate_toc_based_chapter_plan` | `:16078` | invalid ref → confidence low 강등 |
| Track C-V | `validate_chapter_pattern_family` | `:17087` | medium/low confidence → expandable=false 강제 |
| 12.0-V | `validate_unit_observation` | `template_observer.py:627` | blockers → unit_observations 비우고 label undetermined |
| 12.2-V | `validate_target_unit_plan` | `target_unit_planner.py:427` | invalid → empty plan |
| 13.3-V | `validate_shallow_output` | `:9709` | (Note: B13 import만, 호출 0 — dead) |
| 13.7c-V | `validate_adaptation_decision` | `:11983` | `should_demote` → `make_validation_failed_decision` |
| 13.7c-N | `normalize_adaptation_decision` | `:12203` | alias / enum 정규화 |
| 13.7a-V | `assert_chapter_object_invariants` | `:11205` | (실패 동작 검증 필요 — raise vs log [uncertain]) |
| 2b-V | `validate_ai_parent_ids` | `:15091` | invalid → `apply_parent_id_fallback` |
| B2.2-V | `validate_section_role_proposal` | `:12798` | invalid → `make_fallback_section_role_proposal` |
| cache-V | `validate_structure_for_cache` | `:5142` | section0만 `should_abort=True` → raise ValueError |
| reconstruction-V | `validate_reconstruction` | `:7083` | tree 재구성 검증 |
| text-V | `validate_text_quality` | `:7128` | — |
| summary | `build_validation_summary` | `:7331` | `11_validation_summary.json` 생성 |

🚩 section0만 cache abort gate. multi-section에서 section 1~4는 invalid silent (debug only).

## C4 — 🚩 Audit notes (통합)

### C4.1 status tag와 실제 동작 불일치

1. **Track C "debug-only" 표기 vs production 영향 (A4.5)** — MEMORY/주석 "debug-only"이지만 `_phase_e_to_chapter_types`가 `pattern_families`로 type 병합 → chapter_types topology에 영향
2. **B2.2 "production HWP 영향 X" 코멘트 vs A16 driver (B3)** — `decide_section_processing`이 B2.2 `reference_label`로 generate/preserve 결정 → 실제 production 분기 결정자
3. **12.0 template_unit_observation "debug-only" 표기 vs A8/A9 input (B5)** — `unit_observations`가 A8 `propose_template_regions`, A9 legacy_comparison 입력
4. **`hybrid_mode` valve 이름 "MEASUREMENT" vs 실제 production override (A2.3)** — ON 시 `structure["paragraphs"]` 교체

### C4.2 Dead code / dead imports / disabled

- `_step_cache_path` 등 (A1.5)
- `run_phase_e_chapter_planner` (A3.8)
- `_collect_roles` (A7.5)
- `build_shallow_fill_prompt`/`parse_shallow_fill_from_llm`/`validate_shallow_output` import만 (A10.4)
- `generate_hwpx_dynamic` (A18.13) + 11 `_execute_*` action handlers
- `_sort_actions`, `_clear_unmodified_fields` (A18.13)
- `build_role_interpret_prompt`, `build_role_content_prompt` (B7) — analyzer 파일 안에 있지만 file analysis endpoint 전용
- `build_content_mapping_prompt` (B7) — 정의만 호출 0
- `build_exclusivity_analysis_prompt`/`parse_exclusivity_from_llm` (B8)
- `build_format_analysis_prompt`/`parse_format_rules_from_llm` (B8)
- `build_style_profile_prompt`/`parse_style_profile_from_llm` (B6 + A19.3)
- `_chapter_trees` append (B15) — 13.7a-A1 후 dead-write
- `_chapter_proc["adapted_title_deferred"]` list (B16) — 실제 apply는 다른 path
- `_residual_candidates`, `_preserved_per_section` (B17)
- `_TEMPLATE_CORE_CASES` (A1.4) — 특정 양식 hash 하드코딩, 다른 양식에 dead

### C4.3 Duplicate / drift 위험

5. **target_unit_plan 5번 mutation** (A1.10/A8.4/A9.3/A19.1, + cache sync)
6. **chapter_types 4중 위치** (top-level cache / structure / section_results[sid] / section_results[sid].structure) — Phase E overwrite도 4번 mutation (A5.2)
7. **marker_policy_1f 3중 위치** (top-level / structure / section_results)
8. **12.2 target_unit_planning A19 block A8과 duplicate** — `is_plan_cache_valid` False 시 legacy AI 재호출 가능 [uncertain]
9. **"1e" 레이블 두 번** (A2.5 AI canonicalization + A2.7 code format) — 실행 순서와 label 불일치
10. **outer scope vars** (`structure`, `chapter_types` 등)이 backward-compat alias로 `section_results[0]`에서 복원 (A2.14) — multi-section data는 살아있지만 downstream은 section 0 가정

### C4.4 Production-affecting bugs (현재 진행)

11. **A12.3 split path overall_source_focus drop** — 출력 시 `_ap_parsed`가 chapter_decisions만 합치고 root field 누락. 2026-05-18 인수인계 outstanding.
12. **`_section_n_si = _source_inventory` NameError-guarded reuse (A16.6)** — A12 안 돌면 fragile (chapter route + `_chapter_plan_seed` None)
13. **outer `truncated_xml`/`removed_indices`/`idx_map` dead-but-computed** (A1.2) — cache-miss path에서 per-section이 덮어씀

### C4.5 Side effects on shared dirs

14. **`write_stage_debug_files`가 `/tmp/hwpx_debug/*.json` glob delete** (A19.5) — 외부 producer(A2.15 05d, A18 _d00~_d04)가 그 전에 쓰면 wipe. `.jsonl`은 살아남음 (의도적인지 검증 필요)
15. **`/tmp/hwpx_debug` numbering drift** — `12_` 빈 슬롯

### C4.6 데이터 흐름 손실 위험

16. **chapter_local_exemplars remap이 chapter ordering 가정** (A17.4) — A11 chapter ordering 변경 시 silently wrong index
17. **section 0 cache abort gate만** (A2.9) — multi-section에서 section 1~4 invalid silent
18. **PDF 50000자 silent truncate** (A1.3) — `pdf_to_text` 기본값. 13.7c는 `_broad_source[:50000]`로 명시했지만 PDF source 자체가 이미 한 번 잘림. 더 긴 source 미보호

### C4.7 두 데이터 source가 같은 정보를 producer로 갖는 패턴

19. **`extract_header_roles` analyzer 함수 vs DB tool inline** (A6.1) — MEMORY는 함수 호출이라 표기, 실제 dump는 inline. prompt builder는 양쪽 shape 수용 — 변경 시 두 곳 다 봐야 함
20. **`extract_marker_policies` (12.1) vs `marker_policy_1f` (1f)** — 둘 다 marker policy 정보 보유. 1f가 cache + structure, 12.1은 12.1 path만 — 일관성 검증 필요

---

## Unresolved references (cross-agent 합산 후 미해결)

### outputs that have no consumer (intentional debug-only confirmed)
- `_debug_payload["source_blocks"]` (A7.2)
- `_debug_payload["shallow_section_plan_compliance"]` (A10.6)
- `_debug_payload["target_unit_plan_phase_e_production"]["legacy_target_unit_plan_for_compare"]` (A9.3)
- `_debug_payload["chapter_types_phase_e_production"]` (A5.2)
- `_route_debug` (A10.1) → `_debug_payload["shallow_generation"]`
- `_chapter_plan_debug["source_diagnostic"]` (A14)
- `_debug_payload["title_role_consistency"]` (A15)
- `_debug_payload["multi_section_diagnostic"]["gate_decision"]` (A17.3)
- `_chapter_proc["adapted_title_deferred"]` (A18.2) — list build만, apply path 별도
- `_residual_candidates`, `_preserved_per_section` (A18.9) — debug build만

### outputs that have no consumer (likely dead, confirm + cleanup 후보)
- `analysis["original_xml"]` (A1.1)
- outer `removed_indices`, outer `idx_map` (A1.2)
- `_msgs_1f`, `_llm_1f` (A2.8) — `_marker_policy_1f`만 debug payload에 들어감
- `_role_registry` (A2.5) — `apply_structural_clustering` side effect로 충분, registry 자체는 debug only
- `_paragraphs_before`, `marker_norm` 가능성 (A2.1b)
- `messages_1` closure return key 미사용 (A2.1a)
- `_chapter_trees` list (A13.3) — dead-write since 13.7a-A1

### inputs whose producer was confirmed via cross-agent
- `_idx_full_texts` (A10.3, A8 cache sync) ← A2.1b 출력 → section_results[sid] / outer 복원
- `_marker_policy_1f` / `structure.get("marker_policy_1f")` ← A2.8
- `_cached`, `_cache_key`, `_dump_path`, `__event_emitter__`, `_call_llm` ← A1 setup
- `_source_split_log` ← A7.1
- `structure["template_grammar"]`, `["chapter_types"]`, `["role_text_types"]`, `["per_type_role_semantics"]`, `["exclusive_rules"]`, `["format_rules"]` ← A2 (1d/1e/1f)
- `_section_count`, `_actual_section_count` ← A1.7, A1.9

### genuinely uncertain (코드 직접 확인 필요)
- A9.4 A19.1 dual write: `is_plan_cache_valid`가 Phase E shape를 invalid로 판단하는지 → A19가 legacy AI 재호출 → cache 덮어쓰기 가능성
- `assert_chapter_object_invariants` 실패 시 raise vs log (C3 table)
- `_TEMPLATE_CORE_CASES` 외에 다른 hash 키 추가 의도

---

## 별도 참조 문서

- `_pipeline_audit_part_a1_a2.md` — Agent 1 원본 (A1+A2 상세, 458줄)
- `_pipeline_audit_part_a3_a10.md` — Agent 2 원본 (A3~A10 상세, ~560줄)
- `_pipeline_audit_part_a11_a19.md` — Agent 3 원본 (A11~A19 상세, ~450줄)
- `_pipeline_audit_part_b_c.md` — Part B + C 초기 draft
- `pipeline_audit_2026_05_11.md` — 이전 audit (2026-05-11), 13.7c 이전 시점
- `client_presentation_hwpx_pipeline.md` — 클라이언트 발표용 요약 (2026-05-11, untracked)

본 문서는 위 4개 source를 합쳐서 cross-reference + audit findings 통합한 것.
