# Pipeline Audit — Part A1 + A2

Audit window: DB tool (`/tmp/dbtool_dump.py`) lines 343 ~ 1334 and supporting helpers in `app/backend/open_webui/utils/hwpx_analyzer.py`. Documents actual runtime behavior, not the design intent.

---

## Part A1 — setup / cache load (dbtool lines 343 ~ 520)

### A1.1 analyze_hwpx [active]
- location: `hwpx_analyzer.py:193` (called at `dbtool_dump.py:355`)
- when called: Step 1, always — first thing after file resolution.
- purpose: Read section0.xml from the HWPX zipfile, lighten it (drop render-only tags), return XML + count metadata.
- inputs:
  - `template_path` (str): producer = `Storage.get_file(template_file.path)` at `dbtool:351`. Type: filesystem path.
- outputs:
  - `analysis["light_xml"]` (str) — consumed: yes, at `dbtool:356` → `light_xml`, then passed to `truncate_xml`.
  - `analysis["original_xml"]` (str) — actually consumed: **no** in A1/A2 path (only returned). confidence: high.
  - `analysis["paragraph_count"]` (int) — consumed: yes, at `dbtool:363` (log) and `dbtool:1117` (cache save).
  - `analysis["table_count"]` (int) — consumed: yes, at `dbtool:363` (log) and `dbtool:1118` (cache save).
- cache/debug: no direct cache. Logged via `_debug_add("Step 1: 양식 분석 + 축소", ...)` at `dbtool:367`.
- mutation/side effects: none in `structure`. Only reads.
- failure/fallback path: file open failures bubble up as Python exceptions; `ValueError` raised earlier at `dbtool:350` if template_file is None.
- audit note: only operates on the FIRST section (`extract_section_xml`, not `extract_all_sections_xml`). multi-section work is delegated to A2 loop. The metadata counts here only cover section0.
- confidence: high

### A1.2 truncate_xml [active]
- location: `hwpx_analyzer.py:884` (called at `dbtool_dump.py:357`)
- when called: Right after analyze_hwpx, always.
- purpose: Shrink light XML below ~100kB while preserving structure (truncates cell text, dedupes 1×1 textboxes, etc.). Renumbers `_idx` and returns mapping.
- inputs:
  - `light_xml` (str): from A1.1
  - `max_chars` default 100000
- outputs:
  - `truncate_result["xml"]` → `truncated_xml` (str) — consumed: yes at `dbtool:358`, then `dbtool:1155` (`len(_section_truncated_xml)` for debug). Note: at A1 level the **outer** `truncated_xml` is overwritten by per-section `_section_truncated_xml` inside the A2 loop (line 653). The outer one is effectively only used by the `_from_cache=True` debug branch (line 1299).
  - `truncate_result["removed_indices"]` → `removed_indices` (list) — consumed: only logged (`dbtool:1156`, 1300). actually consumed downstream: **no**. confidence: high.
  - `truncate_result["idx_map"]` → `idx_map` (dict {ai_idx → real_idx}) — consumed: actually used **per-section** at A2 (`_section_idx_map` used by `compute_format_observations`). The OUTER `idx_map` from A1 is never read again. confidence: high.
- cache/debug: included in `_debug_payload["xml"]` for size reporting.
- mutation/side effects: none.
- failure/fallback path: parsing exceptions would bubble; truncation has identity-map fast path when `len(light_xml) <= max_chars`.
- audit note: The outer `truncated_xml`/`removed_indices`/`idx_map` returned here are essentially **dead** in the cache-miss path because the A2 loop re-runs lighten/truncate per section starting at `dbtool:651-655`. They are still computed because the cache-hit branch (`dbtool:1296-1301`) uses the outer XML size for debug, and `_debug_add` at line 367 prints a snippet of `truncated_xml`.
- confidence: high

### A1.3 PDF / content load (pdf_to_text → pdf_text_content) [active]
- location: `dbtool:376-398`. `pdf_to_text` is `hwpx_analyzer.py:1120`.
- when called: Step 1 immediately after analyze_hwpx, if `content_file_id` is set.
- purpose: Extract plain text from the source PDF (via subprocess `pdftotext -layout`) up to 50000 chars (function default). Fallback to `content_file.data["content"]` text if PDF extraction returns nothing.
- inputs:
  - `content_file_id` (str) → resolved to `content_file` (`Files.get_file_by_id`) and `content_path` (`Storage.get_file`).
  - `content_text` (str): function parameter, may be passed in.
- outputs:
  - `pdf_text_content` (str) — consumed: yes downstream in A3+ (source intake / chapter split). At A1/A2 level: only the existence-check at `dbtool:397` (`raise ValueError if all three sources empty`).
  - `content_text` (str) — same as above.
  - `content_images` — explicitly forced to `None` at `dbtool:392-393` ("이미지 추출 제거 — 텍스트만 사용 (토큰 절약)"). The `content_images is None` branch at line 394 is now always-true when PDF path runs.
- cache/debug: not cached at A1.
- mutation/side effects: subprocess call out to `pdftotext`. Reads file from Storage.
- failure/fallback path:
  - PDF extraction exception → `log.warning("PDF 텍스트 추출 실패: ...")`, `pdf_text_content` stays "".
  - All three sources empty → `raise ValueError("작성할 내용이 없습니다.")` at `dbtool:398`.
- audit note: `pdf_to_text` truncates at 50000 chars and appends "... (총 N자 중 M자만 포함)" tail. This silently caps long source PDFs — note for A3+ source intake. The `content_images=None` assignment makes the multimodal branch dead-code (legacy from when images were passed).
- confidence: high

