# HWPX Pipeline Audit — Part A11~A19

Scope: DB tool (`/tmp/dbtool_dump.py`) lines 2055~end of generate flow, plus the assembly path in `hwp_generator.py` and the debug finalization tail.

## A11 — 13.7a-A1 chapter route prep

### A11 [active]

- **Location**: `/tmp/dbtool_dump.py:2055-2083`
- **When called**: Inside `_shallow_done==False` branch, immediately after shallow `body_items`/`_section_fill_debug`/`_chapter_trees` reset.
- **Purpose**: Initialize chapter-grouped assembly state and template-driven loop driver decision (13.4b seed extraction).
- **Inputs**:
  - `_shallow_done` (var, producer: earlier shallow route block, bool)
  - `_tup` (var, producer: A8 target_unit_plan cached structure read, dict|None)
  - `_chapter_plan_seed` factor — `extract_chapter_template_plan_seed(_tup, structure, _idx_full_texts)` (hwpx_analyzer.py:9781)
  - `structure` (var, producer: 1a cache, dict)
  - `_idx_full_texts` (var, producer: earlier full-text gather, dict)
  - `pdf_text_content` / `content_text` for `_broad_source`
- **Outputs**:
  - `_chapter_objects` (list `[]` or None) — consumed by A13 (append), A17 assembly (content_data["chapters"]). actually consumed: yes
  - `_chapter_empty_reasons` (list `[]` or None) — consumed by A13 (append), A19 `_debug_payload["chapter_empty_reasons"]`. actually consumed: yes
  - `_tup_regions` / `_tup_region_by_id` / `_tup_chapter_regions` (dict / list) — consumed by A13 chapter_object region attach. actually consumed: yes
  - `_chapter_plan_seed` (dict|None) — consumed by A12 (whole 13.7c block) + A13 (loop driver branch). actually consumed: yes
  - `_chapter_loop_driver` ("template_plan" | "2a_chapters") — consumed by A13 branch label only (debug). actually consumed: yes
  - `_chapter_plan_debug` (dict `{}`) — consumed by A13 inside template loop block + emitted to `_debug_payload["chapter_template_plan"]`. actually consumed: yes
  - `_broad_source` (str) — consumed by A12 source_inventory call (50000-char), A13 (pdf_text=_broad_source), A16 (section N path), A14 (source_diagnostic). actually consumed: yes
- **Cache/debug**: no direct cache write. Seed inputs are read-only from `structure` cache (already-loaded via earlier full-namespace load).
- **Mutation/side effects**: None — only var binding.
- **Failure/fallback path**: If `_chapter_plan_seed` is `None` or `confidence == "low"` → loop driver falls back to `"2a_chapters"` and `_chapter_plan_seed = None`. A13 then routes through 2a-driven `elif not _shallow_done` block.
- **Audit note**: Note `_chapter_objects = None` when `_shallow_done` is True — A17 assembly logic uses `_chapter_objects is not None and not _shallow_done` gating, so shallow → flat `body` path.
- **Confidence**: high

---

## A12 — 13.7c Source-to-Template Adaptation Planning

### A12 [active]

- **Location**: `/tmp/dbtool_dump.py:2084-2306` (chapter loop body starts at 2293)
- **When called**: After A11, inside `if not _shallow_done and _chapter_plan_seed:` guard (chapter route with template-driven seed only). 2a-driven fallback skips this entire block.
- **Purpose**: Run two AI calls — source inventory (A) + adaptation plan (B) — to attach `adaptation_decision` (action / adapted_title / evidence / supporting evidence) to each template chapter; falls back to preserve-all on exception.
- **Inputs**:
  - `_chapter_plan_seed["chapters"]` (list[dict]) — producer: A11 (extract_chapter_template_plan_seed)
  - `_broad_source` (str) — producer: A11
  - `_ch_inputs_for_plan` (list[dict], local) — derived from seed; carries `idx`, `original_title`, `description`, `local_pattern_summary`, `local_catalog_summary`
  - LLM responses via `_call_llm`
- **AI calls**:
  - (A) `build_source_inventory_prompt(_broad_source, max_source_chars=_si_input_max)` (hwpx_analyzer.py:11321), `_si_input_max = 50000` (task 2 fix). Tag `hwpx_13_7c_source_inventory`. Retry once (tag `..._retry`) if validation fails.
  - (B) `build_adaptation_plan_prompt(_source_inventory, _ch_inputs_for_plan, broad_source_preview=_broad_source[:50000], max_source_preview_chars=50000)` (hwpx_analyzer.py:11465). Tag `hwpx_13_7c_adaptation_plan`. Retry once.
  - Split path: `should_split_adaptation_batch(_ap_prompt_text)` (12185) → if True, prompts are rebuilt per half-chunk via additional `build_adaptation_plan_prompt(..., max_source_preview_chars=50000)` calls (tag `..._chunk_N`). **OUTSTANDING BUG: split path does NOT parse top-level `overall_source_focus`** — `_ap_parsed` in split path is the synthetic merged dict containing only `chapter_decisions` + `_validation`, so `_osf` lookup at line 2254 finds nothing (returns `None`). Single path only parses `_osf`.
- **Validation / normalize / demotion**:
  - `parse_adaptation_plan_from_llm(raw, _expected_idx)` (11889) — strips fences, parses JSON, surfaces `overall_source_focus`, validates per-decision idx + presence
  - For each decision: `normalize_adaptation_decision(_d, _orig_title)` (12203) then `validate_adaptation_decision(_norm_d)` (11983) — if `should_demote` → `make_validation_failed_decision(...)` (12329)
  - Missing idx fallback: `make_unavailable_decision(...)` (12282)
- **Reference metrics**: `compute_reference_metrics(_decision, broad_source=_broad_source, generated_body_text=_gen_body_text)` (12120) — runs INSIDE A13 chapter loop after each `_sf_result` produced, not here. **debug-only** per design (no policy effect).
- **Outputs**:
  - `_adaptation_plan_summary` (dict | None) — emitted by `summarize_adaptation_plan(...)` (12377). Consumed by A19 `_debug_payload["adaptation_plan"]` write at line 2826. actually consumed: yes
  - `_ch_decisions_by_idx` (dict[int, dict]) — consumed by A13 inside template chapter loop (decision lookup for adapted_title / action / hint prepend). actually consumed: yes
  - `_adaptation_ai_calls` (dict) — folded into `_adaptation_plan_summary["ai_calls"]`. actually consumed: yes
  - `_source_inventory` (dict) — consumed implicitly by A16 (`_section_n_si = _source_inventory` at line 2920 via NameError-guarded read). actually consumed: yes (via A16 reuse)
  - `_normalized_decisions` (list) — only used to build summary. actually consumed: yes
