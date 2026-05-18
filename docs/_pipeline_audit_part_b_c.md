# Pipeline I/O Audit — Part B + Part C (draft, pre-merge)

작성 기준: 워크트리 hwpx_analyzer.py + hwp_generator.py + DB tool `generate_document_hwp_local` (2026-05-18 기준). 이상적 설계 X — 실제 실행 흐름 그대로.

---

## Part B — side / debug-only / legacy paths

### B1 hint_tree 비교 실험 + canonicalize_by_data baseline [debug-only when `hybrid_mode=off`, hybrid replaces main path when on]
- 위치: DB tool 795~870
- 호출 시점: 1c parent post-correction 직후 (1e canonical AI 직전)
- 목적: parent_first(hint_tree) 트리 vs stack post-correction 트리 자기모순 비교 측정
- 입력:
  - `_paras_now` — stack 보정 직후 paragraphs (1c parent post-correction 결과, A2.x)
  - `_decisions_hint` — `level_parsed["decisions"]` (1c AI 결과 일부, A2.x)
  - `_hint_validation` — validate_parent_hints 결과 (코드)
  - `_CORE_CASES_ANSWERS` — 하드코딩 측정용 core idx set (코드 상수)
- 출력:
  - `_hint_tree_paras` — build_hint_tree(paragraphs) 결과. actually consumed: yes (hybrid_mode일 때 main path 교체) / no (off일 때 debug only)
  - `_tree_diff` — compute_tree_diff. actually consumed: yes — `_debug_payload["parent_hint_measurement"]["tree_comparison"]["diff"]`
  - `_pc_hint` — compute_parent_instance_children_by_parent_idx(hint_tree). actually consumed: yes — debug payload
  - `_excl_hint` — compute_exclusivity_rules_code(_pc_hint). actually consumed: yes — debug payload (hint_tree_exclusive_rules)
  - `_chapter_types_hint` — build_chapter_types_from_structure(hint deepcopy). actually consumed: yes — debug payload
  - `_stack_inconsistency`, `_pf_inconsistency` — measure_tree_inconsistency. actually consumed: yes — debug payload
  - `_pc_stack_by_pidx`, `_excl_stack_by_pidx` — stack parent_idx-based exclusive rules. actually consumed: yes — debug payload
- mutation: `hybrid_mode=on` 일 때 `structure["paragraphs"] = _hint_tree_paras` 로 교체 (line ~856). 이 경우 stack post-correction 결과는 `_stack_post_correction_paras`에 보존 후 debug payload `parent_correction.stack_post_correction_paragraphs`로만 들어감.
- failure: 각 try/except로 _excl_hint, _chapter_types_hint 등 개별 실패 허용 (log warning).
- audit note:
  - 🚩 `hybrid_mode` valve가 production 결과를 직접 바꾸는데, 변수명/주석에는 "측정 모드", "비교 실험"이라 표현 — name vs behavior 괴리.
  - 🚩 `_CORE_CASES_ANSWERS` 하드코딩 — 측정용이지만 코드 상수로 박혀 있음. CLAUDE.md "특정 문서명/문구 하드코딩 X" 원칙과 충돌 여부 점검 필요.
- confidence: medium (hybrid_mode 분기 두 갈래 모두 직접 확인했으나, `_CORE_CASES_ANSWERS` 정의 위치는 별도 grep 필요)

### B1b canonicalize_by_data baseline [debug-only]
- 위치: DB tool 866~875
- 호출 시점: hint_tree 분기 직후, 1e AI 직전
- 입력: `structure["paragraphs"]` deepcopy
- 출력: `_role_registry_baseline` (dict). actually consumed: yes — `_debug_payload["1e_canonical_clustering"]["role_registry_baseline_code"]` (debug only, main path 영향 X 명시)
- failure: try/except log warning만
- audit note: 주석 "main path 영향 X" 명시 — 명확히 debug
- confidence: high