### A1.4 compute_template_hash → _cache_key [active]
- location: `hwpx_analyzer.py:5052` (called `dbtool:410`)
- when called: Always, before cache lookup.
- purpose: Compute 16-hex-char SHA256 prefix of the template file bytes. Used as cache key (resilient to re-upload because content-based, not file_id-based).
- inputs:
  - `template_path` (str): from A1.1
- outputs:
  - `_cache_key` (str, 16 hex chars) — consumed: yes, used at `dbtool:454, 461, 1110-1122, 1133, 1135, 1294-1295` and in `_TEMPLATE_CORE_CASES.get(_cache_key, ...)` at line 774 (hybrid_mode debug-only).
- cache/debug: included in `_debug_payload["cache_key"]`.
- mutation/side effects: none.
- failure/fallback path: try/except at `dbtool:411`. On failure → `_cache_key = template_file_id` (fallback). Logged as warning.
- audit note: the hardcoded `_TEMPLATE_CORE_CASES = {"34fce805c7cbccc0": {...}}` at `dbtool:761-773` is a debug-only answer key tied to this hash format. dead for any other template.
- confidence: high

### A1.5 _step_cache_path / _load_step_cache / _save_step_cache [legacy]
- location: `dbtool:417-431`
- when called: Defined inside the entry function but **never actually called** in the audited A1/A2 path. confidence: high (grep shows zero call-sites).
- purpose: Originally a per-step (1a/1b/...) JSON cache helper for the `step1ab` namespace experiment. Replaced by hybrid_mode flag and full-namespace cache.
- inputs: `step_name` (str)
- outputs: cache file paths and dict
- audit note: dead code. Note: also the `step1ab` namespace mentioned in comments (line 403, 1338 etc.) has no remaining write path in A1/A2.
- confidence: high

### A1.6 hybrid_mode / canonical_mode valves [active — but hybrid path is debug-only]
- location: `dbtool:433-437`
- purpose: Read `self.valves.HYBRID_MEASUREMENT` ("on"/"off") and `self.valves.CANONICAL_FALLBACK_MODE` ("on"/"report_only"/"off").
- inputs: valve settings
- outputs:
  - `hybrid_mode` (bool) — consumed: yes, gates the `parent_hint_measurement` block at `dbtool:746-835` and the parent-first switchover at `dbtool:845-853`. Also chooses `_pc_data` source at line 1005-1008.
  - `canonical_mode` (str) — consumed: yes, passed to `merge_levels_into_structure(... canonical_mode=canonical_mode)` at `dbtool:686`.
- audit note: per MEMORY.md "HYBRID_MEASUREMENT valve: off로 변경됨 (full 캐시 사용)" — `hybrid_mode` is currently off in production. The entire `if hybrid_mode:` block at 746-835 is debug-only. The `_hint_tree_paras → structure["paragraphs"]` parent-first switchover at 845-853 ONLY runs when hybrid_mode is on, so default path uses stack-based parent tree.
- confidence: high

### A1.7 extract_all_sections_xml pre-check → _actual_section_count [active]
- location: `hwpx_analyzer.py:89` (called `dbtool:445`)
- when called: Always, before cache load (B2.1.2b cache integrity check).
- purpose: Count actual section XMLs in the HWPX zip to detect stale cache.
- inputs:
  - `template_path` (str)
- outputs:
  - `_actual_section_count` (int) — consumed: yes at `dbtool:462` for cache section_count mismatch invalidation.
- failure/fallback path: try/except wraps it; on exception → `_actual_section_count = 0` + warning log. With value 0, mismatch check at 462 will likely fire and invalidate cache.
- audit note: Called TWICE in the run when cache miss — once here as a pre-check (`_extract_all_sections_xml_pre`) and again at `dbtool:631` inside the cache-miss branch (`_extract_all_sections_xml`). Both call the same function under different import aliases.
- confidence: high

### A1.8 load_template_cache(namespace='full') → _cached [active]
- location: `hwpx_analyzer.py:5099` (called `dbtool:454`)
- when called: Only if `hybrid_mode == False`. With hybrid_mode on, `_cached` stays None and the entire AI pipeline re-runs.
- purpose: Read JSON cache at `/tmp/hwpx_cache/<key>.json`, drop if `cache_schema_version < CACHE_SCHEMA_VERSION` (currently 6, defined at `hwpx_analyzer.py:5049`).
- inputs:
  - `_cache_key` (str)
  - `namespace` = 'full'
- outputs:
  - `_cached` (dict or None) — consumed: yes at `dbtool:456 (set _from_cache flag), 461-468 (section_count check), 481-502 (load all cached fields)`.
- audit note: `CACHE_SCHEMA_VERSION = 6` (Phase E v6+). Lower versions silently treated as miss (log info only, no warning).
- confidence: high

### A1.9 section_count mismatch invalidation [active]
- location: `dbtool:460-468`
- when called: only when `_from_cache` initially True (i.e., cache loaded successfully).
- purpose: B2.1.2b safeguard — if cached `section_count` ≠ actual section count, treat cache as miss to avoid stale-state bugs after template re-cut.
- inputs:
  - `_cached["section_count"]` (int)
  - `_actual_section_count` (int) from A1.7
- outputs:
  - sets `_cached = None`, `_from_cache = False` if mismatch. Logged as warning.