- **Cache/debug**:
  - `_debug_payload["source_inventory_diag"]` (line 2138) — task 3 fix. Contains `source_length`, `source_inventory_input_length`, `source_inventory_call_status`, raw preview (2000 chars), parsed summary/topics/headings/evidence_count.
  - `_debug_payload["adaptation_plan"]` (set at 2826/2827, A19 path) — full summary dict with action_distribution (alias of title_action_distribution — task 5), title_action_distribution, content_action_distribution, validation_failure_count, average_confidence (mode), title_source_fit_distribution, chapter_title_mode_distribution, overall_source_focus, ai_calls, batch_strategy, batch_split_reason.
- **Mutation/side effects**: None on `structure`. Builds local `_ch_decisions_by_idx` only. `summarize_adaptation_plan(...)` is pure.
- **Failure/fallback path**: Outer `try/except` (2275-2291): on any exception, every chapter gets `make_unavailable_decision(ch_i, original_title, f"adaptation_plan_exception: {_ap_e}")` → action='adapt_topic_terms' + adapted_title=original_title (`supported_as_is` strict per `make_unavailable_decision`).
- **Audit note**:
  - `_osf` (`overall_source_focus`) extracted from `_ap_parsed.get("overall_source_focus")` (line 2254) only in single-path branch. In split path, `_ap_parsed` is reconstructed without `overall_source_focus` → `_osf` becomes `{"topic": None, "reason": "missing_from_llm_output"}` upstream OR plain None (depends on which branch built it). **Confirmed outstanding bug per task description.**
  - Note `action_distribution` is an alias (title_action_distribution) for backward compatibility (task 5).
- **Confidence**: high

---

## A13 — 2b chapter loop (core content generation, single-section path)

### A13 [active]

- **Location**: `/tmp/dbtool_dump.py:2293-2614` covers both template-driven (2293-2513) and 2a-driven (2514-2614) variants. Focus here on single-section (section 0) path.
- **When called**: Two mutually exclusive branches:
  - Template-driven: `if not _shallow_done and _chapter_plan_seed:` (2293)
  - 2a-driven: `elif not _shallow_done:` (2514)
- **Purpose**: For each template chapter (or 2a chapter), call 2b LLM to fill body items, run grammar validation, build `chapter_object`, append to `_chapter_objects`.
- **Inputs (template-driven)**:
  - `_seed_chapters = _chapter_plan_seed["chapters"]`
  - `_seed_ch_type` / `_seed_type_info` / `_seed_pattern` / `_seed_title_role` / `_seed_pattern_roles` / `_seed_catalog`
  - Per chapter: `tpl_ch` (local_pattern, local_catalog, region_id), `_ch_decisions_by_idx[ch_idx]`
- **Per-chapter flow (template-driven)**:
  1. ch_title resolution: `_ad_t = (_ch_dec_pre.get("adapted_title") or "").strip()`; if non-empty → `ch_title = _ad_t`. 13.7c-2phase: all actions (including preserve) use adapted_title.
  2. Per-chapter pattern (13.6-B): if `_ch_local_pattern` (from `tpl_ch.get("local_pattern")`), use it; else fallback to `_seed_pattern`.
  3. Decision unpack: `_title_action`, `_content_action`, `_action = _title_action` (debug alias).
  4. 13.7e: all chapters call 2b (no `source_gap` branch — `if False: pass` placeholder kept).
  5. `build_section_fill_prompt(...)` (14542) called with `pdf_text=_broad_source` (broad-source fallback).
  6. **13.7e adaptation hint prepend**: if `_decision and _title_action in ("adapt_role_equivalent_title", "adapt_from_source_block", "adapt_topic_terms")`, prepend `[13.7e adaptation hint]` block (adapted_title, original_title, title/content_action, preserved_aspects[:3], adapted_aspects[:3], supporting_evidence[:3]) to first user message in `messages_2b`.
  7. `await _call_llm(messages_2b, f"hwpx_section_fill_{ch_idx}")`
  8. Override grammar (13.6-B): if local_pattern + `_ch_pattern_source == "per_chapter_subtree"` → `pattern_to_grammar(_ch_local_pattern)` (hwpx_analyzer.py:9972).
  9. `process_section_fill_result(llm_content_2b, ch_idx, ch_title=_2b_title, ch_type=_seed_ch_type, title_role=_ch_title_role, template_grammar=structure.get("template_grammar", {}), role_text_types=structure.get("role_text_types"), pattern_roles=list(_ch_pattern_roles), section_pdf_text_len=len(_broad_source), override_grammar=_override_grammar, override_root_roles=_override_root_roles)` (15360) — does: `parse_section_fill_from_llm` (14867) → `normalize_section_items` (14920) → `validate_ai_parent_ids` (15091) → `apply_parent_id_fallback` (15295) → `reconstruct_tree_from_flat` + `validate_reconstruction` for grammar validation.
  10. `_ch_status = "filled" if len(_ch_items) > 1 else "insufficient_source"`; if `_action == "preserve"` → `"preserved_by_13_7c"`.
  11. Region attach: `_ch_region = _tup_region_by_id.get(tpl_ch.get("region_id"))`
  12. Empty diagnostic: `_empty_reason = diagnose_chapter_empty_reason(_sf_result)` (11007) — returns `{is_empty, stage, evidence}` based on debug_entry's llm_raw_response_len / raw_items_count / normalized_items_count / grammar_violations_count.
  13. Reference metrics: if `_decision` → join body_items text → `compute_reference_metrics(_decision, broad_source=_broad_source, generated_body_text=_gen_body_text)` (12120). **debug-only — never feeds back to policy** per docstring.
  14. `build_chapter_object(source_chapter_idx=ch_idx, target_region=_ch_region, section_fill_result=_sf_result, empty_reason=_empty_reason, adaptation_decision=_decision, reference_metrics=_ref_metrics)` (11090) — produces `{source_chapter_idx, target_region_id, section_id, first_paragraph_idx, paragraph_indices, section_local_first_idx, section_local_paragraph_indices, title_item, title_node, body_items, body_nodes, status, _debug}`. Note `section_id` defaults to 0 here (region.section_id missing).
  15. If `_action == "preserve"` → force `_ch_obj["status"] = "empty"`.
  16. Append to `_chapter_objects` + `_chapter_empty_reasons`. Append debug rows to `_per_ch_status`.
  17. On exception (2453): build `_ch_fail_obj` with `status="fail"`, append. `_chapter_trees.append(None)`.