### B2 13.7b B0a — Pre-1a Section Census [debug-only]
- 위치: DB tool 2633~2647
- 호출 시점: 2b chapter loop 직후, B2.2 직전 (assembly 직전 측정 단계)
- 목적: section별 metric (paragraph count, first preview 등) 측정 — B0b/B3 정책 결정용 evidence
- 입력: `template_path` (양식 파일 경로, A1.1에서 set)
- 출력:
  - `_section_census` — extract_section_census 결과 (sections list + reference_metrics dict)
  - actually consumed: yes — `_debug_payload["section_census"]` + B2.2 prompt 입력 + B0b measure_merge_feasibility 입력
- 코드: extract_section_census (hwpx_analyzer.py:10754). AI 호출 0.
- failure: try/except → `_debug_payload["section_census"] = {"error": ...}`
- audit note: 13.7b B0a 주석에 "shallow/chapter route 무관하게 모든 양식에 호출" — 즉 shallow route에서도 census는 측정됨
- confidence: high

### B3 13.7b B2.2 — Section Role Proposal [debug-only, chapter route only, AI 1 call + retry 1]
- 위치: DB tool 2650~2789
- 호출 시점: B0a section_census 직후
- 입력:
  - `_section_census["sections"]` (B0a 결과)
  - `section_results` (A2 결과 — 모든 section의 structure/chapter_types/marker_policy_1f 등)
  - template_title_hint (census[0].first_paragraph_preview[:200])
  - `_doc_ctx_b22` (template_title, section_count, route="chapter")
- AI: `build_section_role_proposal_prompt` → `_call_llm` (id: `hwpx_13_7b_section_role_proposal` / retry: `_retry`) → `parse_section_role_proposal_from_llm` → per-section `validate_section_role_proposal` → fallback `make_fallback_section_role_proposal`
- 출력:
  - `_srp_proposals` — list[dict] (각 section_role_proposal)
  - `_srp_validation_results` — list[dict]
  - `_srp_ai_info` — {raw_response_len, retry_count, validation_ok, errors}
  - `_srp_fallback_count` — int
  - `_debug_payload["section_role_proposals"]` ← `summarize_section_role_proposals(...)`. actually consumed: yes (B0b artifact 빌더 입력으로 사용 — 자기 자신 debug 외에도 _b0b_artifact에 들어감)
- failure: AI call 실패 → 모든 section을 `make_fallback_section_role_proposal(call_failed)` 채움
- audit note:
  - 🚩 "debug-only — production HWP 영향 X" 명시. 하지만 _srp_summary가 B0b artifact 입력 — 같은 layer 안에서만 소비.
  - "의미 매핑은 B0b review에서 사용자+claude 합의 (§9.5)" → 즉 결과는 사람이 보고 정책 결정용
- confidence: high

### B4 13.7b B0b — Post-1a Merge Feasibility [debug-only, chapter route only]
- 위치: DB tool 2791~2823
- 호출 시점: B2.2 직후
- 입력:
  - `section_results` (A2 결과)
  - `_section_census` (B0a 결과)
  - `_srp_summary_for_artifact` (B2.2 결과, error만 있으면 error만 추출)
- 코드:
  - `measure_merge_feasibility(section_results, _section_census)` → `_b0b_mf` (cross_section_parent + section_marker_policy_comparison 등)
  - `build_b0b_observation_artifact(_b0b_mf, section_role_proposals_summary=_srp_summary_for_artifact)` → `_b0b_artifact`
- 출력:
  - `_debug_payload["merge_feasibility"]` = _b0b_mf. actually consumed: debug only
  - `_debug_payload["b0b_observation_artifact"]` = _b0b_artifact. actually consumed: debug only (/tmp/hwpx_debug/13_7b_b0b_observation.json 작성 단계는 write_stage_debug_files)
- failure: try/except → error dict
- audit note: 🚩 "정책 결정은 B0b review에서 합의" — 데이터 수집만, downstream 영향 0
- confidence: high

### B5 12.0 Template Unit Observation [debug-only, AI 1 call + retry 1~2, cache 통합]
- 위치: DB tool 3509~3640 (대략 — assembly 후, 12.2/12.1 사이)
- 호출 시점: assembly 완료 후
- 목적: 양식의 unit 단위 mode (chapter_loop / shallow_flat / mixed 등) label 도출 — debug 분석용
- 입력:
  - `structure` (post-assembly, 1a~1f + Phase E + chapter_types 등 다 포함)
  - `_cached` (full cache, features 산출용 reference)