- audit note: legitimate self-defense check. Only counts schema-level mismatch; does not verify per-section content.
- confidence: high

### A1.10 section_results dict init [active]
- location: `dbtool:477` (`section_results: dict = {}`)
- when called: Always, regardless of cache hit/miss. Will be populated by either the cache-load branch (`dbtool:492 — section_results = _cached.get("section_results", {})`) or the A2 loop body (`dbtool:1095`).
- purpose: Section-local result container per 13.7b B2.1.1. Keyed by `section_id` (int).
- inputs/outputs: see A2 schema below.
- mutation: written at `dbtool:492` (cache hit), `dbtool:1095` (A2 loop), `dbtool:1616-1618` (Phase E chapter_types overwrite), `dbtool:1843-1888` (target_unit_plan sync).
- audit note: cache hit branch at `dbtool:481-502` ALSO sets `structure = _cached["structure"]`, `chapter_types = _cached.get("chapter_types", {})` (line 484-485) AS WELL AS attaching section_results. The outer `structure` is effectively a backward-compat alias to `section_results[0]["structure"]`.
- confidence: high

### A1.11 Cache-hit shortcut path [active]
- location: `dbtool:481-502`
- purpose: When `_from_cache` is True, skip 1a~1f and Phase E (if cached) entirely.
- inputs from `_cached`:
  - `_cached["structure"]` → `structure`
  - `_cached.get("chapter_types", {})` → `chapter_types`
  - `_cached.get("signals", {})` → `_signals_cache` (later assigned to `_signals` at line 1288)
  - `_cached.get("idx_texts", {})` → `_idx_texts_cache` → `_idx_texts`
  - `_cached.get("marker_policy_1f")` → `_marker_policy_1f_cache`; if truthy, also re-injected as `structure["marker_policy_1f"]`
  - `_cached.get("section_results", {})` → `section_results`
  - `_cached.get("phase_e_chapter_planner")` → `_cached_phase_e`
  - `_cached.get("chapter_pattern_family")` → `_cached_track_c` (Track C, debug-only)
- outputs:
  - placeholder vars set: `messages_1=[]`, `llm_content_1="[FROM CACHE]"`, `messages_level=[]`, `llm_content_level="[FROM CACHE]"`, `level_map={}` for downstream debug payload uniformity.
- audit note: cache hit also skips the A2 loop entirely; the variable `_section_cache_validations` is therefore NOT defined in the cache-hit branch. The downstream `section_results_debug` builder at `dbtool:1314-1332` defensively guards via `dir()` check at line 1315.
- confidence: high

---

## Part A2 — per-section 1a~1f loop (dbtool lines 471 ~ 1334)

Note: A2 is the cache-MISS branch (`if not _from_cache:` at `dbtool:616`). The loop runs for ALL sections (`[:1]` gate was removed per comment at line 645). For multi-section templates, all sections are analyzed but `section_results[0]` is the only one used downstream (per Phase E policy at `dbtool:1350-1357`).

### A2.0 Section enumeration setup [active]
- location: `dbtool:627-655`
- purpose: For each cache-miss run: enumerate sections via `extract_all_sections_xml` and per-section run `lighten_xml` + `truncate_xml`.
- inputs:
  - `template_path` (str)
- outputs:
  - `_all_sections` (list[(name, xml)]) — from `_extract_all_sections_xml(template_path)`
  - `sections_to_analyze` (list[(idx, name, xml)]) — enumerated tuples
  - per-iter: `_section_light_xml`, `_section_truncated_xml`, `_section_removed_indices`, `_section_idx_map`
- audit note: `_section_light_xml` is **per-section** light XML. It is what the A2.1e_code (`compute_format_observations`) actually sees. The outer A1.1 `light_xml` (section0 only) is unused in A2.
- confidence: high

### A2.1 Per-section 1a + 1b — `_do_step1a_1b(_section_truncated_xml)` [active]
- location: closure defined at `dbtool:516-614`, called at `dbtool:658`.
- when called: Every section iteration.
- purpose: Two AI calls bundled — (1) structure analysis (`hwpx_structure_analysis`), (2) role classification (`hwpx_1b_role_candidates`).

#### A2.1a build_structure_analysis_prompt + parse_structure_from_llm
- location: `hwpx_analyzer.py:1967` and `:4995`.
- purpose: LLM converts compact-text serialization of light XML → JSON `{paragraphs: [...], tables: [...]}`. Each paragraph carries `idx`, `marker`, `description` (role from AI but stored under `role` key) — NO `level`, NO `paraPrIDRef`/`charPrIDRef` in AI output (those are stripped from prompt and re-injected from code).
- inputs:
  - `_section_truncated_xml` (str)
- outputs:
  - `messages_1` (list[dict]) — consumed: yes, stored in `section0._debug_payload["llm_raw_response"]` (line 1137) implicitly via prompt build, and explicitly in `1c_structure_global.prompt_messages` placement is wrong; actually `messages_1` is not stored as prompt in the debug payload directly — only `llm_content_1` is at line 1137.
  - `llm_content_1` (str) — consumed: yes at `dbtool:1137` (`llm_raw_response`), also parsed back into `data_before` for split_log.
  - `structure_l` (dict) — consumed: yes, mutated and returned as `structure_after_1b`.
  - `_paragraph_styles` (dict or None) — consumed: yes at `dbtool:531-537` for paraPrIDRef/charPrIDRef code re-injection.
