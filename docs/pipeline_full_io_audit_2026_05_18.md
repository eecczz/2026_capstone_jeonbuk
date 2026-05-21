# HWPX Pipeline — Full I/O Audit (2026-05-18, refresh 2026-05-21)

## 목적

현재 구현 기준으로 전체 파이프라인을 실제 실행 순서대로 정리한 문서. 이상적 설계 X — `DB tool generate_document_hwp_local` + `hwpx_analyzer.py` + `hwp_generator.py` + 관련 utils가 **실제로** 어떤 순서로 실행되고, 각 단계가 무엇을 입력받아 무엇을 출력하며, 그 출력이 **실제로 어디서 소비되는지** 정리.

본 문서로 발견 가능한 것:
- 생성됐지만 consumer 없는 output
- 읽지만 producer 모호한 input
- 같은 정보를 여러 단계가 set/override (drift 위험)
- 코드는 살아있지만 호출 0인 dead-code / legacy / disabled

기준 상태: 마지막 git commit `8d1e909` (2026-05-20 refresh) **+ 워크트리 dirty (Sprint 1+2+3 미커밋, 2026-05-20 cp+DB+SIGHUP으로 배포된 상태)**. 변경 시점에 따라 line number는 drift 가능.

## 2026-05-21 Refresh 요약 (Sprint 1+2+3 — 양식 분석 phase에 말투/마커-내용 글꼴/inline 강조 layer 추가)

이전 5-20 refresh 후 워크트리 dirty 상태로 누적된 Sprint 1+2+3 변경 반영. 미커밋이지만 production(`/app`)에는 cp+SIGHUP으로 이미 배포됨 (handoff_2026_05_20). 영향 받은 섹션은 본문 내 `[2026-05-21 update]` 마커로 표시.

| 분류 | 변경 | 영향 섹션 |
|---|---|---|
| **Sprint 1 — 11.2 본체 (style profile)** | cluster별 말투 rule AI 추출. STYLE_PROFILE_PROMPT + `_collect_style_samples` (80K budget stratified) + 10개 cluster batch | **NEW A5b.1** / B6 (dead → active) / A19.3 (옛 자리 dead pass block 잔존) |
| **Sprint 1 — 11.2b emphasis** | cluster별 inline 강조 layer (charPr 다양성) AI 분석. `extract_paragraph_emphasis_map` (raw zip + tbl cell 안 run 포함) + EMPHASIS_LAYER_PROMPT + base/layer 판정 | **NEW A5b.2** |
| **Sprint 1 — style namespace cache + ANALYSIS_ONLY_MODE valve** | main cache 와 분리된 `<hash>_style.json` (STYLE_CACHE_SCHEMA_VERSION=2). ANALYSIS_ONLY_MODE valve로 분석 phase 끝나면 early return | **NEW A5b.3** + A1.6 valve list / C1 (cache schema 별도 namespace) |
| **Sprint 2A — 2b prompt에 style rule** | `build_section_fill_prompt`에 `style_profiles` 인자 + "role별 말투 가이드" 섹션 prompt 박음 | A13.1 |
| **Sprint 2B — 조립 단계 marker/content 글꼴 분리** | `_replace_text_in_paragraph_elem_split` (양식 t element 중 첫 글자 매칭으로 marker_t 선택, content_t = 가장 긴 t). 양식 텍스트박스 cell의 t[0]이 공백 case 우회 | A18.6 (chapter title path) / A18.10 (body item path) |
| **Sprint 3A — 2b prompt에 emphasis layer + markup 지시** | SECTION_FILL_PROMPT에 "강조 표시 — 양식 글꼴 분리 보존" 섹션 + `[[emN]]...[[/emN]]` markup 형식 + `build_section_fill_prompt`에 `emphasis_layers`, `paragraph_emphasis_map` 인자 | A13.1 / SECTION_FILL_PROMPT |
| **Sprint 3B — emphasis markup parser** | `_parse_emphasis_markup` — `[[(em\d+)]](.*?)[[/em\d+]]` 매칭 + `valid_layer_ids` set으로 AI 환각 markup 무시 | A18.10 sub-step |
| **Sprint 3C — emphasis-aware run split** | `_replace_text_with_emphasis_segments` — Sprint 2B split + content_t를 segments별 새 run으로 분할 + `charpr_map[layer_id]` 적용 | A18.10 sub-step |
| **Sprint 3D — assemble + DB tool wire** | `assemble_hwpx_hybrid` 시그너처에 `emphasis_layers=None` 추가. body item path에서 emphasis-aware / Sprint 2B split / 기존 path 3분기. tbl_box cell paragraph target. **markup 0개 pre-parse**로 emphasis path 우회 (cluster base 박힘 회피, 양식 본래 글꼴 보존) | A18.10 |
| **marker_separator.py — leading emphasis markup 인식** | AI가 marker도 `[[emN]]<marker>[[/emN]]`로 감싸 출력하는 경우 strip_marker가 markup 안쪽으로 marker 매칭 | A18.6b / A18.10 (strip_marker path) / A19.2 |

→ **검증 완료** (사용자 어제 양식 실행으로 4 항목 확인 완료, handoff_2026_05_20 "검증해야 할 것" 통과). 다음은 다른 양식(민원인/CC7)에서도 정상 작동 + 회귀 case 발견 시 fix.

## 2026-05-20 Refresh 요약 (이전 5-18 audit 대비 주요 변경 — 17 commit)

이전 audit 작성 후 추가된 17개 commit이 파이프라인 위상을 크게 바꿈. 영향 받은 섹션은 본문 내 `[2026-05-20 update]` 마커로 표시.

| 분류 | 변경 | 영향 섹션 |
|---|---|---|
| **flow reorg** | Phase E를 1c 후로 이동 (`3346bf0`) — 1e canonical clustering이 chapter context(chapter_id) 알고 결정 | A2.2 → **Phase E inline (new A2.2b)** → A2.5 / A3 reduced role |
| **1e clustering 강화** | 자식 약화(부모 강한 신호) + chapter root cross-chapter 통합(`cc5786f`) | A2.5 |
| **형제 배타 모델 전환** | blacklist(exclusive_rules.variants) → white-list(`sibling_cooccurrence_rules`) (`d4088a5`) + variant 단위 표현(`f8a6304`) + instance-aware + pattern_tree multi_variant 경고(`f05b773`) | A2.6 / A13 prompt input |
| **1f table_kind 도입** | marker_policy_1f role entry에 `table_kind` (decorative_box/real_table/not_applicable) 추가 (`535e3a4`) | A2.8 / A18.10 outer fallback safety |
| **2b prompt 강화** | root role hard constraint + instance count hint (`7f35fc0`) + chapter title 답습 금지 (`81aceb4`, `e1723f3` 일반화) | A13 |
| **chapter title marker 자동 조립** | assembly 단계에서 양식 1f marker_policy로 chapter title marker 자동 부착 (`b3ff4ca`/`92823df`/`7acc9f1`). AI input은 `strip_chapter_title_marker`로 정제 → AI는 의미만 결정, code가 형식(marker) 책임 | **NEW A18.6b** / A12 input strip / A6 |
| **표지(cover) preserve** | chapter title 첫 등장 전 doc.paragraphs 통째 header_indices 추가 (`e7fab74`) — truncate_xml이 빈 paragraph 제거하여 1a-doc 매핑 깨지는 문제 우회 | **NEW A18.5b** |
| **blank line region-aware** | blank 빈 줄도 chapter_anchors[ci] 뒤에 insert (body item과 같은 path, `b0e6ffc`) | A18.10 |
| **`_replace_text_in_paragraph_elem` t-run 보존** | cover table cell처럼 ctrl run + text run 혼합 paragraph에서 text run을 지우지 않도록 has_t 체크 추가 (`8d1e909`) | A18.6 (via marker_separator 대체 path) |
| **seed local_title_role fallback** | `extract_chapter_template_plan_seed`이 sub-tree 없는 chapter(표/일정 위주 마지막 chapter)도 첫 paragraph role을 항상 채움 (`2cbcc4f`) | A11 |
| **13.7c split 임계값 완화** | `should_split_adaptation_batch` default budget=128000, ratio=0.95 → threshold ~121,600자 (이전 60,000) (`18e187b`) — 작은 chapter set은 single batch 유지하여 overall_source_focus 일관성 확보 (handoff fix a 완료) | A12.3 |
| **cache version bump** | CACHE_SCHEMA_VERSION 6 → 11 (paragraph.chapter_id, sibling_cooccurrence_rules, child_set_variants, multi_variant_parents 경고, 1e prompt 강화 누적) | C1 |