- AI:
  - cache 무효 시: `extract_template_unit_features` (code) → `build_template_unit_prompt` → `_call_llm("hwpx_template_unit_observation" / retry)` → `parse_template_unit_observation_from_llm` → `validate_unit_observation` → `derive_mode_label`
  - cache valid: `is_cache_valid(structure["template_unit_observation"])` 통과 시 그대로 사용
- 출력:
  - `_tuo_parsed` (unit_observations / not_assessed_units / cross_unit_concerns / ambiguity_flags)
  - `_tuo_label` (label + confidence_level)
  - `_tuo_val` (validation result)
  - cache 통합: cache 무효 시 `structure["template_unit_observation"]` 업데이트 + cache write-back
- actually consumed:
  - `template_unit_observation` → A8 13.7e early target_unit_planning + A9 production override + 12.2 debug. unit_observations + derived_mode_label 다양한 곳에서 참조
  - debug 외에도 production target_unit_plan에 영향 가능 (legacy path)
- failure: AI fail → blockers + ambiguity_flag, unit_observations 빈 list, label undetermined
- audit note:
  - 🚩 "debug-only"라고 주석되어 있지만 실제로는 production target_unit_plan 후보 (A8/A9)에서 unit_observations 입력으로 사용 — 진짜 debug-only인가? confidence: medium (downstream 영향 검증 필요)
  - cache 무효일 때 production AI cost 발생 (debug only라면 valve로 끌 수 있어야 함)
- confidence: medium

### B6 11.2 Style Profile Observation [dead-code]
- 위치: DB tool 3500~3505
- 코드:
  ```python
  # ── 11.2 Style Profile Observation (DISABLED for speed) ──
  try:
      pass  # Style profile AI calls disabled to reduce latency
  except Exception as _sp_e:
      log.warning(f"[STYLE-PROFILE] 실패 (pipeline 영향 없음): {_sp_e}")
  ```
- 호출 시점: 12.0 직전
- 입력: 없음 (pass)
- 출력: 없음
- 관련 함수 (사용 안 함): `build_style_profile_prompt` (hwpx_analyzer.py:6277), `parse_style_profile_from_llm` (6333), `_collect_style_samples` (6138)
- audit note:
  - 🚩 imports 라인 216~217에서 import는 살아있는데 실제 호출은 없음. dead import.
  - DB tool 함수 정의도 함께 남아있음. cleanup 후보.
- confidence: high (literal pass + comment 명시)

### B7 Legacy 분석용 AI prompts [legacy, not in HWPX generation path]
- 함수:
  - `build_role_interpret_prompt` (hwpx_analyzer.py:547) + `parse_role_interpret_from_llm` (591)
  - `build_role_content_prompt` (658) + `parse_role_content_from_llm` (730)
  - `build_content_mapping_prompt` (4864)
- 호출처 (repo-wide grep):
  - `routers/files.py:1764, 1766, 1841, 1852` — `build_role_interpret_prompt`, `build_role_content_prompt` 사용
  - `build_content_mapping_prompt`: 정의만 있고 호출처 없음 → dead
- HWPX generation pipeline (DB tool generate_document_hwp_local)에서 호출 없음 — separate file analysis endpoint용
- audit note: 🚩 같은 hwpx_analyzer.py 안에 HWPX generation용 함수 + 별도 분석 endpoint용 함수가 섞여있음. 분리 cleanup 후보.
- confidence: high

### B8 Dead imports — exclusivity/format AI [dead-code]
- 함수:
  - `build_exclusivity_analysis_prompt` (hwpx_analyzer.py:4747) + `parse_exclusivity_from_llm` (4809)
  - `build_format_analysis_prompt` (4576) + `parse_format_rules_from_llm` (4616)
- DB tool import: 라인 197~201
- DB tool 실제 호출처: 없음 (grep 결과: only the import lines)
- 대체된 함수:
  - 1d: `compute_exclusivity_rules_code` (line 7737 + 7758 hybrid 분기) — code
  - 1e (format): `compute_format_observations` (4342) + `compute_format_rules_code` (7827) — code