- **Inputs (2a-driven)**:
  - `chapters` (list, producer: A4 chapter_classify) — instead of `_chapter_plan_seed["chapters"]`
  - `chapter_types[ch_type]` — instead of seed pattern lookup
  - `source_sections[ch_idx]` (producer: A6 `split_source_by_chapters`) — instead of `_broad_source`. **No adaptation hint** in 2a-driven path (no `_decision` available).
- **Per-chapter flow (2a-driven)**:
  1. Skip if `ch_type not in chapter_types`.
  2. Build prompt with `pdf_text=section_pdf_text` (per-chapter slice).
  3. `process_section_fill_result(...)` with no `override_grammar` (no local_pattern from 2a path).
  4. Region attach via `_tup_chapter_regions[ch_idx]` (positional fallback, marked `region_match: fallback_no_region` if absent).
- **Outputs**:
  - `_chapter_objects` (mutated by append) — consumed by A16 (section-local extends), A17 assembly (content_data["chapters"]).
  - `_section_fill_debug` (mutated by append) — consumed by A17 `_debug_payload["section_fill"]`.
  - `_chapter_trees` (mutated by append) — historical (13.7a-A1 noted "chapter_trees param removed"), still appended for backward-compat debug. actually consumed: only via append to `_chapter_trees` list (no downstream reader since chapter_trees param dropped from assemble_hwpx_hybrid).
  - `_per_ch_status` (list) — consumed by A14 source_diagnostic + `_chapter_plan_debug["per_chapter_status"]`.
  - `_chapter_plan_debug` (dict) — emitted to `_debug_payload["chapter_template_plan"]` (line 2512). actually consumed: yes (debug only).
- **Cache/debug**:
  - `_debug_payload["chapter_template_plan"]` (line 2512) — full template plan debug including `seed`, `per_chapter_status`, `source_mode`, `2a_chapters_ignored`, `2a_vs_template_diff`, `source_diagnostic` (A14).
- **Mutation/side effects**:
  - `_chapter_objects.append(...)` (mutation list owned by A11)
  - `_section_fill_debug.append(...)`
  - `_chapter_trees.append(...)`
  - `_chapter_empty_reasons.append(...)` (mutation list owned by A11)
  - **NO mutation of `structure`** in chapter loop itself.
- **Failure/fallback path**: per-chapter try/except → fail chapter_object appended with `status="fail"`, `_debug.fail_reason=str(e)`. No retry at 2b level here.
- **Audit note**:
  - `_chapter_trees` list still appended despite chapter_trees= kwarg dropped from `assemble_hwpx_hybrid`. Dead-ish but harmless.
  - `_2b_title = ch_title` and ch_title is already adapted_title at this point (line 2278-2312), so the `[13.7e adaptation hint]` prepend then carries adapted_title twice (in `## 대제목` block and in hint preamble).
- **Confidence**: high

---

## A14 — 13.6-C Source diagnostic

### A14 [debug-only]

- **Location**: `/tmp/dbtool_dump.py:2490-2511`
- **When called**: Inside template-driven branch tail (only runs when `_chapter_plan_seed` triggered). NOT run in 2a-driven path.
- **Purpose**: Per-chapter source size vs item-count metric to flag anomalies (long source / zero items, short source / many items).
- **Inputs**:
  - `_broad_source` (str, producer: A11)
  - `_per_ch_status` (list[dict], producer: A13 template-driven loop)
  - `source_sections` (list, producer: A6 split_source_by_chapters) — for `split_available` flag
  - `_seed_chapters` (list, producer: A11)
- **Outputs**:
  - `_chapter_plan_debug["source_diagnostic"]` — consumed only by `_debug_payload["chapter_template_plan"]`. actually consumed: no consumer beyond debug write.
  - Fields: `broad_source_chars`, `estimated_tokens_per_chapter`, `total_estimated_tokens`, `per_chapter` (ch, items, insufficient, source_chars, ratio), `anomalies` (`source_long_items_zero` / `source_short_items_many`), `split_available`, `split_section_lengths`.
- **Cache/debug**: writes into `_chapter_plan_debug["source_diagnostic"]` (no separate cache key).
- **Mutation/side effects**: pure dict construction.
- **Failure/fallback path**: None (no try/except). Heuristic anomalies are informational only — no policy effect.
- **Audit note**: Anomaly thresholds `_bs_len > 10000 and _ic == 0` and `_bs_len < 1000 and _ic > 20` are hardcoded heuristics. Not consumed by any downstream code path.
- **Confidence**: high (debug-only, never read downstream)

---

## A15 — 13.7a-0 A0 parallel measurement

### A15 [debug-only]

- **Location**: `/tmp/dbtool_dump.py:2615-2631`
- **When called**: After chapter loop (both paths) completes. Always runs unconditionally inside `try/except`.
- **Purpose**: Measure 1d `chapter_types[*].title_role` set vs `chapter_template_plan.seed.chapters[*].local_title_role` set — flag mismatch for future 1d fix prioritization.
- **Inputs**:
  - `structure` (var, producer: 1a cache)
  - `_chapter_plan_seed` (dict|None, producer: A11)
  - `_ctp_wrap = {"seed": _chapter_plan_seed}` — wrap because `measure_title_role_consistency` expects `chapter_template_plan.seed.chapters` schema
- **Outputs**:
  - `_debug_payload["title_role_consistency"]` — consumed only by debug writer. Contains `chapter_types_title_roles`, `chapter_types_title_roles_set`, `local_title_roles_per_chapter`, `local_title_roles_set`, `mismatch_summary` (`all_local_in_1d_set`, `missing_from_1d_set`, `extra_in_1d_set`), `per_chapter`, `status` (one of "ok"/"no_plan"/"no_chapter_types"/"empty_plan"). actually consumed: no downstream beyond debug.
- **Cache/debug**: only `_debug_payload["title_role_consistency"]`.
- **Mutation/side effects**: none.
- **Failure/fallback path**: try/except wraps everything → on error, writes `{"error": str(_a0_e), "debug_only": True}`.
- **Audit note**: Comment says "정책에 영향 X — 1d-fix stage 우선순위 판단 자료". Confirmed debug-only.
- **Confidence**: high

---

### Note: 13.7b B0a Section Census + B2.2 + B0b (between A15 and A16)