[handoff fix (a)] 완료. 남은 fix (b) split path overall_source_focus 합치기는 큰 양식(수십+ chapter)에서만 유효 — 현재 양식 3개는 single batch 유지로 우회 가능.

### commit traceability index (5/18 → 5/20)

신규 commit과 본문 섹션 매핑. 변경 코드 위치도 함께.

| commit | 설명 | 본문 섹션 | 핵심 코드 |
|---|---|---|---|
| `3346bf0` | Phase E를 1c 후로 이동 | A2.2b, A2.5, A3, C1.4 | `hwpx_analyzer.py:17284` (`assign_chapter_ids_from_phase_e`) + `:3108` prompt |
| `cc5786f` | 1e clustering — 자식 약화 + chapter root cross-chapter 통합 | A2.5, C1.2 | `:2969` (`CANONICAL_CLUSTERING_PROMPT`) |
| `d4088a5` | 형제 배타 blacklist → cooccurrence white-list | A2.6, A13.1, C1.4 | `:7880` (`compute_sibling_cooccurrence_rules`) |
| `f8a6304` | 형제 배타 — child_set_variants (variant 단위) | A2.6, A13.1, C1.2 | `:7880` (variants field) |
| `f05b773` | cooccurrence instance-aware + pattern_tree multi-variant 경고 | A2.6, A13.1, C1.2 | `:7880` (samples field) + `_format_pattern_tree` |
| `535e3a4` | 1f table_kind (decorative_box vs real_table) | A2.8, A18.10 | `:3701+` (`MARKER_POLICY_PROMPT`) + `hwp_generator.py` outer fallback |
| `7f35fc0` | 2b prompt — root role hard constraint + instance count hint | A13.1 | `:14716+` (`SECTION_FILL_PROMPT`) |
| `81aceb4` | 2b prompt — chapter title 답습 금지 | A13.1 | `SECTION_FILL_PROMPT` 신규 섹션 |
| `e1723f3` | SECTION_FILL_PROMPT 일반화 (양식 specific 예시 제거) | A13.1 | `SECTION_FILL_PROMPT` |
| `b3ff4ca` | chapter title marker 자동 조립 (assembly) | A18.6b | `hwp_generator.py:1696-1725` |
| `92823df` | chapter title marker — UnboundLocal fix + 13.7c input strip | A12.2, A18.6b | `hwp_generator.py:1541-1543` + `hwpx_analyzer.py:10208` (`strip_chapter_title_marker`) |
| `7acc9f1` | chapter title marker sibling_index 1-based 보정 | A18.6b | `hwp_generator.py:1709` (`ci + 1`) |
| `e7fab74` | 표지(cover) preserve | A18.5b | `hwp_generator.py:1806-1830` |
| `b0e6ffc` | blank line region-aware placement | A18.10 | `hwp_generator.py:2678-2719` |
| `8d1e909` | `_replace_text_in_paragraph_elem` t-run 보존 | A18.6 | `hwp_generator.py:2868~` (has_t check) |
| `2cbcc4f` | seed local_title_role fallback | A11 | `hwpx_analyzer.py:10050` (`extract_chapter_template_plan_seed`) |
| `18e187b` | 13.7c split 임계값 완화 (gpt-5.4 128k 활용) | A12.3, C4.4 | `hwpx_analyzer.py:12511` (`should_split_adaptation_batch`) |

### 2026-05-20~21 dirty workfile traceability (워크트리 미커밋, production에는 배포됨)

| Sprint | 설명 | 본문 섹션 | 핵심 코드 (실측 line) |
|---|---|---|---|
| **1A** | 11.2 본체 — STYLE_PROFILE_PROMPT + sample 수집 + 10 cluster batch + parse | A5b.1, B6 | `hwpx_analyzer.py:6221` (prompt) / `:6275` (`_collect_style_samples`) / `:6442` (`build_style_profile_prompt`) / `:6501` (`parse_style_profile_from_llm`) |
| **1C** | 11.2b emphasis — extract_paragraph_emphasis_map + EMPHASIS_LAYER_PROMPT + base/layer 판정 | A5b.2 | `:6584` (`extract_paragraph_emphasis_map`) / `:6774` (prompt) / `:6843` (`build_emphasis_layer_prompt`) / `:6903` (`parse_emphasis_layer_from_llm`) / `:14380` (`_build_1a_to_xml_p_idx_mapping` — 1C에서 기존 13.7b helper 재사용) |
| **1D** | ANALYSIS_ONLY_MODE valve (DB tool) — 11.2/11.2b 완료 후 1차 dump 후 2a 직전 early return | A1.6, A5b.3 | DB tool valve only |
| **1E** | style namespace cache (`<hash>_style.json`, STYLE_CACHE_SCHEMA_VERSION=2). main cache와 독립 invalidate | A5b.3, C1 | `:5218` (`save_template_cache(namespace=...)`) / `:5234` (`load_template_cache(namespace=...)`) |
| **2A** | 2b prompt에 style rule 박기 (`style_profiles` 인자) | A13.1, SECTION_FILL_PROMPT | `:15509` (`build_section_fill_prompt`) |
| **2B** | 조립 단계 marker/content 글꼴 분리 — 첫 글자 매칭으로 t[0] 공백 cell case 해결 | A18.6 (chapter title), A18.10 (body) | `hwp_generator.py:3260` (`_replace_text_in_paragraph_elem_split`) — chapter title 호출 `:1745`, body 호출 `:2833` |
| **3A** | 2b prompt에 emphasis rule + `[[emN]]...[[/emN]]` markup 지시 | A13.1, SECTION_FILL_PROMPT | `:15263` (prompt 강조 섹션) / `:15509` (`emphasis_layers`, `paragraph_emphasis_map` 인자) |
| **3B** | emphasis markup parser — valid_layer_ids로 AI 환각 markup 무시 | A18.10 sub-step | `hwp_generator.py:3362` (`_parse_emphasis_markup`) |
| **3C** | emphasis-aware run split + charpr 매핑 | A18.10 sub-step | `:3401` (`_replace_text_with_emphasis_segments`) |
| **3D** | assemble + DB tool wire — body item 3분기 path + tbl_box cell target + markup 0개 우회 | A18.10 | `:1089` (`assemble_hwpx_hybrid(emphasis_layers=...)`) / body path `:2782-2844` |
| **strip_marker** | leading emphasis markup wrap 인식 | A18.6b / A18.10 / A19.2 | `marker_separator.py:strip_marker` (~line 83 부근 patch) |

→ Sprint 1+2+3은 **단일 commit으로 묶이지 않은 워크트리 dirty** 상태로 5/19~5/20 동안 누적. 사용자가 cp+DB update+SIGHUP만으로 배포. handoff_2026_05_20에 "git commit 안 함" 명시. 검증 완료 후 본 audit refresh와 함께 git commit 예정.

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

### A1.6 hybrid_mode / canonical_mode / ANALYSIS_ONLY_MODE valves [active] **[2026-05-21 update — ANALYSIS_ONLY_MODE 추가]**
- 위치: `dbtool:433-437` + ANALYSIS_ONLY_MODE는 DB tool valves 정의 블록
- 출력:
  - `hybrid_mode` (bool) — gates A2.3 (parent_hint_measurement). 현재 OFF
  - `canonical_mode` (str) — passed to `merge_levels_into_structure(canonical_mode=...)`
  - `ANALYSIS_ONLY_MODE` (str, "on"/"off") — Sprint 1D valve. **ON** 시 A5b 끝나고 1차 debug dump 후 2a 진입 전 early return (A5b.3 참조). cluster style + emphasis 분석 검증 전용 모드.
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