- DB tool 주석 (line 1023): "AI 호출 폐기. 결정적·고속·무토큰."
- audit note:
  - 🚩 import는 살아있고 함수 정의도 남아있음 — cleanup 후보.
  - 🚩 함수 이름 `parse_format_rules_from_llm`이 살아있어 새 작업자가 잘못 import할 위험.
- confidence: high

### B9 Source block adapter / 13.0 debug-only [debug-only]
- 위치: DB tool 1767~1779
- 호출 시점: 2a 직후 (source_sections split 직후)
- 코드: `text_blob_to_source_blocks(_source_text_for_blocks)` (source_block_adapter.py:24)
- 출력:
  - `_source_blocks` (list[dict])
  - `_debug_payload["source_blocks"] = {block_count, source_length, blocks}`. actually consumed: debug only (no downstream code reads `source_blocks`)
- 관련 함수: `compute_preserve_indices` (source_block_adapter.py:99) — DB tool에서 import는 됐지만 호출 grep 필요. likely dead.
- audit note: 🚩 13.0 adapter는 13.7c 도입 전 source preserve 후보 매핑용 — 13.7c가 source-side 매핑을 흡수했다면 dead 가능성. confidence: medium
- confidence: medium

### B10 hint_tree 측정 관련 helpers — `validate_parent_hints`, `classify_hint_conflicts`, `build_hint_override_tree`, `build_hint_tree`
- 정의: hwpx_analyzer.py:2621, 2664, 2716, 2742
- 호출: B1 hint_tree 분기 안에서만 (DB tool 795~)
- `hybrid_mode=on` 일 때 main path 교체, `off` 일 때 debug only
- audit note: B1 항목 참조

---

## Part C — cross-cutting

### C1 Cache schema (namespace='full', `CACHE_SCHEMA_VERSION = 6`)
- 위치: hwpx_analyzer.py:5049 (constant), 5083~5120 (save/load), 5046 (`TEMPLATE_CACHE_DIR = "/tmp/hwpx_cache"`)
- 파일 경로:
  - full: `<TEMPLATE_CACHE_DIR>/<cache_key>.json` (suffix 없음)
  - step1ab: `<TEMPLATE_CACHE_DIR>/<cache_key>_step1ab.json`
- 버전 호환: `cached_version < CACHE_SCHEMA_VERSION` → 자동 invalidate (load_template_cache가 None 반환)
- 버전 이력 (memory 기준):
  - v6: Phase E + chapter_pattern_family 통합 (현재)
  - v5: section_results 분리 (B2.1.1)
  - 이전 버전은 memory/handoff 참조
- Top-level keys (실제 cache file 인스펙션 결과):
  ```
  structure                        — main 1a~1f + chapter_types + grammar + target_unit_plan + template_unit_observation 등 다 포함
  chapter_types                    — structure.chapter_types와 중복 (outer alias)
  signals                          — _signals (compute_role_context_signals)
  idx_texts                        — _idx_texts (paragraph idx → text preview)
  idx_full_texts                   — _idx_full_texts (full text)
  marker_policy_1f                 — structure.marker_policy_1f와 중복 (outer alias)
  paragraph_count, table_count     — analysis 결과 sanity check용
  template_file_id                 — original file_id (cache 해시 fallback)
  section_count                    — extract_all_sections_xml 길이
  section_results                  — {sid: {structure, chapter_types, marker_policy_1f, signals, idx_texts, idx_full_texts}}
  cache_schema_version             — int
  ```
- `structure` 내부 keys (실측):
  ```
  paragraphs, tables, validator_issues, exclusive_rules, format_rules, blank_rules,
  marker_policy_1f, chapter_types, template_grammar, role_text_types,
  per_type_role_semantics, target_unit_plan, template_unit_observation
  ```
- `section_results[sid]` keys (실측):
  ```
  structure, chapter_types, marker_policy_1f, signals, idx_texts, idx_full_texts
  ```