- cache/debug: `messages_1` packed into the closure return dict but only `llm_content_1` ends up in `_debug_payload["llm_raw_response"]`. confidence: medium — full debug for prompts may be elsewhere.
- mutation/side effects: `structure_l["paragraphs"][i]["paraPrIDRef"]` and `["charPrIDRef"]` are SET from `_paragraph_styles` if missing (setdefault at 536-537).
- failure/fallback path: `parse_structure_from_llm` raises ValueError on missing/unparseable JSON. No fallback inside `_do_step1a_1b`. JSON repair attempted via `_repair_json`.
- audit note: `data_before` is re-parsed from raw `llm_content_1` (lines 539-548) using regex; the only reason is to capture `before_role` for `split_log_l` (auto-split tracking). If AI auto-split a role, split_log shows {idx, marker, marker_type, before_role, after_role}.
- confidence: high

#### A2.1b post-1a code work — paraPrIDRef/charPrIDRef + marker_norm + idx_texts + signals_pre
- location: `dbtool:531-592`
- purpose:
  - 531-537: re-inject `paraPrIDRef`/`charPrIDRef` from `_paragraph_styles` (compact serializer's side data) using `setdefault`.
  - 539-565: replay raw JSON to build `split_log_l` and `before_by_idx`.
  - 566-577: build `marker_norm_l` = `{role: {marker_type: [markers]}}` mapping from pre-split paragraphs.
  - 579-585: build `idx_texts_l` (max_chars=80) and `idx_full_texts_l` (max_chars=None) via `_extract_texts_by_idx` over `_section_truncated_xml`.
  - 587: `structure_l["paragraphs"] = compute_paragraph_features(...)` enrichment (adds `marker_family`, `prev_marker`, `next_marker`, `prev_marker_family`, `next_marker_family`, `same_paraPr_run`).
  - 589-592: build `signals_pre_l = {"paragraph_texts": [...]}` for 1b prompt input.
- inputs:
  - `_paragraph_styles` (dict) — from compact serializer in build_structure_analysis_prompt.
  - `structure_l` (mutable dict)
  - `_section_truncated_xml` (str)
- outputs (all stored as keys in `_r1ab` closure return):
  - `paragraphs_before` (list) — consumed: debug at `dbtool:1139` (`structure_before_split`).
  - `split_log` (list[dict]) — consumed: debug at `dbtool:1150`.
  - `marker_norm` (dict) — consumed: debug at `dbtool:1151` and per MEMORY.md note "marker_norm 등 reference — 이후 section iteration에서 변수 덮어쓰기되어도 dict 안 reference는 section0 시점 값 유지". Otherwise not consumed downstream by code logic. confidence: medium.
  - `idx_texts` (dict {idx: str ≤80 chars}) — consumed: yes; goes into `section_results[sid]["idx_texts"]` (line 1100), used by `compute_role_context_signals(... idx_texts=)` at line 992, used by `build_marker_policy_prompt` and `verify_marker_policy_evidence` at 1054-1057.
  - `idx_full_texts` (dict {idx: str unlimited}) — consumed: stored in section_results[sid] (line 1101). Downstream used by Phase E (`_sr.get("idx_full_texts")` at line 1388).
  - `signals_pre` (dict) — consumed: yes, passed as `signals=_signals_pre` to `build_level_analysis_prompt` at line 683. Only carries `paragraph_texts` shape.
- mutation/side effects: `structure_l["paragraphs"]` is replaced (line 587) with the feature-enriched version.
- audit note: `signals_pre_l` carries ONLY `paragraph_texts`, not the full `compute_role_context_signals` shape. The full signals are computed later at `dbtool:991-993` after 1e cluster_id apply. So 1c sees minimal signals, 1f sees the full signals indirectly via `_signals` not actually passed to it (1f only gets paragraphs + idx_texts).
- confidence: high

#### A2.1c build_role_classification_prompt + parse_role_classification_from_llm + merge_roles_into_structure → role_candidates [active]
- location: `hwpx_analyzer.py:4044`, `:4116`, `:4186`. Called `dbtool:596-599`.
- purpose: AI assigns 1-3 role candidates per paragraph with scores. Code then merges back into structure (each p gains `role_candidates` field; `role` set to top candidate as placeholder).
- inputs:
  - `structure_l` (dict, paragraphs feature-enriched)
  - `signals=_signals_pre_l` (dict, paragraph_texts)
- outputs:
  - `messages_role` (list) — consumed: yes at `dbtool:1159` (`_debug_payload["1b_role_candidates"]["prompt_messages"]`).
  - `content_role` (str) → `llm_content_role` — consumed: yes at `dbtool:1160`.
  - `role_candidates_l` (dict {idx: [{role, score, reason}]}) — consumed: yes via merge into structure AND stored standalone at `dbtool:1161, 893, 940`. Used by 1e clustering prompt (line 893) and 1e repair prompt (line 940).
  - merged structure has each p with `role_candidates` and `role` (placeholder).
- mutation/side effects: `structure_l.paragraphs[i].role` and `.role_candidates` are set.
- failure/fallback path: `parse_role_classification_from_llm` raises on bad JSON. Legacy "roles" key supported (line 4144-4151 in analyzer). Empty candidates → p["role"]="" (line 4207).
- audit note: comment in `parse_role_classification_from_llm` says "1c" but called from 1b stage in the DB tool — naming is inverted vs comments because of legacy step ordering. The function docstring labels it "1c (AI 1)" while DB tool labels the step "1b". Confusing but stable.
- confidence: high

### A2.2 Per-section 1c — level + selected_index (AI 2) [active]
- location: dbtool:683-687; analyzer `hwpx_analyzer.py:2024 (build), :2107 (parse), :2230 (merge)`.
- when called: Right after `_do_step1a_1b` returns, every section.
- purpose: AI 2 assigns final `level`, `selected_role_candidate_index` (chooses among 1b candidates), and (hybrid only) `parent_hint_idx` + `confidence` + `parent_hint_reason_code`. Code then merges and computes `parent_idx`/`sibling_group_id` algorithmically.
- inputs:
  - `structure` (with role_candidates + features)
  - `signals=_signals_pre`
  - `hybrid=hybrid_mode` (bool)
- outputs:
  - `messages_level` (list) — consumed: `_debug_payload["1c_structure_global"]["prompt_messages"]` at line 1164.
  - `llm_content_level` (str) — consumed: same block, line 1165.
  - `level_parsed` (dict) with keys:
    - `decisions` (dict {idx: {level, parent_idx, sibling_group_id, selected_index, selection_reason_code, legacy_final_role, +hybrid: parent_hint_idx, confidence, parent_hint_reason_code}}) — consumed: yes at line 1167 (debug), 754, 780, 894, 941 (hybrid branches + 1e prompts).
    - `level_map` (dict {idx: level}) — consumed: line 687 (`level_map = level_parsed.get("level_map", {})`) and 1166 (debug).
  - `structure` after `merge_levels_into_structure`: paragraphs gain `level`, `semantic_role`, `canonical_role`, `structure_role`, `role` (replaced), `parent_idx`, `sibling_group_id`. Also `validator_issues` if `_validate_and_split` flagged anything.
- mutation/side effects: heavy paragraph mutation inside merge:
  - validator can fallback `selected_role_candidate_index` to 0 (line 2291-2300 of analyzer) — `selection_fallback_reason` field added.
  - `canonical_role` is now just `semantic_role` (line 2329 — `_FAMILY_DEFAULT_CANONICAL` override disabled; comment says "raw semantic_role 그대로 보존").
  - `structure_role = f"{family_label}__{sem_role}"` if marker family present.
  - `compute_parent_and_sibling_from_levels` reassigns `parent_idx` and `sibling_group_id` based on level sequence (ignores AI's parent_idx claim per comment at line 2281).
- failure/fallback path:
  - JSON parse failure → ValueError from `parse_level_from_llm`.
  - Legacy `levels` key supported (line 2140-2149).
  - `_validate_selected_index` enforces score ≥ 0.50, score_diff ≤ 0.20, non-empty reason_code; fails → fallback to index 0.
- cache/debug: stored in `_debug_payload["1c_structure_global"]` (lines 1163-1169) and `_debug_payload["level_analysis"]` (legacy compat at 1170-1173).
- audit note: comment at line 2281 of analyzer: "parent_idx, sibling_group_id는 코드가 계산 (1c가 줘도 무시)". So even if AI returns parent_idx, code overwrites.
- confidence: high

### A2.3 hybrid_mode block [debug-only]
- location: `dbtool:729-835`
- when called: only if `self.valves.HYBRID_MEASUREMENT == "on"` (currently off per MEMORY.md).
- purpose: Parent-hint reliability measurement. Builds parallel `_hint_tree_paras` via `build_hint_tree(...)`, compares with stack tree, runs core_cases against hardcoded answer key.
- inputs:
  - `level_parsed["decisions"]` (with parent_hint_idx populated only in hybrid)
  - `_paras_now = structure.get("paragraphs", [])`
  - `_TEMPLATE_CORE_CASES[_cache_key]` (hardcoded dict at lines 761-773, only `34fce805c7cbccc0` filled in)
- outputs (all debug-only, none consumed by downstream logic):
  - `_hint_validation`, `_hint_conflicts`, `_hint_override_paras`, `_hint_tree_paras`, `_tree_diff`, `_pc_hint`, `_excl_hint`, `_chapter_types_hint`, `_stack_inconsistency`, `_pf_inconsistency`, `_pc_stack_by_pidx`, `_excl_stack_by_pidx`, `_core_cases`
  - all packed into `_debug_payload["parent_hint_measurement"]` (lines 1187-1222).
- mutation/side effects: **HIGH-IMPACT** if hybrid_mode is on — line 845-847 replaces `structure["paragraphs"] = _hint_tree_paras`, meaning downstream (1d, chapter_types, 2a, 2b, assemble) uses hint_tree instead of stack tree. Also line 846 stores `_stack_post_correction_paras` for debug.
- audit note: This is the **parent-first transition** path. With hybrid_mode off, it never runs and the stack tree from `compute_parent_and_sibling_from_levels` is used unchanged.
- confidence: high

### A2.4 canonicalize_by_data baseline [debug-only]
- location: `dbtool:857-869`
- purpose: Run `canonicalize_by_data(_baseline_paras)` on a deepcopy of paragraphs to capture baseline cluster registry, dump only.
- inputs: deepcopy of `structure["paragraphs"]`
- outputs:
  - `_role_registry_baseline` (dict or None) — consumed: `_debug_payload["1e_canonical_clustering"]["role_registry_baseline_code"]` at line 1184.
- audit note: not consumed by main logic. Always runs (not gated by hybrid_mode).
- confidence: high

### A2.5 1e AI structural canonicalization [active]
- location: `dbtool:871-986`
- when called: Every section iteration, after parent correction. Important to note this is **labeled "1e" in code/comments but happens AFTER 1c and BEFORE 1d-code** in execution order. The numbering does NOT match execution order — see audit note below.
- purpose: AI assigns canonical cluster IDs to paragraphs (e.g., merges paragraphs of same role into role_cluster_N for downstream lookup). 1-pass + 1-repair-pass + fallback flow.
- inputs:
  - `paragraphs=structure["paragraphs"]`
  - `role_candidates=role_candidates` (from A2.1c)
  - `decisions=level_parsed.get("decisions", {})` (from A2.2)
- outputs:
  - `_1e_messages` (list) → debug `_canonical_clustering_dump["prompt_messages"]`
  - `_1e_llm_raw` (str) → debug `llm_raw_response`
  - `_1e_parsed` (dict {cluster_map, clusters, issues}) — used to decide branch
  - `_1e_repair_messages` / `_1e_repair_raw` / `_1e_repair_parsed` (optional)
  - `_role_registry` (dict {cluster_id: [paragraph idxs]}) — set via 1e_original, 1e_repaired, or fallback_baseline. Consumed: `_debug_payload["1e_canonical_clustering"]["role_registry"]` (line 1183). Downstream main-logic consumption: **not directly used** in A1/A2 — the registry is debug-only because `apply_structural_clustering` already mutated `structure["paragraphs"][i].role = cluster_id`.
  - `_1e_final_source` (str: "1e_original" / "1e_repaired" / "fallback_baseline")
  - `_fallback_reason` (str or None)
- mutation/side effects: `apply_structural_clustering(...)` mutates `structure["paragraphs"][i].role` to the cluster_id. This overwrites the placeholder `role` set in A2.2 (where role was the synthetic structure_role like `char_□__section_header`).
- failure/fallback path:
  - parse fail → `canonicalize_by_data(structure["paragraphs"])` fallback (deterministic code), `_1e_final_source = "fallback_baseline"`.
  - parse success + issues → repair LLM call. Repair parse fail OR repair still has issues → `canonicalize_by_data` fallback.
  - parse success + no issues → apply directly.
- audit note (numbering): Despite docstring/code labels of "1e", this stage executes BEFORE "1d code" (exclusivity) and BEFORE "1e code" (format observations). The "1e" label is reused twice — once for AI canonicalization (this), once for code format observations (A2.7). This is confusing. Source of truth: execution order is 1a → 1b → 1c → parent-correction → 1e AI canonicalization → 1d code → 1e format code → 1f marker policy.
- confidence: high

### A2.6 1d code — compute_exclusivity_rules_code [active]
- location: `hwpx_analyzer.py:7737`, called `dbtool:999-1018`.
- when called: Every section iteration, after 1e AI canonicalization.
- purpose: From parent→children co-occurrence counts, derive exclusivity rules (which child roles never co-occur). Pure code, no AI.
- inputs:
  - `_pc_data` (dict {parent_role: [child_set, ...]}) — computed by `compute_parent_instance_children_by_parent_idx` when hybrid_mode, else `compute_parent_instance_children(structure)`.
- outputs:
  - `exclusive_rules` (list[dict {parent, variants, pairs_cooccurred}]) — consumed: yes, set as `structure["exclusive_rules"]` (line 1018) AND stored in `_debug_payload["exclusivity_analysis"]` (lines 1223-1237). Downstream chapter assembly may consult these; in A1/A2 only the structure mutation matters.
- failure/fallback path: try/except → `exclusive_rules = []`.
- audit note: also computes a "before correction" version `_exclusive_before` (lines 710-712) for debug compare.
- confidence: high

### A2.7 1e code — compute_format_rules_code [active]
- location: `hwpx_analyzer.py:7827`, called `dbtool:1024-1041`.
- when called: Every section iteration, after 1d-code.
- purpose: From light XML format observations, derive `format_rules` (indent_parts, marker_style, markers_sample, separator per role) and `blank_rules` (between-role blank line policy). Pure code.
- inputs:
  - `_format_obs` = `compute_format_observations(structure, _section_light_xml, idx_map=_section_idx_map)` at line 1026
- outputs:
  - `format_rules` (dict) — consumed: yes, set as `structure["format_rules"]` (line 1039) AND debug at line 1241.
  - `blank_rules` (list) — consumed: yes, set as `structure["blank_rules"]` (line 1041) AND debug at line 1242.
- failure/fallback path: try/except → both empty.
- audit note: this is the second "1e" label collision (with A2.5). Reads per-section light_xml directly (blanks included), not truncated_xml.
- confidence: high

### A2.8 1f marker policy induction [active — IS AI]
- location: dbtool:1042-1064. Functions: `hwpx_analyzer.py:3731 (build), :3776 (parse), :3799 (verify)`.
- when called: Every section iteration, after 1e-code. **AI path** in the main pipeline.
- purpose: AI inspects role samples (text previews via `idx_texts`) to determine per-role marker policy: `explicit` / `no_marker` / `ambiguous`, with evidence (detected_marker per sample_idx). Code then verifies evidence against actual text.
- inputs:
  - `structure.get("paragraphs", [])`
  - `_idx_texts` (dict from A2.1b)
- outputs:
  - `_msgs_1f` (list) — consumed: not stored in `_debug_payload` directly (no key references it). actually consumed: unknown beyond LLM call. confidence: medium.
  - `_llm_1f` (str) — same.
  - `_marker_policy_1f` (dict {roles: [{role, marker_policy_status, evidence, verification}]}) — consumed: yes:
    - stored as `structure["marker_policy_1f"]` (line 1058)
    - stored as `section_results[sid]["marker_policy_1f"]` (line 1098)
    - included in `_debug_payload["marker_policy_1f"]` (line 1136)
    - cached at line 1116
    - downstream consumed by `extract_marker_policies(... marker_policy_1f=...)` at `hwpx_analyzer.py:7490` (separately from A1/A2).
- failure/fallback path: try/except around the entire block → `_marker_policy_1f = None` + warning log.
- mutation/side effects: `structure["marker_policy_1f"]` added.
- audit note: **CONFIRMED AI** (not code). `build_marker_policy_prompt` constructs LLM messages, `_call_llm(_msgs_1f, "hwpx_1f_marker_policy")` invokes the LLM. There is a separate code-path `extract_marker_policies` (line 7490 of analyzer) which is used in 12.1 marker roundtrip — that's a different downstream path, not 1f itself.
- confidence: high

### A2.9 validate_structure_for_cache + write_cache_validation_debug [active]
- location: `hwpx_analyzer.py:5142 (validate), :5332 (write)`. Called `dbtool:1070-1078`.
- when called: Every section iteration, after 1f.
- purpose: 9-check structural validation gate (SC1-SC9). Determines `can_cache` (no blockers) and `should_abort` (any blocker).
- inputs:
  - `structure` (with paragraphs, template_grammar, etc.)
  - `chapter_types`
- outputs:
  - `_cache_validation` (dict {can_cache, should_abort, blocker_count, watch_count, checks: [...]}) — consumed:
    - written to `/tmp/hwpx_debug/05b_cache_validation.json` via `write_cache_validation_debug` (line 1075)
    - stored in `_section_cache_validations[section_id]` (line 1078)
    - section0-only: gates `_section0_can_cache` (line 1084) and triggers abort raise (line 1085-1090)
    - stored in `_debug_payload["cache_validation"]` (line 1135) for section 0
    - dumped to `/tmp/hwpx_debug/05d_section_cache_validations.json` at loop exit (lines 1252-1271)
- failure/fallback path: if `should_abort` → raise ValueError with check IDs. ONLY section0 triggers raise (line 1083); other sections' validations are debug-only.
- audit note: SC1-SC5 are "blocker" severity; SC6-SC9 are "watch" only. Cache gate logic deliberately ignores SC6-SC9.
- confidence: high

### A2.10 build_chapter_types_from_structure (code) [active]
- location: `hwpx_analyzer.py:5476`. Called `dbtool:1066-1067`.
- when called: Every section iteration, before validate_structure_for_cache.
- purpose: From `paragraphs` (with levels), derive `chapter_types` dict. Also computes `template_grammar`, `role_text_types`, `per_type_role_semantics`. All set on `structure`.
- inputs:
  - `structure` (with paragraphs having level, role)
- outputs:
  - `structure["chapter_types"]` (dict {type_name: {title_role, description, pattern}}) — consumed: line 1067 (`chapter_types = structure.get("chapter_types", {})`), validate at 1073, debug payload at 1145, cache save at 1112, Phase E re-overwrite at 1616-1618, section_results at 1097.
  - `structure["template_grammar"]` — consumed: cache + debug (line 1146).
  - `structure["role_text_types"]` — consumed: cache + debug (line 1147).
  - `structure["per_type_role_semantics"]` — consumed: cache + debug (line 1148).
- failure/fallback path: `_build_chapter_types` warns and returns `{}` if no chapter titles found.
- audit note: chapter_title_level is auto-decided — level 0 if 2+ level-0 paragraphs have descendants; else level 1 (treats lone level-0 as container/TOC). Confirmed at analyzer line 6386-6389.
- confidence: high

### A2.11 section_results[sid] population [active]
- location: `dbtool:1095-1102`
- when called: Every section iteration, after 1f + validate + chapter_types.
- purpose: Section-local result dict per 13.7b B2.1.1 (no document-level merge).
- shape per `section_results[section_id]`:
  - `structure` (dict): the full mutated structure (paragraphs with role, level, parent_idx, sibling_group_id, role_candidates, semantic_role, canonical_role, structure_role, marker_family, marker_policy_1f, exclusive_rules, format_rules, blank_rules, chapter_types, template_grammar, role_text_types, per_type_role_semantics, validator_issues).
  - `chapter_types` (dict): same as `structure["chapter_types"]` (duplicated for backward-compat).
  - `marker_policy_1f` (dict or None): same as `structure["marker_policy_1f"]`.
  - `signals` (dict): `compute_role_context_signals` result (line 991-993), with role_to_letter, compressed_sequence, role_stats, adjacency, role_scope_children, paragraph_texts.
  - `idx_texts` (dict {idx: str ≤80}).
  - `idx_full_texts` (dict {idx: str unlimited}).
- consumed: yes by many downstream paths — Phase E (`dbtool:1356`), section_results_debug (`dbtool:1316`), outer variable restoration (`dbtool:1276-1283`), cache save (`dbtool:1110-1122`), Phase E chapter_types overwrite (`dbtool:1616-1618`).

### A2.12 Incremental cache save [active]
- location: `dbtool:1104-1124`
- when called: Every section iteration if `_section0_can_cache` is True AND section 0 is in section_results.
- purpose: Save cumulative `section_results` to `/tmp/hwpx_cache/<key>.json` after every section. Mutable dict means each save includes all sections processed so far.
- inputs from `section_results[0]`: structure, chapter_types, signals, idx_texts, idx_full_texts, marker_policy_1f
- outputs:
  - JSON file at `/tmp/hwpx_cache/<_cache_key>.json` with `cache_schema_version=6` injected by `save_template_cache`.
- failure/fallback path: try/except → warning log only, does not raise.
- audit note: cache stores section 0's structure as the top-level `structure`/`chapter_types` keys plus the FULL `section_results` dict. This is a backward-compat shape — outer code consumes top-level, new code consumes section_results.
- confidence: high

### A2.13 _debug_payload assembly (section 0 only) [active]
- location: `dbtool:1129-1244`
- purpose: Build the in-memory debug payload (also written to disk by other stages). Only the section_id == 0 iteration writes it; other iterations skip.
- outputs (top-level keys):
  - model, from_cache, cache_path, cache_key, cache_validation, marker_policy_1f, llm_raw_response, structure_before_split, structure_after_split, split_log, marker_normalization, signals, xml, 1b_role_candidates, 1c_structure_global, level_analysis, parent_correction, 1e_canonical_clustering, parent_hint_measurement, exclusivity_analysis, format_analysis.
- audit note (multi-iteration safety): the comment at lines 1126-1128 claims dict-internal references (`split_log`, `marker_norm`, etc.) keep section0 values because the variables themselves get reassigned in next iteration but the dict holds references. Verified: dict stores by reference at assembly time, then those variables are reassigned to new objects in next iter — section0 values preserved.

### A2.14 Loop-end outer variable restoration [active]
- location: `dbtool:1273-1283`
- purpose: After the loop ends, `structure`/`chapter_types`/`_signals`/`_idx_texts`/`_idx_full_texts`/`_marker_policy_1f` are restored from `section_results[0]` for backward-compat with the chapter/shallow route downstream (which reads outer-scope vars).
- audit note: This is an explicit backward-compat hack. Outer-scope vars are now "alias to section 0".
- confidence: high

### A2.15 05d_section_cache_validations dump [debug-only]
- location: `dbtool:1250-1271`
- purpose: Dump per-section validation results to `/tmp/hwpx_debug/05d_section_cache_validations.json` for B0b review.
- consumed: external review only. confidence: high.

### A2.16 section_results_debug attach [debug-only]
- location: `dbtool:1311-1332`
- purpose: After cache hit OR cache miss, attach `_debug_payload["section_results_debug"]` with per-section summary (paragraph_count, table_count, chapter_types_keys, marker_policy_1f_present, role_count, cache_validation).
- inputs: `section_results`, `_section_cache_validations` (defensively probed via `dir()`).
- consumed: external debug. confidence: high.

---

## Unresolved references — Part A1+A2

### outputs I produced but couldn't find consumer for
- key: `analysis["original_xml"]` (produced at A1.1) — searched in: DB tool A1+A2 path. Never read in A1/A2. Possibly consumed elsewhere via `analyze_hwpx`'s exposure, but no reference in audited range. confidence: high.
- key: `removed_indices` outer (produced at A1.2) — searched in: dbtool 343-1334. Only logged for size at line 1300 (cache-hit branch). Inner per-section `_section_removed_indices` is also only logged. confidence: high.
- key: outer `idx_map` (produced at A1.2) — searched in: dbtool 343-1334. Never read after line 360. Per-section `_section_idx_map` IS used (passed to `compute_format_observations`). confidence: high.
- key: `messages_1` (closure-local `msgs_1`) (produced at A2.1a) — searched in: dbtool 343-1334. Returned in `_r1ab["messages_1"]`, unpacked at line 661, but never inserted into `_debug_payload` (only `llm_content_1` is at line 1137). Likely intended for prompt logging that is now dead. confidence: medium (might be logged by `_call_llm` internally — uncertain).
- key: `_msgs_1f` and `_llm_1f` (produced at A2.8) — searched in: dbtool. Not stored in `_debug_payload`. Only `_marker_policy_1f` is. The raw 1f prompt/response are NOT retained for debug. confidence: medium.
- key: `_role_registry` (produced at A2.5) — searched in: dbtool. Only used for debug payload at line 1183. Main-logic consumption is via `apply_structural_clustering` SIDE EFFECT on `structure["paragraphs"][i].role`, not via `_role_registry` itself. The variable is effectively debug-only. confidence: high.
- key: `_paragraphs_before` (produced at A2.1b post-1a) — searched in: dbtool. Only used at line 1139 for debug `structure_before_split`. Not consumed by logic. confidence: high.
- key: `marker_norm` (produced at A2.1b) — searched in: dbtool. Only debug line 1151 (`marker_normalization`). Not consumed by main logic in A1/A2. May be consumed by later stages — unknown. confidence: medium.

### inputs I read but couldn't find producer for
- key: `template_file_id` (read at A1.1) — searched in: dbtool function signature. Producer: function parameter `template_file_id` passed in by the caller (the AI tool framework dispatching `generate_document_hwp_local`). Not in audit window — external caller. confidence: high.
- key: `content_file_id` (read at A1.3) — same as above. External function parameter. confidence: high.
- key: `content_text` (read at A1.3) — function parameter. confidence: high.
- key: `self.valves.HYBRID_MEASUREMENT`, `self.valves.CANONICAL_FALLBACK_MODE`, `self.valves.DEBUG_MODE`, `self.valves.AI_MODEL` (read at A1.6, A2.3) — producer: tool valves configured outside this function. confidence: high.
- key: `_TEMPLATE_CORE_CASES` (read at A2.3) — hardcoded inline at dbtool:761-773. Self-contained. confidence: high.