### A2.2b Phase E inline — TOC chapter planner → chapter_id 부여 [active] **[2026-05-20 update — NEW position]**
- commit `3346bf0` "Phase E를 1c 후로 이동". 이전엔 A3에서 별도 stage로 호출되었으나, 1e canonical clustering이 chapter context를 알고 cluster 결정하도록 1c 직후로 이동.
- 호출 함수: `has_toc_gate` (`:16128`) → `build_toc_based_chapter_plan_prompt` (`:16390`) → `parse_toc_based_chapter_plan_from_llm` (`:16481`) → `validate_toc_based_chapter_plan` (`:16505`) → `assign_chapter_ids_from_phase_e` (`:17284`)
- mutation: 각 `structure["paragraphs"][i]`에 `chapter_id: int` 부여 (`-1` = chapter 밖, 표지/header/TOC 등). Phase E 실패/no_toc → 모두 `-1`.
- 이전 A3에서 했던 작업(orchestration + cache) 일부는 그대로 유지 (A3은 cache 통합 + chapter_types overwrite 책임만 남음).
- 효과: A2.5 1e clustering이 chapter_id 기준으로 같은 marker라도 다른 chapter면 분리 (chapter root 예외: cross-chapter 통합 허용).
- confidence: high (cache schema v7+에 paragraph.chapter_id 보존)

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

### A2.5 1e — AI structural canonicalization (chapter-aware) [active] **[2026-05-20 update]**
- AI tag: `hwpx_canonical_clustering` (+ `_repair` retry)
- 위치: `dbtool:871-986`
- 흐름: AI call → parse → issues 있으면 repair AI call → 둘 다 실패 시 `canonicalize_by_data` fallback
- 입력: paragraphs (with `chapter_id` from A2.2b Phase E inline). prompt(`build_canonical_clustering_prompt`, `:3108`)이 paragraph table에 `ch={chapter_id}` 컬럼 추가.
- **chapter-aware constraint** (`CANONICAL_CLUSTERING_PROMPT`, `:2969`):
  1. **자식 약화** (`cc5786f`): 부모 role 같으면 자식 종류·개수 차이만으로 split 금지. 부모 role 다르면 hard constraint로 다른 cluster.
  2. **chapter_id 분리** (`3346bf0`): chapter 내부 body paragraph는 같은 marker라도 chapter_id 다르면 다른 cluster.
  3. **chapter root 예외** (`cc5786f`): 각 chapter 첫 paragraph(level 0, 같은 부모 role, 같은 marker family)는 chapter_id 무관 통합 (chapter title role이 N개 cluster로 파편화되어 marker 부착이 깨지는 문제 방지).
- 출력:
  - `_role_registry` — actually consumed: **debug payload 전용**. `apply_structural_clustering`이 이미 `paragraphs[i].role`을 cluster_id로 mutate했기 때문에 `_role_registry` 자체는 main logic에서 안 읽음
  - `_1e_final_source`: "1e_original" / "1e_repaired" / "fallback_baseline"
- mutation: `apply_structural_clustering`이 `structure["paragraphs"][i].role`을 cluster_id로 교체
- audit note: 🚩 **"1e" 레이블 두 번 사용**. 여기는 AI canonical_clustering, A2.7은 code format_rules. 실행 순서: 1a → 1b → 1c → **Phase E inline (A2.2b)** → parent-correction → **1e AI canonicalization (chapter_id 입력)** → 1d code → **1e code format** → 1f marker policy. 번호와 실행 순서 불일치는 그대로.
- confidence: high

### A2.6 1d — `compute_exclusivity_rules_code` + `compute_sibling_cooccurrence_rules` [active, code only] **[2026-05-20 update — model 전환]**
- 위치: `hwpx_analyzer.py:7737` (exclusivity_rules), `:7880` (sibling_cooccurrence_rules) (호출 `dbtool:999-1018`)
- 입력: `_pc_data` (`compute_parent_instance_children_by_parent_idx` if hybrid_mode else `compute_parent_instance_children`)
- 출력:
  - `exclusive_rules` (legacy blacklist 모델, 양식에서 분리된 child set만 명시) — `structure["exclusive_rules"]`. 호환을 위해 유지.
  - `sibling_cooccurrence_rules` (**new white-list 모델**, `d4088a5`) — `structure["sibling_cooccurrence_rules"]`. default 배타 + 양식 관찰 공존 쌍만 예외. per-parent `child_set_variants` (`f8a6304`) 보유: `variants = [{variant_id, child_set, samples, instance_count}, ...]` (frozenset dedup).
  - **instance-aware** (`f05b773`): variant마다 양식 instance sample (marker + text + first_idx) 포함. AI가 instance ↔ variant 직관 매핑.
- 모델 전환 의도: 옛 "variant 다양화 권장" + "한 instance 안 안 섞기" 두 instruction 충돌 (예: cluster_11 instance 1개에 cluster_12+cluster_16 같이 박는 위반) → default 배타 + white-list로 명시.
- consumed by: A13 2b prompt (`build_section_fill_prompt`)에 `cooccurrence_rules` 인자로 전달 → cooccurrence section render. SECTION_FILL_PROMPT 룰 5번이 "한 instance 한 variant + 유동적 갯수" 강조.
- audit note: AI 대안 (`build_exclusivity_analysis_prompt`)이 import만 있고 호출 0 (Part B 참조)

### A2.7 1e (코드) — `compute_format_rules_code` [active, code only]
- 위치: `hwpx_analyzer.py:7827` (호출 `dbtool:1024-1041`)
- 입력: `compute_format_observations(structure, _section_light_xml, idx_map=_section_idx_map)`
- 출력: `format_rules`, `blank_rules` → `structure["format_rules"]`, `structure["blank_rules"]`
- audit note: 🚩 "AI 호출 폐기. 결정적·고속·무토큰." 주석. AI 대안 (`build_format_analysis_prompt`)이 import만 있고 호출 0 (Part B 참조)

### A2.8 1f — marker policy induction + **table_kind 분류** [active, AI] **[2026-05-20 update]**
- AI tag: `hwpx_1f_marker_policy`
- 위치: `:3850` (build) / `:3911` (parse) / `:3934` (verify). 호출 `dbtool:1042-1064`
- 입력: `paragraphs`, `_idx_texts`, **`light_xml`** (paragraph idx별 tbl 정보 — cell 수/cell 텍스트 — 첨부, `535e3a4`)
- 출력 `_marker_policy_1f` (dict {roles: [{role, marker_policy_status, evidence, verification, **table_kind**, **table_kind_reason**}]})
  - **`table_kind`** (`535e3a4`): `decorative_box` (텍스트 강조·박스·배너 용도, cell 텍스트가 paragraph 본문과 일치 또는 부분 분할) / `real_table` (행/열 독립 데이터, 매출/일정/비교) / `not_applicable` (해당 role sample에 tbl 없음). AI가 cell 텍스트와 paragraph 본문 의미 비교로 판단.
  - actually consumed: `structure["marker_policy_1f"]`, `section_results[sid]["marker_policy_1f"]`, cache, `extract_marker_policies` (12.1 path), **A18.10 outer fallback safety** (real_table만 skip; decorative_box는 통과 — 박스형 paragraph 누락 버그 해결)
  - 🚩 `_msgs_1f`, `_llm_1f` raw는 `_debug_payload`에 없음 (Agent 1 unresolved). confidence: medium
- audit note: ✅ 1f IS AI confirmed. `extract_marker_policies` (`:7490`)는 별도 함수로 12.1 marker roundtrip path에서만 사용. 변경 후 `extract_marker_policies`는 `table_kind`를 결과 dict에 포함.

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

## A3 — Phase E (TOC chapter planner) (`dbtool:1335-1488`) **[2026-05-20 update — 역할 부분 이동]**

**위상 변화** (`3346bf0`, 2026-05-18): Phase E의 핵심(`has_toc_gate` → AI plan → validate → `assign_chapter_ids_from_phase_e`)이 A2 loop 안 1c 직후로 이동(A2.2b 참조). A3 자리에 남은 책임은:
1. A2.2b가 이미 완료된 Phase E 결과의 cache replay (cache hit 시 AI skip)
2. one_c_diagnostic 매 run fresh recompute (Track D-2)
3. (A5에서) cache 통합 write-back + chapter_types overwrite

단, **DB tool 코드 구조 자체는 두 호출(A2.2b inline + A3 cache replay)을 모두 시도**할 수 있음 (한쪽이 cache hit이면 다른 쪽 skip). 다음 cleanup 후보.