- audit note:
  - 🚩 top-level과 structure 양쪽에 `chapter_types` / `marker_policy_1f` 중복. outer는 alias로만 작동하지만 두 갈래로 fetch 가능 — drift 위험.
  - 🚩 section_results[sid]에도 같은 keys 중복 (전체 schema 깊이 3겹: top-level / structure / section_results)
  - 🚩 cache hit 시 outer alias 우선 (DB tool 487~492), miss 시 structure[...]가 source-of-truth — 분기 일관성 점검 필요
  - 🚩 Phase E `phase_e_chapter_planner` + Track C `chapter_pattern_family` 결과는 schema v6에서 통합됐다 하지만, 실측한 cache file에 둘 다 top-level 없음 — write 분기 확인 필요 (단순히 해당 양식이 not_ok였을 수도)
- confidence: high (top-level/structure는 직접 인스펙션), medium (Phase E key write 분기는 추가 확인 필요)

### C1b cache write 패턴
- save: DB tool 1115~1130 (section0 can_cache + section_results 채우면 incremental save), 1828~1847 (13.7e early), 3402 (12.2 target unit plan), 12.0 (template_unit_observation)
- write-back pattern (in-memory + cache load → mutate → save):
  ```python
  _wb = load_template_cache(_cache_key, namespace='full')
  if _wb and "structure" in _wb:
      _wb["structure"][KEY] = NEW_VALUE
      # + section_results[0].structure도 sync (cache schema v5)
      save_template_cache(_cache_key, _wb)
  ```
- audit note: 🚩 write-back 패턴이 여러 단계(13.7e, 12.0, 12.2, Phase E)에 분산 — 같은 cache를 여러 phase가 partial mutation. race 가능성 0 (single process) 이지만 consumer는 어느 단계 결과를 읽는지 헷갈릴 위험.
- confidence: high

### C2 Debug 파일 위치 + write_stage_debug_files 매핑
- 통합 dump: `/tmp/hwpx_debug_last.json` (전체 _debug_payload — DB tool 매 단계 후 누적 dump)
- 분리 dump: `/tmp/hwpx_debug/*.json` — `write_stage_debug_files(debug_payload)` (hwpx_analyzer.py:8439)
- write_stage_debug_files는 매 호출 시 기존 `*.json` 전부 삭제 후 재생성 (8455~8457)
- 파일 매핑 (소스 grep 결과):
  ```
  01_template_paragraph_analysis.json     ← structure_before_split.paragraphs + 1b_role_candidates
  02_level_parent_tree.json               ← structure_after_split.paragraphs + parent_correction + 1c_structure_global.decisions
  03_role_clustering.json                 ← structure_after_split.paragraphs (role 카탈로그) + 1e_canonical_clustering.role_registry + per_type_role_semantics
  04_chapter_types.json                   ← chapter_types + template_grammar.per_type
  05_template_grammar.json                ← template_grammar (per_type / global / observed_transitions)
  05b_cache_validation.json               ← cache_validation
  05c_marker_policy_induction.json        ← marker_policy_1f
  05d_section_cache_validations.json      ← section별 _cache_validation (DB tool에서 직접 쓰기, write_stage_debug_files 외부)
  06_type_catalog.json                    ← _build_rich_type_catalog 결과 (chapter_classify 2a prompt 입력)
  07_2a_type_selection_result.json        ← chapter_classify.chapters + header_data
  07b_source_split_decision.json          ← source_split_decision (_source_split_log)
  08_2b_generation_by_chapter.json        ← section_fill (chapter별 AI prompt + parsed items)
  09_grammar_validation_result.json       ← (정확한 producer 미확정 — likely process_section_fill_result 안 grammar validation)
  10_assemble_result.json                 ← assembly (success/fail count + errors + marker_rewrite_log)
  11_validation_summary.json              ← (likely build_validation_summary at hwpx_analyzer.py:7331)
  13_template_unit_observation.json       ← template_unit_observation (12.0)
  13_7b_b0b_observation.json              ← b0b_observation_artifact (13.7b B0b)
  14_marker_roundtrip_readiness.json      ← marker_roundtrip_readiness (12.1)
  15_target_unit_planning.json            ← target_unit_planning (12.2)
  16_source_blocks.json                   ← source_blocks (13.0)
  17_section_role_proposals.json          ← section_role_proposals (13.7b B2.2)
  18_merge_feasibility.json               ← merge_feasibility (13.7b B0b raw)
  19_section_local_decisions.json         ← section_local_decisions (13.7b section-local A16)
  20_section_local_chapter_lists.json     ← section_local_chapter_lists (13.7b section-local A16)
  99_debug_summary.json                   ← 메타데이터 (model + cache 정보 등)
  _d02_anchor_per_ci.jsonl                ← chapter_anchors 매핑 (assembly 시 _replace_text 박은 element id) — hwp_generator 가 쓰는 듯
  ```