These three blocks at `dbtool_dump.py:2632-2823` are **measurement-only**. They are not explicitly enumerated as A11-A19 but feed inputs into A16:
- **2632-2648 B0a `extract_section_census(template_path)`** → `_debug_payload["section_census"]` + `_section_census` local var (consumed by A16 for `_xml_texts` reference).
- **2649-2788 B2.2 Section Role Proposal AI** (`build_section_role_proposal_prompt` + `parse_section_role_proposal_from_llm` + `validate_section_role_proposal` + `make_fallback_section_role_proposal`) — feeds `_debug_payload["section_role_proposals"]`, **consumed by A16 via `_b22_by_sid` for `decide_section_processing(...)` ref_label lookup**. status: [active] (B2.2 proposal directly drives A16 generate/preserve decision, deadline policy at hwpx_analyzer.py:13907).
- **2790-2823 B0b Merge feasibility** → `_debug_payload["merge_feasibility"]` + `b0b_observation_artifact`. status: [debug-only].

---

## A16 — 13.7b section-local generation-lite (chapter route, section N != 0)

### A16 [active]

- **Location**: `/tmp/dbtool_dump.py:2834-3232`
- **When called**: `if not _shallow_done and section_results and _chapter_objects is not None:` after A12/A13/A15/B0a/B2.2/B0b.
- **Purpose**: Run a SECOND pass for each section N != 0, building `section_local_chapter_list` from 1a section_results, picking generate-or-preserve via `decide_section_processing(...)`, calling 2b for each chapter in generate sections. Adds chapter_objects with `section_id != 0`. Single source-of-truth for multi-section assembly anchor matching.
- **Inputs**:
  - `section_results` (var, producer: 1a multi-section split, dict[sid → section_result])
  - `_section_census` (var, producer: B0a)
  - `_debug_payload["section_role_proposals"]` (B2.2)
  - `template_path` for re-extracting section XMLs
  - `_chapter_objects` (list, producer: A11/A13 — mutated by append)