### A3.1 `diagnose_1c_non_body_handling` [active]
- 위치: `:16694` (Track D-2)
- 동작: 1c가 non-body paragraph를 어떻게 다뤘는지 측정. CONTAINER(자식 OK) / LEAF(자식 wrong) 분리.
- 출력: `_pe_one_c_diag` → `_debug_payload["phase_e_chapter_planner"]["one_c_diagnostic"]`

### A3.2 multi-section guard [active]
- 위치: `dbtool:1353-1359`
- 2026-05-17 정책: multi-section template은 Phase E에서 section 0만 분석
- 출력: `_section_results_for_phase_e` (narrowed dict)

### A3.3 `has_toc_gate` [active]
- 위치: `:16128`
- 출력: `_pe_gate` (`has_toc, toc_paragraph_hints, detection_method, scanned_*`)
- 동작: role match (`table_of_contents`/`toc`) OR text regex (`차례`/`목차`/`Contents` 등 12개)

### A3.4 `build_toc_based_chapter_plan_prompt` [active]
- AI tag: `hwpx_phase_e_toc_based_chapter_plan`
- 위치: `:16390`
- 입력: `_pe_toc` + `_pe_body` + `_pe_tree`
- 🚩 [2026-05-20 update] prompt 후속 정리(`75f2c13`, `ca0b87b`, `04b0c2a`, `7a470d0`, `56a11ac`, `8812b92`, `6162067`, `93e1972` 등)로 TOC sub-list chapter vs subpattern 구분 원리, level binary choice(0 vs 1), 같은 level 일관성 절대 원칙, sibling group 단위 depth 선택 등 강화.

### A3.5 `parse_toc_based_chapter_plan_from_llm` + retry [active]
- 위치: `:16481`. max 1 retry. 실패 시 `status="ai_call_failed"`

### A3.6 `validate_toc_based_chapter_plan` [active]
- 위치: `:16505`. paragraph_ref 존재 체크 + 불일치 시 confidence low 강등

### A3.7 Phase E cache hit branch [active]
- 위치: `dbtool:1361-1371`
- 동작: `_cached_phase_e` 있으면 AI skip. one_c_diagnostic은 매번 fresh recompute
- 출력: `_phase_e_skipped_by_cache=True` flag (A5.1에서 사용)

### A3.8 `run_phase_e_chapter_planner` [dead-code]
- 위치: `:16868`
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

## A5b — Stage 11.2 + 11.2b (Sprint 1, 양식 분석 phase) **[2026-05-21 update — NEW stage]**

위치: Phase E + Track C PRODUCTION 전환 직후, A6(2a chapter_classify) 진입 전. cluster 확정된 후 cluster별 (a) 말투 rule + (b) inline 강조 layer 추출. **양식 분석만의 부산물** — 본문 생성 단계(A13 2b prompt + A18 assembly)에 입력. analyzer.py 미커밋 워크트리 dirty(production 배포됨).

### A5b.1 — 11.2 본체 (말투 rule, AI) [active]

- AI tag: `hwpx_style_profile` (10 cluster batch)
- 위치:
  - prompt 상수: `hwpx_analyzer.py:6221` (`STYLE_PROFILE_PROMPT`)
  - sample 수집: `:6275` (`_collect_style_samples`)
  - prompt build: `:6442` (`build_style_profile_prompt`, 10 cluster batch)
  - parse: `:6501` (`parse_style_profile_from_llm`)
- 입력:
  - `paragraphs` (cluster_id 부여 후, A2.5 + A2.2b)
  - `idx_full_texts`
  - `semantic_tags` (optional)
- 동작:
  1. cluster별 paragraph 전수 sample 수집 + 정규화 dedup
  2. 80K char budget 초과 시 forced (shortest + longest + tag별 1) + stratum stratified
  3. cluster 10개씩 batch → AI 호출 → role별 `content_style_rules_for_generation` + `additional_observations`
  4. rule마다 `when` / `when-not` + `[sN]` inline 인용 강제
- 출력 `style_profiles` (dict by cluster_id):
  ```
  {cluster_id: {"rules": ["rule1 ... [s0, s2]", ...], "observations": "자유 자연어 ... [sN]"}}
  ```
- 소비처: A13 2b prompt (`build_section_fill_prompt(style_profiles=_style_profiles, ...)`)
- audit note: ✅ 이전 audit B6 "dead-code (literal pass)" 표기는 **stale**. Sprint 1로 활성화됨. A19.3 옛 pass block은 잔존 가능 (확인 후 cleanup 후보).
- confidence: high

### A5b.2 — 11.2b emphasis (inline 강조 layer, AI) [active]

- AI tag: `hwpx_emphasis_layer` (10 cluster batch)
- 위치:
  - charPr 다양성 추출: `hwpx_analyzer.py:6584` (`extract_paragraph_emphasis_map`) — 양식 raw zip 직접 열어 tbl cell 안 paragraph 포함
  - 1a paragraph idx ↔ raw zip top-idx 매핑: `:14380` (`_build_1a_to_xml_p_idx_mapping`) — 13.7b 기존 helper 재사용 (text 정규화 substring matching)
  - prompt 상수: `:6774` (`EMPHASIS_LAYER_PROMPT`)
  - prompt build: `:6843` (`build_emphasis_layer_prompt`) — sample paragraph에 `[[em1]]...[[/em1]]` markup으로 layer 표시
  - parse: `:6903` (`parse_emphasis_layer_from_llm`)
- 입력: 양식 raw zip + `structure["paragraphs"]` (cluster_id) + `idx_full_texts`
- 동작:
  1. raw zip 직접 순회 → 각 paragraph descendant run 순회 (텍스트박스 cell 안 run **포함**)
  2. cluster별 charPr 다양성 측정 → 빈도순 em1/em2/... layer 부여
  3. multi-charpr paragraph만 sample 보존
  4. AI 호출 → cluster마다 `base_layer_id` + `emphasis_layers[]` (rule + 적용 조건 + 비적용 조건 + [sN])
- 출력 `emphasis_layers_by_cluster` (dict by cluster_id):
  ```
  {cluster_id: {
    "base_layer_id": "em3",
    "base_charpr_id": "154",
    "emphasis_layers": [
      {"layer_id": "em1", "charpr_id": "240", "rule": "..."},
      ...
    ]
  }}
  ```
- 소비처:
  - A13 2b prompt (`build_section_fill_prompt(emphasis_layers=..., paragraph_emphasis_map=...)`)
  - A17/A18 assembly (`assemble_hwpx_hybrid(emphasis_layers=...)`)
- audit note: 매핑 정확성 — `_build_1a_to_xml_p_idx_mapping`이 빈 text paragraph는 fallback (이전엔 sequential index로 잘못 매핑돼 cluster_19 같이 빈 1a paragraph 가진 cluster의 cell 안 charPr 못 잡았음, Sprint 1C fix)
- confidence: high

### A5b.3 — style namespace cache + ANALYSIS_ONLY_MODE early return [active]

- style cache 파일: `/tmp/hwpx_cache/<hash16>_style.json` (main `<hash16>.json`과 **별도 namespace**)
- 버전: `STYLE_CACHE_SCHEMA_VERSION = 2` (1→2 bump 시 Sprint 1 진행 중 cell run 포함 등 변경)
- 호출: `save_template_cache(cache_key, payload, namespace='style')` (`hwpx_analyzer.py:5218`), `load_template_cache(cache_key, namespace='style')` (`:5234`)
- 효과: main cache(CACHE_SCHEMA_VERSION=11)와 독립 invalidate — 11.2/11.2b 변경해도 main 1a~1f cache hit 유지
- early return: ANALYSIS_ONLY_MODE valve `on` 일 때 A5b.1+A5b.2 완료 + 1차 dump 후 2a 진입 전 `return None, _summary_msg` → outer `generate_document`가 `hwpx_bytes is None` 분기 처리 (debug_log만 반환, .hwpx 출력 X). 분석 phase 검증 전용 모드.
- audit note: 🚩 cache write 분산 — main cache 5개 mutation 위치(C1.6) + style cache 1개 mutation(A5b). 총 6번. main과 style 동기화 깨질 위험은 없으나(다른 namespace) cache file 위치는 동일 directory.
- confidence: high

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

## A11 — 13.7a-A1 chapter route prep (`dbtool:2055-2083`) **[2026-05-20 update]**