- audit note:
  - 🚩 번호 비어있음: 12_ 없음. 과거에 12_가 template_unit_observation이었다가 13_으로 이동된 흔적 (numbering drift).
  - 🚩 `09_grammar_validation_result` + `11_validation_summary` producer 정확한 위치는 추가 grep 필요 — confidence: low
  - 🚩 write_stage_debug_files가 매 호출 시 dir 전체를 삭제 (`_glob_mod.glob("*.json") → os.remove`) — 외부에서 같은 dir에 다른 json 두면 사라짐 (예: 05d는 DB tool에서 직접 쓰는데 write_stage_debug_files 호출 전이면 삭제 위험)
  - 🚩 `_d02_anchor_per_ci.jsonl`은 .json 확장자가 아니라 .jsonl이라 cleanup 대상 X — 의도적인지 확인 필요
- confidence: medium (대부분 high, 09/11 producer 미확정)

### C3 Validation gates (실행 순서대로)
| ID | 함수 | 위치 | 역할 | 실패 시 |
|---|---|---|---|---|
| 1f-V | `verify_marker_policy_evidence` | hwpx_analyzer.py:3799 | 1f marker policy의 evidence consistency | role.verification = consistent/inconsistent (drop X) |
| 1a-V | `_validate_selected_index` | 3842 | 1a paragraph selected_index 유효성 | 무효면 split |
| structure-V | `_validate_and_split` | 3876 | 1a 결과 paragraph 분할/검증 | |
| cache-V | `validate_structure_for_cache` | 5142 | 7단계 gate (SC1~SC?) | should_abort=True → raise ValueError ("구조 분석 오류 — 캐시 저장 안 함"). section0만 abort gate 작동 |
| 12.0-V | `validate_unit_observation` | template_observer.py:627 | unit observation evidence | blockers → unit_observations 비우고 label undetermined |
| 12.2-V | `validate_target_unit_plan` | target_unit_planner.py:427 | regions coverage/overlap/granularity | invalid → empty plan |
| Phase E-V | `validate_toc_based_chapter_plan` | hwpx_analyzer.py:16078 | TOC plan 검증 | ai fail → no_toc_deferred |
| Track C-V | `validate_chapter_pattern_family` | 17087 | pattern family 검증 | invalid → expandable=false 강등 |
| 13.7c-V | `validate_adaptation_decision` | 11983 | adaptation decision 필드 enum | violation → make_validation_failed_decision로 강등 |
| 13.7c-N | `normalize_adaptation_decision` | 12203 | normalize alias + enum 정규화 | — |
| B2.2-V | `validate_section_role_proposal` | 12798 | section role proposal validity | invalid → make_fallback_section_role_proposal |
| 13.3-V | `validate_shallow_output` | 9709 | shallow_fill output | invalid → ? (Agent 2 확인 필요) |
| 2b-V | `validate_ai_parent_ids` | 15091 | 2b items의 parent_id refs | invalid → `apply_parent_id_fallback` |
| 13.7a-V | `assert_chapter_object_invariants` | 11205 | chapter_object 필드 invariant | violation 시 raise / log 어느쪽인지 코드 확인 필요 |
| validation-summary | `build_validation_summary` | 7331 | 종합 (11_validation_summary.json 생성 추정) | — |
| roundtrip-V | `validate_reconstruction`, `validate_text_quality` | 7083, 7128 | XML 재구성 검증 | — |
- audit note:
  - 🚩 validation gate가 phase별로 분산 — 어느 gate가 실제 production 차단, 어느 gate가 debug warning인지 layer 일관성 검증 필요
  - 🚩 `validate_structure_for_cache`는 section0만 abort gate — multi-section에서 section1~4의 invalid는 silent (`05d_section_cache_validations.json` 측정만)