- **Sub-steps**:
  1. **XML paragraph text extraction**: `extract_all_sections_xml(template_path)` then for each section name `extract_section_xml_paragraph_texts(template_path, sname)` (hwpx_analyzer.py:13663) → `_section_xml_paragraph_texts: dict[sid → list[text]]`.
  2. **Section offset computation**: `compute_section_offsets(section_results, _section_xml_paragraph_counts)` (13428). Uses census counts when available (assembly's `_section_top_level_paragraphs` idx alignment), else 1a paragraph count fallback.
  3. **1a→xml idx mapping**: `_build_1a_to_xml_p_idx_mapping(idx_texts, _xml_texts_for_sec)` (13469) — text-normalized substring matching to bridge 1a paragraph-count gap to xml top-level p idx. 13.7b fix.
  4. **Section chapter list**: `extract_section_chapter_list(_sid_int, _sr_sec, _offset, ai_to_xml_idx_mapping=_1a_to_xml_map)` (13696) → `{section_id, section_offset, paragraph_count, title_roles_used, chapters: [...], confidence}`.
  5. **Section decision**: `decide_section_processing(_sid_int, _b22_p, _scl)` (13907) — deadline policy: `confidence != low` + `reference_label in ('top_level', 'body', 'other')` → generate, else preserve. Section 0 forced to `{"action": "existing_chapter_route", ...}` (skip generation here).
  6. **Per-section adaptation_plan**: if `_section_n_si` (source_inventory reused from A12 via NameError-guarded read at line 2920, else fresh AI call tagged `hwpx_13_7b_section_n_source_inventory`) → `build_adaptation_plan_prompt(_section_n_si, _sec_ch_inputs, broad_source_preview=_broad_source[:10000])`. Tag `hwpx_13_7b_section_n_adaptation_plan_sec{sid}`. No retry. Same `normalize_adaptation_decision` + `validate_adaptation_decision` → `make_validation_failed_decision` flow. Missing-idx fallback: `make_unavailable_decision`.
  7. **Per-section per-chapter 2b**:
     - Build `_sec_full_catalog` from section's paragraphs (role → first non-empty exemplar).
     - For each chapter in section: pick `_dom_type_n` (chapter local dominant), `_dom_pattern_n`, derive `_dom_catalog_n`.
     - If `_action_n == "preserve"`: build **synthetic** `_sf_result_n` containing `body_items=[_synthetic_title_item_n]` (role + text + marker only), `chapter_tree_nodes=[{id:0, ...}]`, `items_count=0`. This is the "13.7b: preserve 시 synthetic title_item을 _sf_result에 채워서 build_chapter_object가 title_item.role을 추출하게 함" path.
     - Else: `build_section_fill_prompt(...)` with `pdf_text=_broad_source`, `template_chapter_context={template_title, description, section_id, position, total_chapters}`. If `_action_n == "adapted_title_generate"` → adaptation hint prepend (same shape as A13). Tag `hwpx_section_fill_sec{sid}_ch{ci}`. Then `process_section_fill_result(...)` with no override_grammar.
  8. **Synthetic region for chapter_object**: `_synthetic_region_n = {region_id: None, section_id: _sid_int, section_local_first_idx: ch.title_section_local_idx (xml idx), section_local_paragraph_indices: ch.section_local_paragraph_indices, title_role: _title_role_sec_n, marker: ch.marker, paragraph_indices: ch.document_global_paragraph_indices}`.
  9. **build_chapter_object(...)** with synthetic region + decision → `_ch_obj_n`. Force `_ch_obj_n["section_id"] = _sid_int`. If preserve action → `status="empty"`. Append to `_chapter_objects`.
  10. **`_analyzed_section_ids.add(_sid_int)`** — passed to A17 assembly.
- **Outputs**:
  - `_section_local_decisions` (dict[sid → decision]) — consumed by A19 `summarize_section_local_decisions(...)` → `_debug_payload["section_local_decisions"]`. actually consumed: yes (debug + driver in this block).
  - `_section_local_chapter_lists` (dict[sid → result]) — consumed by `summarize_section_local_decisions`, by A17 for `build_chapter_local_exemplars(...)`. actually consumed: yes.
  - `_section_local_offsets` (dict) — consumed only within block. actually consumed: only here.
  - `_analyzed_section_ids` (set, initially `{0}`) — consumed by A17 `assemble_hwpx_hybrid(analyzed_sections=...)`. actually consumed: yes.
  - `_section_xml_paragraph_texts` (dict) — consumed by A17 chapter_local_exemplars build. actually consumed: yes.
  - Appended `_chapter_objects` items with `section_id != 0` + `section_local_first_idx` set → consumed by A17/A18 chapter_anchor matching (Priority 1 anchor).
  - `_debug_payload["section_local_decisions"]` (3213), `_debug_payload["section_local_chapter_lists"]` (3216).
- **Cache/debug**: see above. No structure cache write.
- **Mutation/side effects**: `_chapter_objects.append(...)` mutates list owned by A11.
- **Failure/fallback path**: outer try/except (2846/3224) → on full block exception, log warning + `_debug_payload["section_local_decisions"] = {"error": ..., "debug_only": True}`. Per-section per-chapter 2b individually try/excepted → `_sf_result_n = {body_items: [], chapter_tree_nodes: [], items_count: 0, grammar_passed: False, debug_entry: {error}}`. source_inventory call individually try/excepted (sets `_section_n_si = None`).
- **Audit note**:
  - `_section_n_si = _source_inventory` (line 2920) reuses A12's variable via NameError guard — fragile if A12 didn't run (e.g., chapter route without `_chapter_plan_seed`).
  - synthetic title_item insertion for preserve has marker derived from `_ch_local.get("marker", "")` — assembly uses this for placeholder role determination.
  - **Critical**: `section_local_first_idx` is set to **xml idx** (via `_resolve_xml_idx`) so A17/A18 assembly Priority 1 matching aligns with `_section_top_level_paragraphs[sid][idx]`.
- **Confidence**: high

---

## A17 — Step 5 Assembly

### A17 [active]

- **Location**: `/tmp/dbtool_dump.py:3233-3361`
- **When called**: Always (if `not _shallow_done`), after A16.
- **Purpose**: Build `content_data`, compute region action plan + multi-section diagnostic + chapter-local exemplars, call `assemble_hwpx_hybrid(...)`, capture result into `_debug_payload["assembly"]`.
- **Inputs**:
  - `_chapter_objects` (list|None, producer: A11/A13/A16)
  - `_shallow_done`, `header_data` (producer: A7)
  - `body_items` (producer: shallow path A10 — empty here if chapter route)
  - `_tup` (producer: A8 read), `structure`, `idx_map`
  - `_section_local_chapter_lists` / `section_results` / `_section_xml_paragraph_texts` (producers: A16)
  - `_analyzed_section_ids` (producer: A16)
  - `template_path`
- **Sub-steps**:
  1. **content_data assembly**:
     - If `_chapter_objects is not None and not _shallow_done` → `content_data = {"header": header_data, "chapters": _chapter_objects}` (chapter route).
     - Else → `content_data = {"header": header_data, "body": body_items}` (shallow/legacy).
     - Log includes ok/empty/fail count from chapter objects.
  2. **Region action plan (13.5)**: `compute_region_action_plan(_tup, structure, idx_map=idx_map)` (10295). Returns `{actions, preserve_indices, summary, warnings}`. `_chapter_preserve = set(_region_plan["preserve_indices"])` — passed as `preserve_indices=` to assemble. Written to `_debug_payload["region_action_plan"]`.
  3. **Multi-section diagnostic (13.6-A)**: `diagnose_multi_section(template_path)` (10489) — `_debug_payload["multi_section_diagnostic"]`. Just observation — `gate_decision.section_aware_assembly_needed` not used downstream as a hard gate.
  4. **Chapter-local exemplars (13.7b §4)**:
     - Build `_ai_to_xml_for_local` for section N (N != 0) using `_build_1a_to_xml_p_idx_mapping(...)`.
     - `_section_n_local_dict = build_chapter_local_exemplars({sid: scl for sid in _section_local_chapter_lists if sid != 0}, section_results, _ai_to_xml_for_local)` (13556) → `{local_ch_idx: {section_id, role_to_xml_idx, chapter_local_idx}}`.
     - Remap local_ch_idx → global chapter_obj_idx by counting section 0 chapters: `_global_ch_idx = _section_0_ch_count_a + _local_ch_idx`. Result stored in `_chapter_local_exemplars`.
  5. **Assembly call**:
     ```python
     assemble_hwpx_hybrid(template_path, structure, content_data,
         removed_indices=removed_indices, idx_map=idx_map,
         content_only_mode=True,
         preserve_indices=_chapter_preserve,
         analyzed_sections=_analyzed_section_ids,
         chapter_local_exemplars=_chapter_local_exemplars)
     ```
  6. **Debug capture**:
     - `_debug_payload["section_fill"] = _section_fill_debug` (A13 collected)
     - `_debug_payload["final_content"] = {header, body_items_count, body_items}` (shallow context; for chapter route body_items=[])
     - `_debug_payload["assembly"] = {success_count, fail_count, errors, output_size, marker_rewrite_log, rewrite_alignment, phase2_reattach_result, section_info}`
- **Outputs**:
  - `result` (`HwpxResult`) — used by A19 status message + bytes output.
  - `content_data` (dict) — local only.
  - `_debug_payload["assembly"]` keys feed final dump.
- **Cache/debug**:
  - `_debug_payload["region_action_plan"]`, `["multi_section_diagnostic"]`, `["section_fill"]`, `["final_content"]`, `["assembly"]`.
- **Mutation/side effects**:
  - `assemble_hwpx_hybrid` writes back to `structure` (`_marker_rewrite_log`, `_rewrite_alignment`, `_phase2_reattach_result`, `_dirty_marking`, `_final_id_reassignment`) — these are then read back here for `_debug_payload["assembly"]` capture.
- **Failure/fallback path**: No try/except at this level for `assemble_hwpx_hybrid` call → if it raises, falls through to outer DB tool error handling (not in this scope).
- **Audit note**:
  - `content_data` keys mutually exclusive (chapter vs body) — assemble logs warning if both present (`assemble_hwpx_hybrid` at hwp_generator.py:1171).
  - `_chapter_local_exemplars` index calculation depends on section 0 chapter count and section N chapter ordering — fragile if A11 chapter ordering changes.
- **Confidence**: high

---

## A18 — hwp_generator (the actual XML writer)

### A18 — `assemble_hwpx_hybrid` [active]

- **Location**: `/home/sprint/2026_capstone_jeonbuk/.claude/worktrees/hwp_gen/app/backend/open_webui/utils/hwp_generator.py:1089-2847`
- **When called**: A17 invocation (only consumer in production). Also called from `routers/files.py:995` import for `/generate-hwpx` endpoint.
- **Purpose**: Open HWPX, build role→exemplar map, preserve header/secPr/preserve_indices/empty-chapter-anchors/unanalyzed-section paragraphs, remove body paragraphs, insert generated body items with marker rewrite + region-aware placement next to chapter anchors, apply adapted_title via `_replace_text_in_paragraph_elem`, return bytes.
- **Inputs**:
  - `template_source` (path/bytes/file-like)
  - `structure` (dict — paragraphs, chapter_types, format_rules, blank_rules, template_grammar, marker_policy_1f)
  - `content` (dict — `chapters` OR `body` + `header`)
  - `removed_indices`, `idx_map`
  - `enable_marker_rewrite` (default True)
  - `content_only_mode` (default False but A17 always passes True)
  - `preserve_indices` (set from A17 region_plan)
  - `analyzed_sections` (set from A17/A16)
  - `chapter_local_exemplars` (dict from A17 §4)
- **Sub-steps (in order)**:
  1. Entry diagnostic write `/tmp/hwpx_debug/_d00_assemble_entry.json` (DIAG only).
  2. Open HwpxDocument.
  3. `_chapter_proc = _process_chapter_objects(_chapter_objects, structure, idx_map)` (744) if `content["chapters"]` present. Both `body` + `chapters` → warning, chapters wins.
  4. Build `role_exemplar_idx` (first idx per role, skip level 0 cover/toc unless title_role or has children).
  5. `_strip_document_ctrls`/`_strip_linesegarray` on exemplar deepcopies.
  6. Build blank_exemplars by `paraPrIDRef`.
  7. Process header data → text replace via `_set_element_text`; track `header_indices`.
  8. Add `_is_skip` level-0 paragraphs + first paragraph (secPr) + 9.1b secPr carrier preserve into header_indices.
  9. Add `preserve_indices` (region plan) into header_indices.
  10. Add `_chapter_proc["empty_preserve_indices"]` into header_indices (will be recomputed at step 15).
  11. **chapter_anchors matching loop** (1566-1752):
      - Priority 1: `section_local_first_idx + section_id` → `_section_top_level_paragraphs[sid][idx]`. `_validate_anchor_signature`.
      - Priority 2: legacy paragraph_indices[0] + idx_map (only if same section). Cross-section → priority 3.
      - Priority 3: text fallback `_find_anchor_in_section_by_text(title_text, sid)` — same section only.
      - Priority 4 (none found): `chapter_anchor_failures.append(...)`, continue with `placement_failure`.
      - Invariant: anchor owning section == chapter.section_id → CROSS_SECTION_BLEED hard fail (continue, skip).
      - Success: add anchor doc_idx to header_indices.
      - **13.7d 2phase adapted_title apply**: `_ad_text` extracted from `_ch_obj_for_anchor._debug.adaptation_decision.adapted_title`. If non-empty and ≠ anchor text → `_replace_text_in_paragraph_elem(_anchor_el, _ad_text, NS)` (3001). action_action check removed per recent fix (line 1673-1674 comment).
      - Per-ci diag write to `/tmp/hwpx_debug/_d02_anchor_per_ci.jsonl`.
  12. Build `_placement_failed_chapter_indices` set; errors appended for each placement_failure.
  13. **Empty preserve recompute**: clear `_chapter_proc["empty_preserve_indices"]`, repopulate using `chapter_anchors[ci]` doc_idx (more accurate than `_process_chapter_objects` paragraph_indices[0] mapping).
  14. Debug dump `/tmp/hwpx_debug/17_assembly_anchor_debug.json`.
  15. Unanalyzed section preserve safety: if `analyzed_sections` set, preserve all paragraphs in sections NOT in `analyzed_sections`.
  16. **Body remove loop**: every paragraph NOT in header_indices → remove from owning section. Track `_remove_per_section`.
  17. Build `_preserved_per_section` + `_residual_candidates` debug entries.
  18. Pick `_target_sec_idx` (section with max removes); `section_elem` = that section's element.
  19. **Body item insertion loop (2408-2746)**:
      - Per body item: pick exemplar by role.
      - Chapter-local exemplar lookup via `chapter_local_exemplars[ci]["role_to_xml_idx"][role]` → unique key `{role}__ci{N}__local` (13.7b §4).
      - Section N placeholder fallback: if chapter is section N + status="empty" → use chapter_anchor element as exemplar (`{role}__ci{N}`).
      - Legacy fallback: if role unknown, use chapter_anchor element with key `{role}__ci{N}`.
      - Outer fallback safety: if outer exemplar contains `tbl`, skip (avoid stray table).
      - **Marker rewrite** (`content_only_mode=True`): `strip_marker` → AI marker residual strip → `generate_expected_marker_normalized` + `reattach_marker` → `_rewrite_marker` safety net. Log conflicts.
      - blank_rules + format_rules indent_parts applied.
      - `deepcopy(exemplars[role])` → `_reassign_unique_ids(...)` (1040, mutates id attributes to be unique).
      - `_set_cloned_element_text(new_elem, prefix+clean_text, NS, is_tbl_box)`.
      - tab insertion if `num_tabs > 0`.
      - **Region-aware placement**: if `_ci in chapter_anchors` → insert AFTER anchor in same section (move cursor to new_elem). Cross-section bleed → error. Else `_target_section_id_for_bi` section append.
      - Fallback append: if no chapter context → orphan body item error.
  20. Build `_rewrite_alignment["chapter_split"]` (chapter_objects_direct) + `["marker_rewrite"]` stats.
  21. Write `structure["_marker_rewrite_log"]`, `["_rewrite_alignment"]`, `["_phase2_reattach_result"]`.
  22. **Section dirty marking**: for sections with remove or new appends → `_all_sections[si].mark_dirty()`. `structure["_dirty_marking"]`.
  23. **Final ID reassignment**: `_reassign_all_section_ids(_all_section_elements, counter_start=4_000_000_000)` (1057). `structure["_final_id_reassignment"]`.
  24. Return `HwpxResult(data, success_count, fail_count, errors)`.
- **Action handlers (`_execute_*`)**: status [dead-code] in chapter-route hybrid path. Used only by `generate_hwpx_dynamic` (path also dead — see below). Defined: `_execute_set_cell` (43), `_execute_clear_body` (114), `_execute_set_paragraph_text` (122), `_execute_add_paragraph` (137), `_execute_add_table` (150), `_execute_remove_paragraph` (175), `_adjust_table_columns` (184), `_execute_add_row` (224), `_execute_remove_table` (288), `_execute_insert_paragraph` (306), `_execute_clone_paragraph` (341). Dispatcher `ACTION_HANDLERS` (385). `_sort_actions` (415).
- **`_sort_actions(...)` (415)** [legacy]: Order phases p1 nonstructural → p2 remove (high idx first) → p3 insert (high idx first) → p4 clear → p5 append. Consumed only by `generate_hwpx_dynamic`.
- **`_clear_unmodified_fields(...)` (456)** [legacy]: Reads `structure["paragraphs"]` `description` containing "고정 텍스트" or "수정 불필요" to skip; otherwise sets `doc.paragraphs[idx].text = ""` for non-modified paragraphs and clears non-modified table cells. Consumed only by `generate_hwpx_dynamic`.
- **`generate_hwpx_dynamic(...)` (503)** [legacy]: Action-list flow for older AI-emits-actions design. Consumed by `routers/files.py:968 generate_hwpx_dynamic_endpoint`. **Still imported (line 995 of files.py imports `assemble_hwpx_hybrid` for this endpoint, not `generate_hwpx_dynamic`)** — actually the endpoint at line 968 imports `assemble_hwpx_hybrid`. `generate_hwpx_dynamic` function itself is not imported anywhere in active code paths. status: [dead-code].
- **`assemble_hwpx(...)` (600)** [legacy]: v2 role-based assembly (style_catalog + role_map + content). Consumed by `routers/files.py:1868` in a different older endpoint flow. status: [legacy] — still callable but not on the chapter-route pipeline.
- **`_reassign_unique_ids(elem, counter)` (1040)** [active]: per-clone id reassignment. Called inside body insertion loop with `_assembly_id_counter = [3_000_000_000]`.
- **`_reassign_all_section_ids(section_elements, counter_start=4_000_000_000)` (1057)** [active]: final pass. Called once at end. Writes `structure["_final_id_reassignment"]`.
- **`_strip_secpr/_strip_linesegarray/_strip_document_ctrls`** [active]: called on every exemplar deepcopy.
- **`_set_element_text(para, text, NS)` (2892)** [active]: header text replacement (table-aware via para.tables; fallback to XML direct).
- **`_set_cloned_element_text(elem, text, NS, is_table_box)` (2947)** [active]: deepcopied exemplar text replacement (tbl → first row last/first tc; container/drawText → biggest drawText subList; fallback → `_replace_text_in_paragraph_elem`).
- **`_replace_text_in_paragraph_elem(p_elem, text, NS)` (3001)** [active]: writes adapted_title back into chapter title element. **13.7d-fix**: iterates all descendant t elements (including inside table cells), writes first one and blanks the rest; removes redundant runs without ctrl/tbl.
- **Returns**: `HwpxResult(data=bytes, success_count, fail_count, errors)`.
- **Outputs**:
  - `result.data` (bytes) — to caller (A17 → A19).
  - `result.success_count` — A19 status message.
  - `result.fail_count` — A19 status message.
  - `result.errors` — A19 `_debug_payload["assembly"]["errors"]`.
- **Cache/debug**: writes diag files `/tmp/hwpx_debug/_d00..._d04` + `/tmp/hwpx_debug/17_assembly_anchor_debug.json`.
- **Mutation/side effects**: extensive — modifies HwpxDocument XML in place; writes back to `structure["_marker_rewrite_log"]`, `["_rewrite_alignment"]`, `["_phase2_reattach_result"]`, `["_dirty_marking"]`, `["_final_id_reassignment"]`.
- **Failure/fallback path**: per-item try/except (errors.append). placement_failure → chapter-level body skip + errors. cross_section_bleed → body item skip + errors. No raise from assemble itself except on HwpxDocument.open failure.
- **Audit note**:
  - `chapter_anchors` cursor update logic: each successful body insert sets `chapter_anchors[ci] = new_elem` so next body item is inserted after the previous body item (preserves order within chapter).
  - adapted_title application path goes through `_replace_text_in_paragraph_elem` (3001) which handles table-cell title boxes — important for templates with title in table.
- **Confidence**: high

---

## A19 — Debug finalization

### A19 [debug-only]

- **Location**: `/tmp/dbtool_dump.py:3363-end-of-function`
- **When called**: After A17 assembly succeeds. Wrapped in try/except blocks per debug block — failure of any block does NOT affect pipeline output.
- **Purpose**: Produce final `_debug_payload`, run a few late debug-only AI calls (12.2 target_unit_planning re-attempt if cache miss, 12.0 template_unit_observation re-attempt if cache miss), dump to disk.
- **Inputs**: `_debug_payload` (var, mutated throughout pipeline), `structure`, `_cached`, `_cache_key`, intermediate state.
- **Sub-steps**:
  1. **12.2 Target Unit Planning Debug (3363-3452)** [duplicate of A8]:
     - Read `structure.get("target_unit_plan")`, check `is_plan_cache_valid(...)` (target_unit_planner.py:584).
     - **If miss** (cache invalid): re-call `propose_template_regions(structure, _cached, _tup_unit_obs)` (28) → `build_target_unit_planning_prompt(...)` (337) → `_call_llm(tag "hwpx_target_unit_planning"/"_retry")` → `parse_target_unit_plan_from_llm(...)` (376) → `validate_target_unit_plan(...)` (427) → `build_plan_cache_payload(...)` (594). Cache write-back via `load_template_cache(_cache_key, namespace='full')` + edit + `save_template_cache(...)`.
     - **Pipeline fit context**: `_tup_pipeline_ctx` from `_source_split_log` if available, else from `chapters`.
     - `compute_legacy_comparison(_tup_plan, _tup_pipeline_ctx)` (530).
     - `assemble_planning_debug(proposal=..., ai_plan=..., validation=..., legacy_comparison=..., unit_observations=..., derived_mode_label=..., paragraph_count=..., cache_status=..., ai_call_info=...)` (609) → `_debug_payload["target_unit_planning"]`.
     - **Audit**: This is a duplicate of A8's planner call — if A8 already cached, this block is fast read; if cache miss for some reason, it can run a fresh AI call here. Late path is wasteful when A8 already succeeded.
  2. **12.1 Marker Roundtrip Readiness (3454-3498)** [debug-only]:
     - Read `structure["_marker_rewrite_log"]` (written by A18 assembly).
     - `extract_marker_policies(structure.get("paragraphs", []), marker_policy_1f=structure.get("marker_policy_1f"))` (hwpx_analyzer.py:7490).
     - If `structure._phase2_reattach_result` present (content_only_mode=True path): build inline summary `{schema_version: 2, phase: "content_only_reattach", reattach_applied_count, ai_marker_residual_count, rewrite_conflict_count, rewrite_conflicts, chapter_title_rewrite_count, body_rewrite_conflict_count, normalization_applied_count}`.
     - Else: `build_marker_roundtrip_debug(body_items, marker_policies, marker_rewrite_log, derived_mode_label)` (marker_separator.py:380).
     - Writes `_debug_payload["marker_roundtrip_readiness"]`.
  3. **11.2 Style Profile (3500-3504)** [DISABLED]: `pass` only — "Style profile AI calls disabled to reduce latency" per comment. Block kept structurally for future re-enable. status: [dead-code in current flow]. Confidence: high (deliberately disabled).
  4. **12.0 Template Unit Observation (3507-3630)** [debug-only]:
     - Check `is_cache_valid(_tuo_cached)`.
     - **If miss**: `extract_template_unit_features(structure, _cached)` → `build_template_unit_prompt(...)` → `_call_llm(tag "hwpx_template_unit_observation"/"_retry")` → `parse_template_unit_observation_from_llm(...)` → `validate_unit_observation(...)` → `derive_mode_label(...)` → `build_cache_payload(...)`. Cache write-back via `load_template_cache(_cache_key, 'full')` + edit + `save_template_cache(...)`.
     - `compute_pipeline_fit(...)` from `_source_split_log` if available.
     - `assemble_observation_output(...)` → `_debug_payload["template_unit_observation"]`.
  5. **Final dump (3632-3638)**:
     - `with open(_dump_path, "w", encoding="utf-8") as _f: json.dump(_debug_payload, _f, ...)` → `/tmp/hwpx_debug_last.json` (set as `_dump_path` earlier in DB tool).
     - `write_stage_debug_files(_debug_payload)` (hwpx_analyzer.py:8439) → wipes `/tmp/hwpx_debug/*.json` (note: `_d00..._d04` diag files written by A18 are NOT json under glob — they ARE json but written before this — actually `write_stage_debug_files` does `glob("*.json")` and `os.remove(...)` so A18's diag files WILL be wiped by this call. The diag files are useful only between A18 and A19 — they get cleared by A19's `write_stage_debug_files` call). Splits payload into `01_template_paragraph_analysis.json`, `02_level_parent_tree.json`, `03_role_clustering.json`, etc. Returns status dict (logged).
  6. **Step 5 result message (3640-3663)**:
     - `step_content = f"성공 {result.success_count}개, 실패 {result.fail_count}개, 크기 {len(result.data):,} bytes" + errors_block`.
     - If `debug` flag: extract result XML preview via `HwpxDocument.open(io.BytesIO(result.data)).paragraphs[0].element.getparent()` → `etree.tostring(...)[:30000]` → markdown details block.
     - `_debug_add("Step 5: HWPX 생성 결과", step_content)`.
- **Outputs**:
  - `_debug_payload` final dict — written to `/tmp/hwpx_debug_last.json` (consumed by external review/clients).
  - `/tmp/hwpx_debug/*.json` files (split debug — consumed by humans + tooling).
  - Step 5 status message.
- **Cache/debug**: see sub-steps. Late `propose_template_regions` / `extract_template_unit_features` cache write-back if cache was missed earlier.
- **Mutation/side effects**:
  - `structure["target_unit_plan"]` mutated if cache miss (3409).
  - `structure["template_unit_observation"]` mutated if cache miss (3575).
  - Cache file mutated via `save_template_cache(...)`.
  - Files in `/tmp/hwpx_debug/` deleted then rewritten.
- **Failure/fallback path**: each block in its own try/except. Failures logged + `_debug_payload[key] = {"error": ..., "debug_only": True}`. No raise that would block result delivery.
- **Audit note**:
  - **Duplication risk**: The 12.2 block re-runs `propose_template_regions` + LLM call if cache is missed despite A8 having attempted the same. Typically only one or the other runs, not both — but if A8 attempt failed without populating cache write-back, A19 retries the same call.
  - **`write_stage_debug_files` clears `/tmp/hwpx_debug/*.json`**: the assembly diag files `_d00..._d04` ARE removed by this call at the end. They're only inspectable mid-run via `tail -f` or fast snapshot. Confirmed by reading `write_stage_debug_files(...)` glob+remove at hwpx_analyzer.py:8456.
  - 11.2 Style Profile block currently no-op — the `try/except` exists but body is `pass`.
- **Confidence**: high

---

## Unresolved references — Part A11~A19

### outputs I produced but couldn't find consumer for
- `_chapter_trees` (list appended in A13) — `assemble_hwpx_hybrid` no longer takes `chapter_trees` kwarg (removed in 13.7a-A1). Only persists in `_chapter_trees` local list with no downstream read. Effectively dead-write after 13.7a-A1.
- `_chapter_plan_debug["source_diagnostic"]` (A14) — written into `_debug_payload["chapter_template_plan"]` debug only; no policy consumer.
- `_debug_payload["title_role_consistency"]` (A15) — debug only, no policy consumer.
- `_debug_payload["multi_section_diagnostic"]["gate_decision"]` (A17 step 3) — observation only, `section_aware_assembly_needed` flag is not read by `assemble_hwpx_hybrid` as a hard gate.
- `_chapter_proc["adapted_title_deferred"]` (set in `_process_chapter_objects` line 802) — appended per chapter with `{chapter_idx, source_chapter_idx, adapted_title_in_title_item}` but the actual adapted_title apply is performed inside `assemble_hwpx_hybrid` chapter_anchors loop (line 1689) from `_ch_obj._debug.adaptation_decision.adapted_title`, NOT from `adapted_title_deferred` list. This list is logged but not consumed by the apply path.
- `_residual_candidates` / `_preserved_per_section` (built in `assemble_hwpx_hybrid` lines 1931-1966) — built but no downstream code reads them in A18.

### inputs I read but couldn't find producer for
- `_source_split_log` referenced via `'_source_split_log' in dir()` in A19 step 1 (line 3423) — producer would be earlier source-split block (likely Agent 2 scope A6 `split_source_by_chapters`). Not in A11~A19 scope.
- `_cached` referenced via `'_cached' in dir()` (A19 line 3374, 3527) — producer is initial cache load (Agent 1 / Agent 2 scope).
- `_dump_path` (A19 line 3633) — producer is earlier DB tool setup (Agent 1 scope).
- `__event_emitter__` status callbacks (A13 line 2329, 2536; A17 line 3237) — caller-provided.
- `_call_llm` (used throughout A12/A13/A16/A19) — defined by DB tool wrapper around `chat.generate_chat_completion`; producer is DB tool boilerplate (Agent 1 scope).
- `structure["template_grammar"]`, `structure["chapter_types"]`, `structure["role_text_types"]`, `structure["per_type_role_semantics"]`, `structure["exclusive_rules"]`, `structure["format_rules"]`, `structure["marker_policy_1f"]` — populated by earlier 1d/1e/1f phases (Agent 2 scope).