- chapter route 변수 초기화: `_chapter_objects=[]`, `_chapter_empty_reasons=[]`
- `_tup_regions/_tup_region_by_id/_tup_chapter_regions` 인덱싱
- `_chapter_plan_seed = extract_chapter_template_plan_seed(_tup, structure, _idx_full_texts)` (`:10050`) — 13.4b
- `_chapter_loop_driver`: `"template_plan"` if seed valid && confidence != low else `"2a_chapters"`
- `_broad_source = pdf_text_content or content_text or ""` (A12, A13, A14, A16 입력)
- **seed local_title_role fallback** (`2cbcc4f`, 2026-05-18): `extract_per_chapter_pattern`이 `_empty_chapter_pattern` (sub-tree 없음 — 표/일정 위주 마지막 chapter 등)으로 fallback할 때, chapter의 **첫 paragraph role을 `local_title_role`로 기본 채움**. 이전엔 빈 문자열로 버려져 13.7c adapted_title 처리에서 role 매칭 실패하던 케이스 해결.

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
- 위치: `:11791` (build) / `:12215` (parse)
- 입력: `_source_inventory`, `_ch_inputs_for_plan`, `broad_source_preview=_broad_source[:50000]` (task 1 fix)
  - **🚩 [2026-05-20 update] AI input title strip** (`92823df`): `_ch_inputs_for_plan[i].original_title`은 `strip_chapter_title_marker` (`:10208`)로 양식 1f marker_policy의 detected_marker prefix 제거 후 AI에 전달. AI는 title의 의미만 판단, 형식(marker)은 code 책임 (책임 분리). assembly 단계의 marker auto-prepend (A18.6b)와 짝.
- 출력: `_ap_parsed`:
  - `chapter_decisions` (list[decision])
  - `overall_source_focus` (dict | None) ← top-level
  - `_validation` (dict)

### A12.3 split path [active, **(a) fix done**] **[2026-05-20 update]**
- 위치: `dbtool:2168-2208`
- 트리거: `should_split_adaptation_batch(_ap_prompt_text)` (`:12511`)
- 임계값 변경 (`18e187b`, 2026-05-18): `model_context_char_budget=128000`, `safety_ratio=0.95` → 약 **121,600자** (이전 60,000자). gpt-5.4 128k context 활용. 작은~중간 양식(예: 조달청 3 chapter, ~60K prompt)은 **single batch 유지** → AI가 chapter set 한 관점으로 보고 `overall_source_focus` 정상 결정.
- 🚨 큰 양식(수십~수백 chapter)에서 여전히 split 발동 가능. split path가 `_ap_parsed = {"chapter_decisions": _all_decisions, "_validation": ...}` 로만 재구성 → `overall_source_focus` 누락 — **(b) fix 보류**:
  - (b) split path에서 chunk1 focus를 final로 저장 + ambiguity_flag (chunk별 다른 focus 가능성)

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

### A13.1 template-driven path [active] **[2026-05-20 update — prompt 강화] [2026-05-21 update — Sprint 2A + 3A 인자]**
- 조건: `not _shallow_done and _chapter_plan_seed`
- 위치: `dbtool:2293-2513`
- **2b prompt 강화** (`7f35fc0`, `81aceb4`, `e1723f3`, 2026-05-18):
  - **root role hard constraint** (`7f35fc0`): `parent_id=null`은 패턴 트리 최상위 role만. 자식 role을 root로 박는 거 금지. 양식 parent instance N개 필요하면 N개 생성, 자식을 root로 합치지 마.
  - **instance count hint** (`7f35fc0`): `build_section_fill_prompt` catalog 출력에 "양식 instance: N개" hint 추가 (count 필드 사용). cooccurrence section에 "양식 instance 수: N개 (생성 권장)" 표시. AI가 instance 갯수 의식하도록.
  - **chapter title 답습 금지** (`81aceb4`): adapted_title prepend로 AI가 첫 root sub-item에 chapter title 그대로 복제하던 버그 해결. root sub-item은 ch title보다 한 단계 좁고 구체적 sub-주제 작성.
  - **양식 specific 예시 제거** (`e1723f3`): 조달청 양식 specific 단어("추진성과 및 평가" 등) 제거 → 일반 가이드 + "양식 role 카탈로그의 sample text를 직접 참고"로 위임 (CLAUDE.md 하드코딩 금지 원칙).
  - **cooccurrence section format 전환** (`d4088a5`/`f8a6304`/`f05b773`): 옛 "공존 가능 쌍" → variant 단위. variant마다 양식 instance sample (marker + text). pattern_tree multi-variant parent 옆에 ⚠️ 경고 주석.
  - 사용자 명시 동의로 MEMORY '2b prompt 수정 X' 룰 부분 해제 (해당 root constraint / answer suppression 항목 한정).
- **Sprint 2A + 3A 인자 추가** (워크트리 dirty, 2026-05-20):
  - **`build_section_fill_prompt` 시그너처** (`hwpx_analyzer.py:15509`): 신규 인자 `style_profiles: dict | None = None`, `emphasis_layers: dict | None = None`, `paragraph_emphasis_map: dict | None = None`
  - **"role별 말투 가이드" 섹션** prompt 박음 (Sprint 2A) — A5b.1 결과 cluster별 rule + observations
  - **"강조 표시 — 양식 글꼴 분리 보존" 섹션** prompt 박음 (Sprint 3A, `SECTION_FILL_PROMPT:15263` 내부) — markup 형식 `[[emN]]...[[/emN]]`, base는 markup 안 함, cluster에 정의된 layer만 사용
  - **"role별 강조 layer 가이드" 섹션** prompt 박음 (Sprint 3A) — A5b.2 결과 cluster별 base_layer_id + 각 강조 layer rule
  - DB tool 4개 호출(template-driven + 2a-driven + section N 등)에 모두 `style_profiles=_style_profiles, emphasis_layers=_emphasis_layers_by_cluster` 전달
- per-chapter flow:
  1. `ch_title = adapted_title` (모든 action에서, 13.7c-2phase)
  2. local_pattern from `tpl_ch.get("local_pattern")` else seed_pattern (13.6-B)
  3. `_title_action`, `_content_action`, `_action = _title_action` (debug alias)
  4. **모든 chapter는 2b 호출** (`source_gap` 분기는 placeholder `if False`)
  5. `build_section_fill_prompt(..., pdf_text=_broad_source, cooccurrence_rules=structure["sibling_cooccurrence_rules"], style_profiles=_style_profiles, emphasis_layers=_emphasis_layers_by_cluster, paragraph_emphasis_map=_paragraph_emphasis_map, ...)` (`:15509`)
  6. **adaptation hint prepend** (13.7e v2): `_title_action in adapt_*` 면 첫 user message 앞에 hint block (adapted_title, original_title, actions, preserved/adapted_aspects[:3], supporting_evidence[:3])
  7. AI call (tag `hwpx_section_fill_{ch_idx}`) — AI 출력에 emphasis markup `[[emN]]...[[/emN]]` 포함 가능 (Sprint 3A)
  8. override grammar (13.6-B): `pattern_to_grammar(_ch_local_pattern)` (`:9972`)
  9. `process_section_fill_result(...)` (`:15360`) — 내부 흐름: `parse_section_fill_from_llm` (`:14867`) → `normalize_section_items` (`:14920`) → `validate_ai_parent_ids` (`:15091`) → `apply_parent_id_fallback` (`:15295`) → `reconstruct_tree_from_flat` + `validate_reconstruction`. **markup은 strip 안 함** — AI 출력 text 그대로 chapter_object 안에 들어가 A18.10 emphasis path에서 parse
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

### A17.5 assembly call [active] **[2026-05-21 update — Sprint 3D `emphasis_layers` 인자]**
```python
result = assemble_hwpx_hybrid(
    template_path, structure, content_data,
    removed_indices=removed_indices, idx_map=idx_map,
    content_only_mode=True,
    preserve_indices=_chapter_preserve,
    analyzed_sections=_analyzed_section_ids,
    chapter_local_exemplars=_chapter_local_exemplars,
    emphasis_layers=_emphasis_layers_by_cluster,  # Sprint 3D
)
```
- 시그너처: `hwp_generator.py:1089` (`emphasis_layers: dict | None = None`, line 1100). A5b.2의 cluster별 base/layer 매핑이 A18.10 body item path에서 charpr 매핑·markup parse에 사용.
- DB tool은 assemble 호출 2개(chapter route + shallow route 또는 동일 path에서 두 분기) 모두 emphasis_layers 전달.

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