- confidence: medium (assert_chapter_object_invariants 실패 동작 확인 필요)

### C4 🚩 audit notes (pre-merge — Agent 결과 합친 후 확정)

#### 잠정 발견 (Part A/B/C 합치기 전 단계)

1. **`hybrid_mode` valve가 production 동작 변경** — 이름과 다름 (B1)
2. **`_CORE_CASES_ANSWERS` 하드코딩** — 측정 보조 코드 상수, CLAUDE.md 원칙 충돌 가능 (B1)
3. **dead imports** — `build_exclusivity_analysis_prompt`, `parse_exclusivity_from_llm`, `build_format_analysis_prompt`, `parse_format_rules_from_llm` (B8)
4. **dead-code** — `build_content_mapping_prompt` 정의만, 호출처 0 (B7)
5. **dead-code** — `build_style_profile_prompt`, `parse_style_profile_from_llm` 11.2 path가 literal `pass` (B6)
6. **legacy함수가 같은 파일 안에 섞여있음** — `build_role_interpret_prompt`/`build_role_content_prompt` 는 routers/files.py 전용인데 hwpx_analyzer.py에 있음 (B7)
7. **cache schema 중복** — top-level / `structure[...]` / `section_results[sid]` 3계층에 같은 key (chapter_types, marker_policy_1f). drift 위험 (C1)
8. **cache write-back 분산** — Phase E / 13.7e / 12.0 / 12.2 각자 부분 mutation (C1b)
9. **numbering drift** — debug 파일 12_ 비어있음, 13_으로 이동 흔적 (C2)
10. **write_stage_debug_files가 dir 전체 삭제** — 외부에서 같은 dir에 쓰는 파일(예: 05d) cleanup 위험 (C2)
11. **section0만 cache gate** — multi-section에서 section1~4 invalid silent (C3)
12. **두 단계 "1e" label** — DB tool 877 (AI canonical_clustering) + 1021 (code format_rules) 둘 다 "1e"로 주석. 코드 가독성 ↓ (Agent 1 보고 후 확정)
13. **target_unit_plan 이중 trigger** — A8 (13.7e early, cache write) + A9 (Phase E override, in-memory only) — 같은 키를 두 단계가 set. consumer는 어느 결과 보는지 명확? (Agent 2 보고 후 확정)
14. **12.0 template_unit_observation "debug-only" 표기 vs 실제 production target_unit_plan 입력** — A8/A9에서 unit_observations 참조 (B5)

---

## Unresolved references — Part B + C (pre-merge)

### outputs I produced but couldn't find consumer for
- `_section_role_proposals` (B2.2 결과): _debug_payload에 들어가는 것 외에는 read 처 없음 — 의도된 debug-only로 간주. confidence: high.
- `_b0b_observation_artifact` (B4 결과): _debug_payload에만. 의도된 debug-only.
- `_source_blocks` (B9 결과): _debug_payload에만. dead 후보. confidence: medium.

### inputs I read but couldn't find producer for
- (B1) `_CORE_CASES_ANSWERS` — 어디서 정의? 코드 상수 grep 필요 (메인 agent task)
- (C2) `09_grammar_validation_result.json` producer — write_stage_debug_files에서 명시적으로 안 보임. 다른 path?
- (C2) `11_validation_summary.json` producer — `build_validation_summary`가 후보지만 실제 write 위치 미확인
- (C2) `_d02_anchor_per_ci.jsonl` producer — hwp_generator에서 write 추정. Agent 3 결과 확인 필요

### 합치기 후 Agent 결과로 확정할 것
- 1e label 두 번 사용 (Agent 1 확인)
- 1f AI vs code 최종 판정 (Agent 1)
- target_unit_plan trigger 두 번 (Agent 2)
- 12.0 template_unit_observation downstream 참조 정확한 위치 (Agent 2, A8/A9)
- 13.7c split path overall_source_focus drop 동작 (Agent 3)
- hwp_generator _replace_text가 어느 chapter_object 필드 사용 (Agent 3)