### A18.5b 표지(cover) preserve [active] **[2026-05-20 update — NEW]**
- 위치: `hwp_generator.py:1806-1830` (`e7fab74`, 2026-05-18)
- 호출 시점: chapter_anchors 매칭 loop(A18.6) 직후
- 동작: `min(chapter_anchors[ci])` doc_idx 이전의 모든 `doc.paragraphs` index를 `header_indices`에 통째 추가
- 해결한 버그: `truncate_xml`이 token budget 위해 빈 paragraph 제거 → 1a paragraphs(예: 224)와 doc.paragraphs(예: 418) 매핑 깨짐 → `_is_skip`이 1a 기반이라 양식 표지의 빈 paragraph 못 잡음 → body remove에서 사라짐 (제목/날짜/기관 사이 spacing 빈 줄 12개 정도 누락)
- 데이터 흐름: chapter_anchors 매칭이 doc.paragraphs idx 직접 사용 → 그 이전을 preserve로 우회
- log: `[cover preserve] chapter 전 doc paragraph N개 추가 preserve (first_chapter_doc_idx=...)`

### A18.6 chapter_anchors 매칭 loop (`:1545-1764`) [active]
- **Priority 1**: `section_local_first_idx + section_id` → `_section_top_level_paragraphs[sid][idx]` + `_validate_anchor_signature`
- **Priority 2**: legacy `paragraph_indices[0] + idx_map` (same section만)
- **Priority 3**: text fallback `_find_anchor_in_section_by_text(title_text, sid)` (same section)
- **Priority 4**: `chapter_anchor_failures.append`, `placement_failure`
- **invariant**: anchor owning section ≠ chapter.section_id → CROSS_SECTION_BLEED hard fail (skip)
- **13.7d 2-phase adapted_title**: `_ad_text = _ch_obj._debug.adaptation_decision.adapted_title`. non-empty && ≠ anchor text → adapted text를 anchor element에 박음. action 무관 적용.
- **🚩 [2026-05-20 update] `_replace_text_in_paragraph_elem` has_t 보존** (`8d1e909`, hwp_generator.py:3210): cover table cell처럼 `runs[0]=ctrl(colPr) + runs[1]=text` 같은 paragraph에서 `runs[1:]` iterate 중 runs[1] (has_ctrl=False, has_tbl=False) 통째 제거하던 버그 fix. **has_t 체크 추가**해 t element 가진 run은 보존.
- **🚩 [2026-05-21 update] Sprint 2B marker/content 글꼴 분리 분기** (`hwp_generator.py:1742-1750`):
  - A18.6b의 `_marker_auto_applied`가 True (1f marker_policy로 marker 자동 부착됨) && `_ad_marker_text` non-empty → `_replace_text_in_paragraph_elem_split(_anchor_el, _ad_marker_text, _ad_content_text, NS)` (`:3260`). 양식 t element들 중 첫 글자 매칭으로 marker_t 선택, 가장 긴 t를 content_t로 → marker 글꼴(예: Ⅱ의 charPr=150)과 본문 글꼴(charPr=154)을 각자 t에 박아 양식 글꼴 보존.
  - 그 외(fallback) → 기존 `_replace_text_in_paragraph_elem(_anchor_el, _ad_text_with_marker, NS)` (`:1750`)로 합쳐서 박음.
  - 효과 (handoff_2026_05_20 양식 evidence): 조달청 Chapter Ⅱ의 paragraph cell 안 t[0]=charPr=150("Ⅱ"), t[1]=charPr=407("."), t[2]=charPr=154(본문) 구조에서 marker = "Ⅱ. " 첫 글자 "Ⅱ" → t[0] match → marker 박음. content_t = 가장 긴 t[2] (charPr=154) → adapted content 박음. t[1] 비움. **본문 글꼴 보존**.
- per-ci diag append → `/tmp/hwpx_debug/_d02_anchor_per_ci.jsonl` (`:1722`)

### A18.6b chapter title marker 자동 조립 [active] **[2026-05-20 update — NEW sub-stage]**
- 위치: `hwp_generator.py:1696-1725` (`b3ff4ca` + `92823df` UnboundLocal fix + `7acc9f1` sibling_index 1-based fix, 2026-05-18)
- 호출 시점: chapter_anchors loop 안, `_replace_text_in_paragraph_elem` 호출 직전
- 책임 분리 (AI/code):
  - AI: chapter title의 **의미** (adapted_title text — marker 없음)
  - code: chapter title의 **형식** (marker — 양식 1f marker_policy + ci 기준 sequence)
- 데이터 흐름:
  1. `_marker_policies = extract_marker_policies(paragraphs_info, marker_policy_1f=_marker_policy_1f)` (chapter_anchors loop 직전, `:1541-1543`)
  2. chapter title role(`_title_role_for_anchor`)의 policy lookup
  3. `strip_marker(_ad_text, role, policy)` → AI text에서 marker residual 제거 (있는 경우)
  4. `generate_expected_marker_normalized(role, policy, ci + 1)` (1-based sibling_index) → 양식 policy 기반 marker 생성 (예: Ⅰ/Ⅱ/Ⅲ)
  5. `reattach_marker(stripped, marker, separator)` → `_ad_text_with_marker`
  6. `_replace_text_in_paragraph_elem(_anchor_el, _ad_text_with_marker, NS)`
- policy_type='no_marker' → skip (AI text 그대로)
- policy 없음 → skip (graceful fallback)
- 효과: AI가 marker 안 넣어도 code가 정확히 sequence 부여. 다음 챕터(Ⅳ, Ⅴ, ...) 추가돼도 code가 자동 처리. body items와 같은 marker_separator path 재사용.
- log: `[13.7d marker auto-prepend ci={ci}] role=... policy=... marker='Ⅱ' sep=' ' '...' → '...'`

### A18.7 empty preserve 재계산
- `_chapter_proc["empty_preserve_indices"]` clear → `chapter_anchors[ci]` doc_idx로 재구성 (정확도 ↑)

### A18.8 unanalyzed section preserve
- `analyzed_sections` 외 section의 모든 paragraph preserve

### A18.9 body remove loop
- header_indices 외 paragraph 전부 remove. `_remove_per_section` 추적
- 🚩 `_residual_candidates`, `_preserved_per_section` (lines 1931-1966) — debug 목적 build만, downstream read 없음

### A18.10 body item insertion loop (`:2408-2846`) [active] **[2026-05-20 update — table_kind + blank region-aware] [2026-05-21 update — Sprint 3D 통합 path]**
- exemplar pick: role + `chapter_local_exemplars[ci]` (13.7b §4) > section N placeholder fallback > legacy fallback
- **outer fallback safety — table_kind aware** (`535e3a4`, 2026-05-18): exemplar에 `tbl` 있으면 무조건 skip이 아니라 **`_marker_policies[role].table_kind`로 분기**. `real_table`만 skip. `decorative_box` / `not_applicable` / missing은 통과. 양식의 박스형 paragraph (◈/[전략1]/과제 1 등 — paragraph 자기 안에 강조용 tbl 배너 자식 포함)가 누락되던 버그 해결.
- **marker rewrite** (content_only_mode=True): `strip_marker` → AI marker residual strip → `generate_expected_marker_normalized` + `reattach_marker` → `_rewrite_marker` safety net
- **blank line region-aware placement** (`b0e6ffc`, hwp_generator.py:2678-2719, 2026-05-18):
  - blank_rules + format_rules indent_parts 적용
  - blank 빈 줄도 body item과 같은 path로 `chapter_anchors[ci]` 뒤에 insert. cursor를 blank로 update → 다음 body item이 blank 뒤에 들어감.
  - 이전엔 `section_elem.append(deepcopy(blank_el))`로 section 끝에 몰림 → chapter 영역 밖 누락 (13.7d region-aware placement 도입 후 비대칭으로 발생한 회귀)
  - chapter 컨텍스트 없으면 (shallow route 등) → `section_elem.append` fallback
- **Sprint 3D 통합 body text path** (`hwp_generator.py:2782-2844`, 2026-05-20 워크트리 dirty):
  - **cluster emphasis lookup**: `_body_cluster_em = (emphasis_layers or {}).get(role) or {}`. cluster 정의된 layer가 있으면 `_body_charpr_map["base"] = base_charpr_id` + 각 layer의 charpr_id, `_body_valid_layers` set 구성.
  - **tbl_box인 경우 cell paragraph target** (`:2799-2811`): exemplar new_elem의 첫 tr → tc → subList → paragraph가 진짜 text 박을 target. 추가 cell paragraph는 remove.
  - **markup 0개 pre-parse 우회 로직** (`:2818-2820`): valid emphasis markup이 0개면 emphasis path를 우회해 Sprint 2B path로 falls through. 이유: markup 0개에 emphasis path 돌면 segments=[(None, 전체)]가 되어 cluster base charpr로 박혀 양식 본래 글꼴 잃음. 이 가드로 양식 글꼴 보존.
  - **3분기**:
    1. `_body_valid_layers && _has_valid_em` → `_replace_text_with_emphasis_segments(target_p, marker_with_indent, segments, charpr_map, NS)` (`:3401`). marker → 첫 bearing t (첫 글자 매칭), content_t의 run 위치에 segments별 새 run 분할 + 각 run의 charPrIDRef = `charpr_map[layer_id]` 또는 `charpr_map["base"]`. AI 환각 markup (정의 안 된 layer)은 `_parse_emphasis_markup`의 valid_layer_ids로 base 처리.
    2. `_body_marker_text && not _has_valid_em` → `_replace_text_in_paragraph_elem_split(target_p, marker_with_indent, content, NS)` (`:3260`). Sprint 2B 첫 글자 매칭으로 marker/content t 분리. 예: "과제 N" paragraph cell의 t[1]=charPr=240(파란 배경) + t[3]=charPr=128(본문) → marker "과제 N " 첫 글자 "과" → t[1] match → marker 박음(파란 배경 보존). content → 가장 긴 t[3] → 본문 박음.
    3. **no marker no emphasis** → 기존 path: tbl_box이면 `_set_cloned_element_text`, 아니면 `_replace_text_in_paragraph_elem(_body_target_p, ...)` (`:3210`).
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

### A19.3 11.2 style profile [moved to A5b — 옛 자리 cleanup 후보] **[2026-05-21 update]**
- 🚩 이전 audit에 "literal pass, Style profile AI calls disabled"라 표기 → **stale**. Sprint 1으로 11.2 본체가 활성화되면서 호출 위치가 **A5b.1 (Phase E + Track C transfer 직후)**로 이동. A19 자리에는 옛 pass block이 잔존할 가능성 (확인 후 cleanup 후보).
- `build_style_profile_prompt`/`parse_style_profile_from_llm` import + 호출 모두 **active**. A5b.1 참조.

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

## B6 — 11.2 Style Profile + 11.2b Emphasis [active] **[2026-05-21 update — Sprint 1로 활성화]**

- A5b.1 + A5b.2 참조. 이전 audit "dead-code (literal pass)" 표기는 stale — Sprint 1으로 활성화됨.
- 호출 위치: Phase E + Track C cache 통합 직후, ANALYSIS_ONLY_MODE early return 직전 (A5b.3)
- 옛 A19 자리의 pass block은 잔존 가능 → A19.3 참조, cleanup 후보

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

## C1 — Cache schema (namespace='full', `CACHE_SCHEMA_VERSION = 11`) **[2026-05-20 update] [2026-05-21 update — style namespace 추가]**

### C1.1 파일 위치 / 경로
- `TEMPLATE_CACHE_DIR = "/tmp/hwpx_cache"` (`hwpx_analyzer.py:5181`)
- namespace='full': `<DIR>/<hash16>.json` (suffix 없음) — 1a~1f + Phase E + Track C + 12.0 + target_unit_plan
- namespace='style': `<DIR>/<hash16>_style.json` — **Sprint 1 신규 namespace** (2026-05-20). 11.2 style_profiles + 11.2b emphasis_layers. `STYLE_CACHE_SCHEMA_VERSION = 2`. main과 독립 invalidate (11.2/11.2b prompt 변경 시 style만 무효화, 1a~1f는 hit 유지)
- namespace='step1ab': `<DIR>/<hash16>_step1ab.json` — 현재 write path 없음 (dead, A1.5)

### C1.2 버전 호환
- `cache_schema_version < 11` → load 시 None 반환 (info log only, warning 아님)
- 자동 invalidate. 영향 없는 변경(assemble logic 등)에서도 v bump 시 강제 재실행
- 5/18 → 5/20 사이 누적 bump:
  - v6→7 (`3346bf0`): paragraph.chapter_id 추가 (Phase E inline)
  - v7→8 (`d4088a5`): structure.sibling_cooccurrence_rules 추가 (white-list 모델)
  - v8→9 (`f8a6304`): per-parent child_set_variants 표현 (variant 단위)
  - v9→10 (`f05b773`): cooccurrence instance-aware + multi_variant_parents 경고 데이터
  - v10→11 (`cc5786f`): 1e prompt 자식 약화 + chapter root cross-chapter 통합 룰 (prompt 변경이지만 결과 다름 → invalidate)

### C1.3 Top-level keys (실측 cache 파일 인스펙션 + 코드 두 source 일치)
```
structure                       — main blob (1a~1f + Phase E mutation + grammar + target_unit_plan + template_unit_observation)
chapter_types                   — outer alias (structure.chapter_types 중복)
signals                         — _signals (compute_role_context_signals)
idx_texts                       — _idx_texts (≤80자)
idx_full_texts                  — _idx_full_texts (unlimited)
marker_policy_1f                — outer alias (structure.marker_policy_1f 중복) — entry에 `table_kind` 포함 (v11)
paragraph_count, table_count    — sanity check
template_file_id                — original file_id (hash fallback)
section_count                   — extract_all_sections_xml 길이
section_results                 — {sid: section_local_view}
phase_e_chapter_planner         — A5.1 write (v6+, conditional on phase E success)
chapter_pattern_family          — A5.1 write (v6+, conditional)
cache_schema_version            — int (현재 11)
```

### C1.4 `structure` 내부 keys
```
paragraphs                      — each item has chapter_id (v7+) from Phase E inline
tables, validator_issues
exclusive_rules                 — legacy blacklist (호환 유지)
sibling_cooccurrence_rules      — NEW (v8+) white-list model; per-parent child_set_variants
format_rules, blank_rules
marker_policy_1f                — entry per role has `table_kind` (v11)
chapter_types                   — Phase E 결과로 overwrite 가능 (A5.2)
template_grammar, role_text_types, per_type_role_semantics
target_unit_plan, template_unit_observation
```

### C1.5 `section_results[sid]` keys
```
structure, chapter_types, marker_policy_1f, signals, idx_texts, idx_full_texts
```

### C1.6 cache write 분산 (단일 cache 파일을 mutate하는 위치들)
- A2.12 incremental save (per-section loop) — `<hash16>.json`
- A5.1 Phase E + Track C 통합 write-back — `<hash16>.json`
- **A5b.3 style cache write** — `<hash16>_style.json` (별도 namespace, **2026-05-21 update**)
- A8.4 target_unit_plan write-back (legacy AI) — `<hash16>.json`
- A19.1 target_unit_plan 재write (cache miss 시) — `<hash16>.json`
- A19.4 template_unit_observation write-back — `<hash16>.json`

🚩 같은 main cache 파일을 5개 단계가 partial mutation. style cache는 1개 단계만 mutate. consumer가 어느 단계 결과 읽는지 헷갈릴 위험 (main만 해당). style은 namespace 분리로 격리.

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
12b_style_profile.json                ← Sprint 1 (A5b.1) 11.2 본체 — 말투 rule per cluster
12d_emphasis_layers.json              ← Sprint 1 (A5b.2) 11.2b — base/layer 판정 + charpr 매핑
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
- 🚩 `12_` 정확한 단일 슬롯은 여전히 비어있음 (과거 `12_template_unit_observation`이 `13_`으로 이동). Sprint 1으로 `12b_`(style_profile), `12d_`(emphasis_layers) 채워짐 — `12_`/`12a_`/`12c_`/`12e_`는 미사용. 그래도 숫자 numbering drift는 부분 해소.

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
- ~~`build_style_profile_prompt`/`parse_style_profile_from_llm`~~ — **[2026-05-21 update] 더 이상 dead 아님**. Sprint 1으로 A5b.1에서 활성화. 옛 A19.3 위치 pass block만 잔존 (cleanup 후보)
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

11. **A12.3 split path overall_source_focus drop** — 출력 시 `_ap_parsed`가 chapter_decisions만 합치고 root field 누락. **[2026-05-20 update] (a) fix (`18e187b`) 완료** — split 임계값 60K → 121.6K로 완화. 작은 양식은 single batch 유지로 우회. (b) split path 합치는 코드 fix는 큰 양식(수십+ chapter) 위해 유지 보류.
12. **`_section_n_si = _source_inventory` NameError-guarded reuse (A16.6)** — A12 안 돌면 fragile (chapter route + `_chapter_plan_seed` None)
13. **outer `truncated_xml`/`removed_indices`/`idx_map` dead-but-computed** (A1.2) — cache-miss path에서 per-section이 덮어씀
14. **[2026-05-20 update — NEW] truncate_xml이 빈 paragraph 제거 → 1a-doc 매핑 깨짐** — `e7fab74` cover preserve로 우회됨(A18.5b)만, 근본 원인은 `truncate_xml`이 token budget 위해 빈 paragraph 제거하면서 1a paragraphs와 doc.paragraphs 인덱스 매핑이 attestation 없이 단순화. 표지 외 위치에도 영향 가능. 별도 fix 후보.

### C4.5 Side effects on shared dirs

14. **`write_stage_debug_files`가 `/tmp/hwpx_debug/*.json` glob delete** (A19.5) — 외부 producer(A2.15 05d, A18 _d00~_d04)가 그 전에 쓰면 wipe. `.jsonl`은 살아남음 (의도적인지 검증 필요)
15. **`/tmp/hwpx_debug` numbering drift** — `12_` 빈 슬롯

### C4.6 데이터 흐름 손실 위험

16. **chapter_local_exemplars remap이 chapter ordering 가정** (A17.4) — A11 chapter ordering 변경 시 silently wrong index
17. **section 0 cache abort gate만** (A2.9) — multi-section에서 section 1~4 invalid silent
18. **PDF 50000자 silent truncate** (A1.3) — `pdf_to_text` 기본값. 13.7c는 `_broad_source[:50000]`로 명시했지만 PDF source 자체가 이미 한 번 잘림. 더 긴 source 미보호

### C4.7 두 데이터 source가 같은 정보를 producer로 갖는 패턴

19. **`extract_header_roles` analyzer 함수 vs DB tool inline** (A6.1) — MEMORY는 함수 호출이라 표기, 실제 dump는 inline. prompt builder는 양쪽 shape 수용 — 변경 시 두 곳 다 봐야 함
20. **`extract_marker_policies` (12.1) vs `marker_policy_1f` (1f)** — 둘 다 marker policy 정보 보유. 1f가 cache + structure, 12.1은 12.1 path만 — 일관성 검증 필요. **[2026-05-20 update]** assembly A18.6b chapter title marker auto-prepend는 `extract_marker_policies`를 호출 (line 1541-1543). table_kind field도 `extract_marker_policies` 출력에 포함되어야 함 (A18.10에서 사용) — 두 producer가 같은 데이터 모양 유지하는지 검증 필요.

### C4.8 [2026-05-20 update] 책임 분리 (AI vs code) 강화

21. **chapter title 책임 분리**: AI는 의미만 결정(`adapted_title` 텍스트), code는 형식(marker, sequence) 책임.
    - AI input strip: `strip_chapter_title_marker` (`:10208`) → adaptation_plan input에서 양식 marker 제거 (A12.2)
    - code marker auto-prepend: A18.6b에서 양식 1f marker_policy + ci 기준 sequence로 자동 부착
    - 다음 챕터 (Ⅳ, Ⅴ, ...) 추가돼도 code가 정확 sequence 부여
    - body items와 같은 `marker_separator` path 재사용으로 일관성 보장
22. **flow reorg 영향 (Phase E → 1c 후로 이동)**: DB tool 코드 구조가 두 위치(A2.2b inline + A3 cache replay)에서 Phase E 호출 시도 가능. cleanup 후보 — 한쪽으로 통합 필요.

### C4.9 [2026-05-21 update] Sprint 1+2+3 책임 분리 / 양식 글꼴 보존

23. **양식 분석 vs 본문 생성 phase 분리** (Sprint 1): 11.2/11.2b는 양식 한 번만 분석하면 충분 → main cache(1a~1f)와 독립된 style namespace cache. 11.2 prompt 변경 시 main cache invalidate 불필요 → 1a~1f 재실행 방지.
24. **AI 출력 emphasis는 텍스트 markup으로** (Sprint 3): AI는 양식 charPr ID를 모름 — 출력 텍스트에 `[[emN]]...[[/emN]]` markup만 표시. cluster별 layer ↔ charPr 매핑은 code가 책임(A5b.2의 charpr_map). 잘못된 layer는 `valid_layer_ids`로 무시.
25. **marker/content 글꼴 분리 — pre-extract 없음, 조립 시 직접 활용** (Sprint 2B): "마커/내용 글꼴 분리"용 별도 분석 stage 추가 X. `_replace_text_in_paragraph_elem_split`이 조립 시 양식 paragraph t element 직접 매칭으로 처리 (첫 글자 매칭으로 marker_t 선택, 가장 긴 t를 content_t로). 양식 텍스트박스 cell t[0]이 공백인 case 해결.
26. **markup 0개 가드 — emphasis path 우회로 양식 글꼴 보존** (Sprint 3D): AI가 markup 0개 출력하면 cluster base charpr로 전체 박혀 양식 본래 글꼴 손실 위험. body item path(`:2818-2820`)에서 valid markup 0개면 emphasis path 우회 → Sprint 2B path로 fall through → 양식 본래 t별 글꼴 보존.

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
- `_pipeline_audit_part_a3_a10.md` — Agent 2 원본 (A3~A10 상세, ~560줄) — **note: A3 Phase E가 이후 1c 후로 이동**(`3346bf0`). 이 원본은 옛 위치 기준.
- `_pipeline_audit_part_a11_a19.md` — Agent 3 원본 (A11~A19 상세, ~450줄)
- `_pipeline_audit_part_b_c.md` — Part B + C 초기 draft
- `pipeline_audit_2026_05_11.md` — 이전 audit (2026-05-11), 13.7c 이전 시점
- `client_presentation_hwpx_pipeline.md` — 클라이언트 발표용 요약 (2026-05-11)

본 문서는 위 4개 source를 합쳐서 cross-reference + audit findings 통합한 것. 2026-05-20 refresh로 17개 추가 commit (5/18 → 5/20) 반영.

### 다음 refresh 시점 trigger

- Phase E inline cleanup (A2.2b + A3 둘 다 호출하는 구조 통합)
- split path overall_source_focus fix (b)
- truncate_xml 빈 paragraph 제거 → 1a-doc 매핑 근본 fix (cover preserve 우회 의존 해제)
- Track D-1 1c prompt 개선 (cache invalidate 비용)
- 새 양식 추가 검증 (production 전환 기준)
- **A19.3 옛 11.2 pass block 정리** (Sprint 1 이후 잔존, 확인 후 cleanup)
- **Sprint 1+2+3 git commit** (현재 워크트리 dirty, production은 cp 배포로 사용 중) — audit refresh와 함께 묶어서 커밋
- **emphasis layer noise 정리 검토** (Sprint 4 후보): 11.2b가 cluster당 50+ layer 만드는 case (구조 기호/공백 등). 사용자는 "AI 판단 위임"으로 결정했지만 2b prompt에 너무 많은 layer 들어가면 focus dilution 가능. monitor 후 noise filter 도입 여부 결정.
- **다른 양식 (민원인 / CC7) Sprint 1+2+3 검증** — 조달청만 검증됨. 다른 cluster pattern에서 회귀/edge case 발생 시 fix
- **◈ 위치 잘못 회귀 fix** (사용자 미룬 항목): 2장 "1 문제정책 관리제도 운영여건" 바로 밑에 ◈ 와야 하는데 다른 위치 — 이전 fix 후 회귀
