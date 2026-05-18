# Pipeline audit — Part A3~A10

Scope: phases A3 through A10 of the HWPX generation pipeline. Documents the actual code behavior as of the current worktree state (commit `f5f49d0`).

Source files:
- `/tmp/dbtool_dump.py` (DB tool `generate_document_hwp_local` body, the orchestrator)
- `/home/sprint/2026_capstone_jeonbuk/.claude/worktrees/hwp_gen/app/backend/open_webui/utils/hwpx_analyzer.py` (function implementations)
- `/home/sprint/2026_capstone_jeonbuk/.claude/worktrees/hwp_gen/app/backend/open_webui/utils/source_block_adapter.py` (13.0 + 13.3 helpers)
- `/home/sprint/2026_capstone_jeonbuk/.claude/worktrees/hwp_gen/app/backend/open_webui/utils/target_unit_planner.py` (legacy AI planner)

Out of scope: A1+A2 (1a~1f) covered by Agent 1; A11~A19 (13.7c + 2b + assembly) covered by Agent 3.

---

## A3 — Phase E (TOC-based chapter planner)

DB tool block: `/tmp/dbtool_dump.py:1335-1488`.

### A3.1 `diagnose_1c_non_body_handling` — [active]
- Location: `hwpx_analyzer.py:16265`.
- When called: immediately before the Phase E cache/AI gate. Runs unconditionally (not gated by `has_toc`).
- Purpose: measure how the 1c result handled non-body paragraphs (Track D-2 instrumentation).
- Inputs:
  - `section_results` (var `section_results`, dict from 1a~1f cache/build, type `dict[int|str, sresult]`).
  - Internal sets: `_NON_BODY_CONTAINER_ROLES`, `_NON_BODY_LEAF_ROLES` (Track D-2 split).
- Outputs:
  - Local var `_pe_one_c_diag` (dict with keys `per_section`, `summary`, `samples`).
  - Consumed by: Phase E debug payload assembly (line 1366, 1460, 1469, 1479). **Actually consumed: yes** (debug-only — never feeds production decisions).
- Cache/debug: stored as `_debug_payload["phase_e_chapter_planner"]["one_c_diagnostic"]`.
- Mutations: none on inputs.
- Failure path: wrapped by outer Phase E try/except (line 1483); falls back to `_debug_payload["phase_e_chapter_planner"] = {"error": ..., "debug_only": True}`.
- Audit note: case A counts only `_NON_BODY_LEAF_ROLES` as parent (Track D-2 fix removes false positives from `appendix_title` etc.).
- Confidence: high.

### A3.2 multi-section guard `_section_results_for_phase_e` — [active]
- Location: dbtool `1353-1359`.
- Purpose: per the 2026-05-17 user policy, multi-section templates only analyze section 0 through Phase E.
- Inputs:
  - `section_results` (dict).
  - `_section_count`, `_is_multi_section` (locally computed).
- Outputs:
  - `_section_results_for_phase_e` — narrowed dict `{0: section_results[0]}` when multi-section, else passthrough.
  - Consumed by: TOC gate / body assembly below.
  - **Actually consumed: yes**.
- Cache/debug: flagged in `_debug_payload["phase_e_chapter_planner"]["section_0_only_due_to_multi_section"]` (cache path only).
- Confidence: high.

### A3.3 `has_toc_gate` — [active]
- Location: `hwpx_analyzer.py:15699`.
- When called: in dbtool line `1373`, only if cache miss (no `_cached_phase_e`).
- Purpose: weak detection of TOC paragraphs (false-negative-averse). Hit triggers: role in `{"table_of_contents", "toc"}` OR text matching `_TOC_TEXT_HINT_PATTERNS` (12 regex incl. `차례`/`목차`/`Contents`).
- Inputs:
  - `_section_results_for_phase_e` (dict).
- Outputs (dict):
  - `has_toc` (bool), `toc_paragraph_hints` (list of `{section_id, local_idx, role, text_preview, hit_by}`), `detection_method` (`role|text|both|none`), `scanned_section_count`, `scanned_paragraph_count`.
  - Stored as `_pe_gate` (local var).
  - Consumed by: Phase E AI gate, debug payload (`gate` key).
  - **Actually consumed: yes**.
- Cache/debug: stored under `_debug_payload["phase_e_chapter_planner"]["gate"]`.
- Confidence: high.

### A3.4 `build_toc_based_chapter_plan_prompt` — [active]
- Location: `hwpx_analyzer.py:15963` (prompt body at `15790` constant `TOC_BASED_CHAPTER_PLAN_PROMPT`).
- When called: dbtool line `1429`, only when `has_toc=True` and cache miss.
- Purpose: build 2-message prompt — system (TOC interpretation rules) + user (JSON payload with toc/body/1c-tree).
- Inputs:
  - `_pe_toc` (list of dicts, toc paragraphs with full text), `_pe_body` (dict by section_id of body paragraphs with truncated text, `max_body_text_preview=200`), `_pe_tree` (dict by section_id of `{local_idx, level, parent_idx}`).
- Outputs:
  - `_pe_msgs` (list of 2 messages).
  - Consumed by `_call_llm` immediately.
  - **Actually consumed: yes**.
- Confidence: high.

### A3.5 `parse_toc_based_chapter_plan_from_llm` + retry loop — [active]
- Location: parser at `hwpx_analyzer.py:16054`.
- When called: dbtool `1441` (initial), `1441` again on retry (max 1 retry).
- Inputs:
  - `_pe_raw` (LLM raw response string).
- Outputs:
  - `_pe_parsed` (dict); if `parse_error` key present, retry triggered.
- Failure path: after 2 attempts, sets `_pe_plan = None` and emits status `ai_call_failed`.
- Confidence: high.

### A3.6 `validate_toc_based_chapter_plan` — [active]
- Location: `hwpx_analyzer.py:16078`.
- When called: dbtool `1453`, only after successful parse.
- Purpose: schema field + `paragraph_ref` existence checks; downgrade invalid refs to ambiguity instead of dropping plan.
- Inputs:
  - `_pe_plan` (parsed dict), `_pe_all_idxs` (dict by `section_id -> set[int]` of valid local_idx).
- Outputs:
  - `_pe_validated` (mutated `_pe_plan` with `validation_result`).
  - Status derived: `validation_fallback` if `fallback_required` else `ok`.
- Mutations: writes `_validation_flags` to invalid units; downgrades evidence with bad refs to confidence `low`.
- Confidence: high.

### A3.7 Phase E cache hit branch — [active]
- Location: dbtool `1361-1371`.
- Purpose: if cache had `_cached_phase_e`, skip AI call. Loaded payload merged with fresh `_pe_one_c_diag` (1c diagnostic is always recomputed against the current section_results).
- Inputs: `_cached_phase_e` (from `_cached.get("phase_e_chapter_planner")` at dbtool line 494), `_from_cache`.
- Outputs:
  - `_debug_payload["phase_e_chapter_planner"]` filled with cached values + `loaded_from_cache: True`.
  - `_phase_e_skipped_by_cache = True` (local flag, consumed by cache write-back path at line 1592).
- Confidence: high.

### A3.8 `run_phase_e_chapter_planner` — [dead-code]
- Location: `hwpx_analyzer.py:16441`.
- When called: NEVER from dbtool. dbtool inlines the orchestration (has_toc_gate + build_prompt + parse + validate) directly.
- Inputs: `section_results, call_llm, log_obj, task_name`.
- Outputs: dict shaped like `_debug_payload["phase_e_chapter_planner"]`.
- Audit note: the function exists in `hwpx_analyzer.py` but no caller imports it. `grep "run_phase_e_chapter_planner"` in dbtool returns zero hits. The dbtool block at `1340-1346` imports `has_toc_gate, build_toc_based_chapter_plan_prompt, parse_toc_based_chapter_plan_from_llm, validate_toc_based_chapter_plan, diagnose_1c_non_body_handling` but NOT `run_phase_e_chapter_planner`. Likely a refactor target left as unused. **Status: dead code in production path.**
- Confidence: high.

---

## A4 — Track C (chapter pattern family analysis)

DB tool block: `/tmp/dbtool_dump.py:1491-1575`.

### A4.1 cache hit branch + gate `_tc_pe_status == "ok"` — [active]
- Location: dbtool `1496-1506`.
- Purpose: skip Track C unless Phase E status is `"ok"`. Cache hit replays cached Track C unchanged.
- Inputs:
  - `_debug_payload["phase_e_chapter_planner"]["status"]` (set by A3).
  - `_cached_track_c` (from cache schema v6+, set at line 495).
  - `_from_cache`.
- Outputs:
  - `_debug_payload["chapter_pattern_family"]` filled either with cached payload + `loaded_from_cache: True`, or `status: "skipped"` when Phase E status not ok.
  - `_track_c_skipped_by_cache` (bool, consumed implicitly by control flow below).
- Confidence: high.

### A4.2 `extract_generation_unit_subtrees` — [active]
- Location: `hwpx_analyzer.py:16624`.
- When called: dbtool `1518`, only on cache miss + Phase E ok.
- Purpose: code-side fact extraction for each `generation_units[i]`. Pulls subtree paragraphs from `section_results` matching the unit's `idx_range`, builds `structural_summary` (depth, role_distribution, marker_set, level_distribution, direct_children_count). Truncates subtree paragraph list to 50 for token budget. Per §22, no similarity computation, no family judgment — pure fact extraction.
- Inputs:
  - `_tc_pe_plan` (Phase E `toc_plan` dict from `_debug_payload["phase_e_chapter_planner"]["toc_plan"]`).
  - `_tc_section_results` (defaults to `_section_results_for_phase_e`, so section 0 only when multi-section).
- Outputs:
  - `_tc_subtrees` (list of unit fact dicts).
  - Consumed by AI prompt + validate.
  - **Actually consumed: yes**.
- Failure path: if `not _tc_subtrees`, status `no_generation_units` set on debug payload.
- Confidence: high.

### A4.3 `build_chapter_pattern_family_prompt` + AI call + parse — [active]
- Location: prompt builder `hwpx_analyzer.py:16819`, parser `16833`.
- When called: dbtool `1520-1537`, AI loop with max 1 retry.
- Purpose: AI judges which generation units form families with shared structural skeleton (markers, roles, depth). Confidence-aware. Expandable=true is suggestion only; downgraded by validation when confidence != high.
- Inputs:
  - `_tc_subtrees`.
- Outputs:
  - `_tc_plan` dict (with `pattern_families`, `non_grouped_units`, `ambiguity_flags`) or None on AI failure.
- Confidence: high.

### A4.4 `validate_chapter_pattern_family` — [active]
- Location: `hwpx_analyzer.py:17087`.
- When called: dbtool `1539`, only after parse success.
- Purpose: prune invalid member indices; force `expandable=false` when `confidence in {"medium","low"}` (conservative safety net); flag singleton families with `singleton_family_weak_evidence`.
- Inputs:
  - `_tc_plan`, `n_units = len(_tc_subtrees)`.
- Outputs:
  - `_tc_validated` (mutated plan + `validation_result`).
- Mutations: writes `_validation_flags` per family; rewrites `members` and `non_grouped_units` to remove invalid refs.
- Confidence: high.

### A4.5 Track C consumption — [debug-only]
- Where Track C result is read after this block: ONLY in A5 `_phase_e_to_chapter_types` (dbtool `1610`), which uses Track C's `pattern_families` for type grouping in the rewritten `chapter_types`. So Track C IS consumed downstream via the chapter_types overwrite — not strictly debug-only.
- Audit note: MEMORY.md labels Track C as "debug-only", but inspection of `_phase_e_to_chapter_types` (`hwpx_analyzer.py:17021-17031`) shows `track_c_result.get("status") == "ok"` is used to build `family_map` which determines whether multiple generation units collapse into the same `chapter_types` key. So Track C output affects production `chapter_types` topology — at minimum `merged_chapter_count` and `_phase_e_family_id` per type. Whether downstream code branches on these is determined by A6+ consumers.
- Confidence: medium (need downstream consumer audit to know if production effect is "real").

---

## A5 — Phase E + Track C cache integration + `chapter_types` PRODUCTION overwrite

DB tool block: `/tmp/dbtool_dump.py:1578-1640`.

### A5.1 Cache write-back — [active]
- Location: dbtool `1592-1605`.
- Trigger: `not _phase_e_skipped_by_cache` (i.e. fresh AI call happened — could be cache miss OR cache hit but phase_e was absent from cache).
- Inputs:
  - `_pe_final = _debug_payload["phase_e_chapter_planner"]` (loaded_from_cache flag stripped before save).
  - `_tc_final = _debug_payload["chapter_pattern_family"]` (loaded_from_cache flag stripped).
- Outputs: cache JSON at `_cache_key` (namespace `full`) gets two new top-level keys:
  - `cache_data["phase_e_chapter_planner"] = _pe_for_cache`
  - `cache_data["chapter_pattern_family"] = _tc_for_cache`
- Side effects: `save_template_cache(_cache_key, _cache_data)`.
- Confidence: high.

### A5.2 `_phase_e_to_chapter_types` PRODUCTION overwrite — [active]
- Location: `hwpx_analyzer.py:16988`; called at dbtool `1609`.
- Trigger: `_pe_final.get("status") == "ok"`.
- Purpose: produce a new `chapter_types` dict from Phase E `generation_units` + Track C `pattern_families`. Maps:
  - Track C family members -> same `type_N` key.
  - Non-grouped units -> `singleton_N` then renamed to `type_N`.
  - `title_role` derived from first unit's `paragraph_ref` -> 1d role lookup (`canonical_role` or `role`).
  - `description` = first unit's `title_text`.
  - `pattern` = `{}` (legacy field — replaced by 13.6 per_chapter_pattern downstream).
  - Per-type metadata: `merged_chapter_count`, `_phase_e_source: True`, `_phase_e_family_id`, `_phase_e_member_unit_indices`.
- Inputs:
  - `_pe_final` (Phase E debug payload), `_tc_final` (Track C debug payload), `structure`.
- Outputs (in-memory only — cache NOT updated here):
  - `_new_chapter_types` -> local var.
  - `chapter_types = _new_chapter_types` (rebinds outer local).
  - `structure["chapter_types"] = _new_chapter_types` (mutation).
  - `section_results[0]["chapter_types"] = _new_chapter_types` (mutation, if section 0 present).
  - `section_results[0]["structure"]["chapter_types"] = _new_chapter_types` (mutation, if section 0 structure dict present).
  - `_debug_payload["chapter_types_phase_e_production"] = {"overwritten": True, "legacy_keys": [...], "new_keys": [...], "new_count": int}`.
- Mutations: 4 separate writes to `chapter_types`/`structure`/`section_results` (in-memory). cache for `chapter_types` is NOT updated — next cache-hit run replays the legacy chapter_types and then overwrites again on the fly.
- Failure path: bound by try/except (line 1635). On failure: `_debug_payload["chapter_types_phase_e_production"] = {"error": ..., "overwritten": False}` and the legacy chapter_types stays.
- Audit note: when Phase E status != "ok", debug payload records `reason: "phase_e status=... — legacy chapter_types 유지"` and the legacy 1c-derived chapter_types remains. This is the dispatcher seam between Phase E and legacy.
- Confidence: high.

### A5.3 Keys written/overwritten — summary
- Cache (file): `phase_e_chapter_planner`, `chapter_pattern_family` (added top-level).
- In-memory (not in cache):
  - `chapter_types` (rebind + 3 nested writes).
  - `structure["chapter_types"]`.
  - `section_results[0]["chapter_types"]`.
  - `section_results[0]["structure"]["chapter_types"]`.
- Debug payload keys added:
  - `phase_e_chapter_planner` (A3).
  - `chapter_pattern_family` (A4).
  - `chapter_types_phase_e_production` (A5.2).
- Confidence: high.

---

## A6 — 2a chapter_classify

DB tool block: `/tmp/dbtool_dump.py:1690-1753`.

### A6.1 `extract_header_roles` — [active]
- Location: `hwpx_analyzer.py:9375`.
- When called: per MEMORY notes, the DB tool imports `extract_header_roles` (and `header_roles = extract_header_roles(structure)` is the intended invocation). However in the dump at line 1697-1717, the dbtool re-implements the same logic INLINE rather than calling `extract_header_roles`. The function exists in `hwpx_analyzer.py` and the import is documented as a customization, but the dump shows inline computation. **Status: inline duplicate in current dump; the analyzer function is still importable.**
- Purpose: extract level-0 roles seen BEFORE the first chapter title; exclude chapter `title_role`s. Returns list of `{role, description}` dicts (analyzer version) or plain role strings (inline version).
- Inputs:
  - `structure` (or `chapter_types` + `paragraphs` for inline).
- Outputs:
  - `header_roles` (list of strings, in dump). Consumed by `build_chapter_classify_prompt(..., header_roles=header_roles, ...)`.
  - **Actually consumed: yes** (passed to prompt).
- Audit note: dump and MEMORY.md disagree — MEMORY claims DB tool calls `extract_header_roles`, but `/tmp/dbtool_dump.py:1697-1717` shows inline computation producing `list[str]`. The analyzer function `extract_header_roles` accepts `structure` and returns `list[dict]`. The prompt builder accepts both shapes.
- Confidence: high (verified by reading both code paths).

### A6.2 `build_chapter_classify_prompt` — [active]
- Location: `hwpx_analyzer.py:9421` (system prompt `CHAPTER_CLASSIFY_PROMPT` at `8259`).
- When called: dbtool `1719`.
- Purpose: build 2a prompt — system rules + user payload containing chapter type catalog, header role list, source PDF text / images, optional user instructions.
- Inputs:
  - `chapter_types` (Phase-E-overwritten or legacy).
  - `header_roles` (list[str], inline-computed).
  - `content_text`, `content_images`, `pdf_text_content`, `template_grammar`, `paragraphs`.
- Outputs:
  - `messages_2a` (list of messages).
- Confidence: high.

### A6.3 `parse_chapter_classify_from_llm` — [active]
- Location: `hwpx_analyzer.py:9550`.
- When called: dbtool `1726`.
- Purpose: parse 2a JSON. Returns `{"chapters": [...], "header": {...}}`. Empty keys default to empty list/dict.
- Inputs:
  - `llm_content_2a` (LLM raw string).
- Outputs:
  - `classify_result` dict.
  - `chapters = classify_result["chapters"]` (consumed by A7 split, A10 shallow, A11+ chapter loop).
  - `header_data = classify_result["header"]` (consumed by content_data assembly in A10 / A11).
  - **Actually consumed: yes** (both keys feed downstream).
- Failure: raises `ValueError` if no JSON found or top-level not dict.
- Cache/debug: stored in `_debug_payload["chapter_classify"]` with keys `prompt_messages`, `llm_raw_response`, `header_roles`, `chapters`, `header_data`.
- Confidence: high.

---

## A7 — `split_source_by_chapters` + source_blocks adapter + role catalog

DB tool block: `/tmp/dbtool_dump.py:1755-1805`.

### A7.1 `split_source_by_chapters` — [active]
- Location: `hwpx_analyzer.py:1152`.
- When called: dbtool `1761`, only if `pdf_text_content` truthy.
- Purpose: locate each 2a chapter title in the source text using 3-stage matching (exact substring -> whitespace-fuzzy regex -> core-keyword), then carve source into per-chapter sections.
- Inputs:
  - `pdf_text_content` (raw source text from PDF or content_text upload).
  - `chapter_titles_list` (built at line 1759 from `chapters`).
- Outputs:
  - `source_sections` (list[str], same length as `chapters`). When a title isn't found, falls back to the full text (`fallback_used=True` flag in log).
  - `_source_split_log` (decision dict — per-chapter `match_method`, positions, chunk lengths, `source_concentration_ratio`, etc).
  - `_debug_payload["source_split_decision"] = _source_split_log` (line 1765).
- Consumers of `source_sections`:
  - Used by per-chapter source carving in A11+ (chapter loop). **Actually consumed: yes** in 2a-driven path. Template-driven path may use `_broad_source` instead (see A8).
- Failure path: empty `pdf_text_content` -> `source_sections = [""] * len(chapters)`, `_source_split_log = None`.
- Confidence: high.

### A7.2 `text_blob_to_source_blocks` — [debug-only]
- Location: `source_block_adapter.py:24`.
- When called: dbtool `1770`, only if `_source_text_for_blocks` truthy.
- Purpose: 13.0 debug-only adapter — split source into typed blocks via 3 heading patterns (markdown `#`, Korean roman numerals, arabic `1.`). Falls back to single broad block if no headings found.
- Inputs:
  - `_source_text_for_blocks = pdf_text_content or content_text or ""`.
- Outputs:
  - `_source_blocks` (list[dict] with `source_block_id, content, order_index, heading_path`).
  - Written to `_debug_payload["source_blocks"] = {"block_count", "source_length", "blocks": _source_blocks}` only.
  - **Actually consumed: no** — no downstream code reads `_source_blocks` or `_debug_payload["source_blocks"]`. Pure observation.
- Confidence: high.

### A7.3 `_extract_texts_by_idx` (text only, debug input) — [active]
- Location: `hwpx_analyzer.py:1797`.
- When called: dbtool `1781`, only if `truncated_xml` truthy.
- Purpose: extract paragraph texts keyed by `_idx` from the truncated XML (max 80 chars per default — but here uses default `max_chars=80`).
- Inputs:
  - `truncated_xml`.
- Outputs:
  - `idx_texts` (dict `int -> str`).
- Consumers: full role catalog construction (next sub-step).
- Confidence: high.

### A7.4 `full_role_catalog` construction — [active]
- Location: dbtool `1785-1795`.
- Purpose: per-role catalog entry (`description, marker, level, sample`) — feeds 2b prompt.
- Inputs:
  - `structure.paragraphs`, `idx_texts`.
- Outputs:
  - `full_role_catalog` (dict `role_name -> {description, marker, level, sample}`).
- Consumers: shallow path (A10) and chapter path (A11+) prompts.
- Confidence: high.

### A7.5 `_collect_roles` inner helper — [dead-code]
- Location: dbtool `1797-1804`.
- Purpose: recursively flatten role names from a pattern dict.
- Audit: defined but never invoked in this part of the dbtool. Likely was used by a removed code path. **Status: dead.**
- Confidence: high.

---

## A8 — 13.7e early `target_unit_planning`

DB tool block: `/tmp/dbtool_dump.py:1807-1858`.

### A8.1 gate — [active]
- Location: dbtool `1816-1818`.
- Trigger: `structure["target_unit_plan"]` either missing or has empty `regions`. Effectively forces a fresh build whenever the early-pass cache hasn't populated it yet.
- Inputs:
  - `structure["target_unit_plan"]` (may exist from cache; cache schema v5 stores it under `_cached["structure"]["target_unit_plan"]`).
- Confidence: high.

### A8.2 `propose_template_regions` — [active]
- Location: `target_unit_planner.py:28` (imported at dbtool `273`).
- When called: dbtool `1821`.
- Purpose: code-side proposal of candidate template regions from the analyzed structure + Phase 12.0 unit observations. Feeds the legacy AI planner.
- Inputs:
  - `structure`, `_cached if '_cached' in dir() else None`, `_tuo_obs_e` (from `structure["template_unit_observation"]["unit_observations"]`).
- Outputs:
  - `_proposal_e` (proposal dict).
- Consumers: prompt builder.
- Confidence: high (location verified, not deep-inspecting since out of scope).

### A8.3 `build_target_unit_planning_prompt` + `parse_target_unit_plan_from_llm` + `validate_target_unit_plan` + `build_plan_cache_payload` — [active]
- Location: `target_unit_planner.py:337, 376, 427, 594`.
- When called: dbtool `1822-1836` (with 1 retry on parse failure).
- Purpose: legacy AI target unit planner — produces `regions` list with `unit_type in {chapter, slot, attachment, shallow_block}` and `paragraph_indices`.
- Inputs:
  - `_proposal_e`, `structure.paragraphs`, `_tuo_obs_e`.
- Outputs:
  - `_parsed_e` (raw AI dict or None).
  - `_val_e` (validation dict).
  - `_plan_e` (cache payload schema — wraps ai_plan + validation + planner_version).
  - Stored at `structure["target_unit_plan"] = _plan_e` (line 1837, in-memory).
- Confidence: high.

### A8.4 cache update (load -> mutate -> save) — [active]
- Location: dbtool `1839-1854`.
- Inputs: `_cache_key`, `_plan_e`.
- Outputs:
  - `_wb_e["structure"]["target_unit_plan"] = _plan_e`.
  - `_wb_e["section_results"][0]["structure"]["target_unit_plan"] = _plan_e` (cache schema v5 sync, if section 0 present).
  - `save_template_cache(_cache_key, _wb_e)`.
- Failure: log warning only — no exception propagation.
- Audit note: This is the ONLY path where `structure["target_unit_plan"]` gets the legacy AI plan into cache. The legacy block at dbtool `3363+` (post-assembly) also calls `propose_template_regions` + `build_target_unit_planning_prompt` etc., but by then `is_plan_cache_valid(_tup_cached)` returns true since A8 already wrote it, so the post-assembly block becomes a cache-hit no-op. The post-assembly path is effectively debug duplication.
- Confidence: high.

### A8.5 Position relative to shallow decision — [active]
- Per the inline comment at dbtool `1807-1815`, A8 was moved to run BEFORE the 13.3 shallow route decision (A10). Previously the planner only ran in the chapter-route fallback path at line 2275 (now renumbered ~3363), so the `should_use_shallow_route` check at line 1926 was getting an empty `target_unit_plan` and always returning False. Fix: A8 runs unconditionally early, so `structure["target_unit_plan"]` is populated by the time A10's `should_use_shallow_route` reads it.
- Confidence: high.

---

## A9 — Phase E -> `target_unit_plan` PRODUCTION overwrite

DB tool block: `/tmp/dbtool_dump.py:1861-1921`.

### A9.1 `build_target_unit_plan_dispatcher_decision` — [active]
- Location: `hwpx_analyzer.py:17068`.
- When called: dbtool `1873`.
- Purpose: pick route. Returns `{"route": "phase_e", "reason": ...}` if Phase E `status == "ok"` and `toc_plan` truthy; else `{"route": "legacy_ai", ...}`.
- Inputs:
  - `_pe_for_tup = _debug_payload["phase_e_chapter_planner"]`.
- Outputs:
  - `_tup_decision` (dict).
- Confidence: high.

### A9.2 `_phase_e_to_target_unit_plan` — [active]
- Location: `hwpx_analyzer.py:16857`.
- When called: dbtool `1881`, only if `_tup_decision["route"] == "phase_e"`.
- Purpose: convert Phase E `generation_units` + `out_of_toc_preserve_regions` to `target_unit_plan`-compatible regions:
  - generation_units -> `unit_type="chapter"`, paragraph_indices expanded from `idx_range` spans (section 0 only).
  - out_of_toc_preserve_regions -> `unit_type="slot"`, paragraph_indices from `paragraph_refs`.
  - Multi-section spans/refs -> recorded in `_multi_section_units_skipped` (not converted, deferred to 13.7b).
- Inputs:
  - `_pe_for_tup`, `structure`.
- Outputs:
  - `_tup_new` (target_unit_plan-shaped dict with `regions, source="phase_e", _phase_e_status, _generation_unit_count, _out_of_toc_count, _multi_section_units_skipped`).
- Confidence: high.

### A9.3 In-memory override (NOT in cache) — [active]
- Location: dbtool `1882-1903`.
- Trigger: route == "phase_e".
- Inputs:
  - `_tup_new`, `_tup_legacy` (saved for comparison).
- Outputs:
  - `structure["target_unit_plan"] = _tup_new` (overwrites A8's legacy AI plan in memory).
  - `section_results[0]["structure"]["target_unit_plan"] = _tup_new` (mutation, if section 0 structure dict present).
  - `_debug_payload["target_unit_plan_phase_e_production"]` populated with `decision, overwritten, legacy_regions_count, new_regions_count, legacy_unit_types, new_unit_types, multi_section_skipped_count, legacy_target_unit_plan_for_compare`.
- Cache: NOT updated. Comment at line 1864: "in-memory만 변경. cache 자체는 그대로 (legacy AI 결과 유지). 매번 실행 시 Phase E 호출 + 변환 + 덮어쓰기 (cache invalidate 회피)." This means every cache-hit run re-runs Phase E (A3 hits its own cache) and then re-derives `_tup_new` and overwrites again.
- Confidence: high.

### A9.4 Dual write path audit
- A8 writes `_plan_e` (legacy AI) to both `structure["target_unit_plan"]` and cache.
- A9 immediately overwrites `structure["target_unit_plan"]` with `_tup_new` (Phase E derived) when route=="phase_e", but does NOT update cache.
- Net effect: on disk, cache always has the legacy AI plan. In memory after A9, structure has the Phase E plan. Next run reloads legacy from cache (at section_results[0]["structure"]["target_unit_plan"]) -> A8 sees `_tup_has_regions=True` -> skips legacy AI rebuild -> A9 overwrites in memory again.
- This means A8's AI call happens only on the FIRST cache miss (or `_tup_has_regions` False). Steady-state: A8 is a no-op (cache hit short-circuits at A8.1), A9 runs every time.
- Audit note: this is intentional per the comment at line 1862-1866. Storing the Phase E result in cache would risk staleness if Phase E logic changes; the current design forces a fresh phase-E->TUP conversion each run.
- Audit note (duplicate): the post-assembly block at dbtool `3363+` (`is_plan_cache_valid(_tup_cached)` check) reads `structure["target_unit_plan"]`. By that point, A9 may have overwritten it with the Phase E shape; `is_plan_cache_valid` would likely return False because `planner_version` is missing on the Phase E shape — so the legacy block might recompute the AI plan and overwrite the cache write-back. To verify, would need to read `is_plan_cache_valid` source. Marking this as a duplicate write path concern: `[uncertain]` whether the post-assembly block can clobber A9's in-memory plan after assembly.
- Confidence: high for A8/A9 description, medium for downstream post-assembly interaction.

---

## A10 — 13.3 Shallow Route decision + shallow path

DB tool block: `/tmp/dbtool_dump.py:1923-2053`.

### A10.1 `should_use_shallow_route` — [active]
- Location: `hwpx_analyzer.py:9730`.
- When called: dbtool `1926`.
- Purpose: detect shallow-flat templates. Returns `(use_shallow, route_debug)`.
- Decision logic:
  - `has_chapter = any(r.unit_type == "chapter")`
  - `has_shallow = any(r.unit_type == "shallow_block")`
  - `shallow_para_count`, `total_body_count` (excludes slot/attachment)
  - `shallow_is_primary = (shallow_para_count > 0 and total_body_count > 0 and shallow_para_count / total_body_count > 0.5)`
  - `use_shallow = not has_chapter and has_shallow and shallow_is_primary`
- Inputs:
  - `_tup = structure.get("target_unit_plan", {})` (the version overwritten by A9 if Phase E succeeded, otherwise A8's legacy AI plan).
- Outputs:
  - `_shallow_route` (bool).
  - `_route_debug` (dict: `has_chapter_regions, has_shallow_regions, shallow_is_primary_body, shallow_para_count, total_body_para_count, route_reason`).
- Audit note: Phase E -> TUP conversion (`_phase_e_to_target_unit_plan`) never emits `unit_type="shallow_block"` — only `"chapter"` and `"slot"`. So after A9, shallow route can only fire if the legacy AI planner had emitted `shallow_block` AND A9 did NOT overwrite (i.e. Phase E was no_toc_deferred / failed). For TOC-bearing templates, shallow route is structurally unreachable post-A9.
- Confidence: high.

### A10.2 shallow region lookup + chapter_type pick — [active]
- Location: dbtool `1930-1953`.
- Trigger: `_shallow_route == True`.
- Purpose: select the first `unit_type="shallow_block"` region; pick first chapter_type for prompt context; walk pattern dict to collect roles.
- Inputs:
  - `_tup`, `chapters` (2a result), `chapter_types`.
- Outputs:
  - `_shallow_region` (dict from `_tup.regions`).
  - `_shallow_ch_type`, `_shallow_type_info`, `_shallow_pattern`, `_shallow_title_role`.
  - `_shallow_pattern_roles` (set, populated by recursive `_walk_pattern`).
  - `_shallow_pi` (paragraph_indices), `_shallow_desc` (description).
- Failure: if no `shallow_block` region OR no chapters -> log warning at line 2049, falls back to non-shallow path (`_shallow_done` stays False).
- Confidence: high.

### A10.3 `extract_shallow_section_plan_seed` — [active]
- Location: `hwpx_analyzer.py:14086`.
- When called: dbtool `1956`, inside shallow-region branch.
- Purpose: 13.3b-1 — code-driven heading candidate extraction for the shallow region, with evidence scoring (E1..E6). Returns seed dict with `headings, primary_heading_role, ...` OR fallback dict with `fallback_reason`.
- Inputs:
  - `_tup`, `structure`, `_idx_full_texts`, `marker_policies=structure.get("marker_policy_1f")`.
- Outputs:
  - `_section_plan_seed_result` (dict).
  - `_has_seed` (bool flag from `bool(seed.get("seed"))`).
- Consumers: shallow 2b prompt builder (line 1980) + `observe_section_plan_compliance` (line 2005).
- Cache/debug: stored at `_debug_payload["shallow_section_plan_seed"]` (line 2016).
- Confidence: high.

### A10.4 `build_shallow_fill_prompt` (or `build_section_fill_prompt` with `shallow_mode=True`) — [active]
- Audit note: dbtool `1968` calls `build_section_fill_prompt(..., shallow_mode=True, section_plan_seed=_section_plan_seed_result, content_only_mode=True, ...)`. NOT `build_shallow_fill_prompt`. So the dedicated `build_shallow_fill_prompt` (hwpx_analyzer.py:9622) is imported (dbtool line 224) but NOT actually called in the shallow path. **Status: `build_shallow_fill_prompt` is dead in production but imported.**
- Same audit applies to `parse_shallow_fill_from_llm` (imported at 225) and `validate_shallow_output` (imported at 226) — `process_section_fill_result` is used instead.
- Confidence: high (verified by grep on dbtool dump).

### A10.5 shallow LLM call + `process_section_fill_result(shallow_mode=True)` — [active]
- Location: dbtool `1984-1998`.
- Purpose: invoke 2b with shallow_mode=True; reuse normal pipeline (parse + normalize + validate + grammar). In shallow mode, `process_section_fill_result` skips title injection (title is in slot/preserve) and emits flat body_items.
- Inputs:
  - `_shallow_2b_msgs` (built by `build_section_fill_prompt` with `shallow_mode=True`).
  - `_shallow_2b_raw` (LLM response).
- Outputs (`_shallow_result` dict):
  - `body_items` (list of `{role, text}`).
  - `chapter_tree_nodes`: None (shallow has no chapter tree).
  - `debug_entry` (with `shallow_mode: True, title_injection_skipped_for_shallow: True, ...`).
  - `grammar_passed`, `items_count`.
- Confidence: high.

### A10.6 `observe_section_plan_compliance` — [debug-only]
- Location: `hwpx_analyzer.py:15572`.
- When called: dbtool `2005`.
- Purpose: 13.3b-1 — observe how well the generated body_items follow the section plan seed (heading count match, missing/extra estimates, order plausibility, thin_section_suspicion, source_topic_repetition_suspicion).
- Inputs:
  - `body_items`, `_section_plan_seed_result`.
- Outputs:
  - `_compliance` (dict, stored at `_debug_payload["shallow_section_plan_compliance"]`).
  - **Actually consumed: no** — debug-only observation, no downstream branching.
- Confidence: high.

### A10.7 `compute_preserve_indices` + `assemble_hwpx_hybrid` — [active]
- Location: `source_block_adapter.py:99`; `hwp_generator.py:assemble_hwpx_hybrid` (Agent 3 scope but called here).
- When called: dbtool `2007-2013` (shallow branch only).
- Purpose: collect slot/attachment paragraph indices that must be preserved in the original; call assembly with the shallow body_items.
- Inputs:
  - `_tup` (Phase E or legacy AI plan), `idx_map`.
  - `template_path, structure, content_data={"header": header_data, "body": body_items}, removed_indices, idx_map`.
- Outputs:
  - `_preserve_set` (set[int]), `_preserve_debug` (dict).
  - `result` (assembly result, structure `{success_count, fail_count, errors, data}`).
- Confidence: high.

### A10.8 `_shallow_done` sentinel + chapter_object stubs — [active]
- Location: dbtool `2047-2057`.
- Purpose: signal that shallow path produced final content. Downstream chapter loop (A11+) checks `not _shallow_done` to skip.
- Outputs:
  - `_shallow_done = True` on success.
  - `body_items, _section_fill_debug, _chapter_trees` kept from shallow result.
  - `_chapter_objects = None`, `_chapter_empty_reasons = None` on shallow path (signals chapter route should not collect).
- Audit note: comment at line 2056 "13.7a-A1: chapter route chapter object 수집 + A0 empty_reason 누적" — these vars are set to `None` (not `[]`) on shallow path to differentiate "shallow, skip chapter collection" from "chapter route, empty list".
- Confidence: high.

---

## Unresolved references — Part A3~A10

### outputs I produced but couldn't find consumer for
- `_debug_payload["source_blocks"]` (A7.2) — produced by `text_blob_to_source_blocks`. No downstream code in dbtool reads it. Pure debug observation.
- `_debug_payload["shallow_section_plan_compliance"]` (A10.6) — produced by `observe_section_plan_compliance`. No downstream branching. Pure debug observation.
- `_debug_payload["target_unit_plan_phase_e_production"]` (A9.3 sub-key `legacy_target_unit_plan_for_compare`) — diff dump; no consumer.
- `_debug_payload["chapter_types_phase_e_production"]` (A5.2) — diff dump; no consumer beyond debug viewer.
- `_route_debug` (A10.1) — written into `_debug_payload["shallow_generation"]` only. No code branch reads it after.
- `_compliance` (A10.6) — same as above.

### inputs I read but couldn't find producer for
- `_idx_full_texts` (A10.3, A8 cache update sync) — In dbtool, set at line 667 (`_idx_full_texts = _r1ab.get("idx_full_texts", {})` during 1a~1f loop), or line 1282 (`_idx_full_texts = sr0["idx_full_texts"]` after section_results loop), or line 1290 (`_idx_full_texts = _cached.get("idx_full_texts", {})` on cache hit). Producer is A1+A2 (out of scope here). Resolved.
- `_marker_policy_1f` / `structure.get("marker_policy_1f")` (A10.3) — Producer is 1f stage (A1+A2). Out of scope.
- `_cached_phase_e`, `_cached_track_c` (A3.7, A4.1) — set at dbtool lines 494-495 from `_cached.get("phase_e_chapter_planner")` etc. The cache version that writes these comes from A5.1 (cache write-back). On first run, cache won't have them yet — A5.1 writes after AI call. Consistent.
- `_cached` itself — produced by `load_template_cache` earlier in the dbtool (out of scope of A3-A10). Resolved.
- `template_grammar`, `role_text_types`, `per_type_role_semantics`, `format_rules` — produced in A1+A2 (1d/1e/1f). Out of scope.
- `_section_count`, `_actual_section_count` — produced at line 445/461 cache validation (also A1+A2 territory).

### Status tag summary
- [active] (in production decision path): A3.1, A3.2, A3.3, A3.4, A3.5, A3.6, A3.7, A4.1, A4.2, A4.3, A4.4, A5.1, A5.2, A6.1, A6.2, A6.3, A7.1, A7.3, A7.4, A8.1, A8.2, A8.3, A8.4, A8.5, A9.1, A9.2, A9.3, A10.1, A10.2, A10.3, A10.5, A10.7, A10.8
- [debug-only] (no production effect): A7.2 `text_blob_to_source_blocks`, A10.6 `observe_section_plan_compliance`. Also: per A4.5 audit, MEMORY labels Track C as debug-only but `_phase_e_to_chapter_types` consumes `pattern_families` to merge types — so Track C is at least partially production.
- [dead-code]: A3.8 `run_phase_e_chapter_planner` (defined, never imported in dbtool); A7.5 `_collect_roles` (defined inline, never invoked); A10.4 `build_shallow_fill_prompt` / `parse_shallow_fill_from_llm` / `validate_shallow_output` (imported but `build_section_fill_prompt(shallow_mode=True)` + `process_section_fill_result(shallow_mode=True)` is used instead).
- [uncertain]: A9.4 post-assembly legacy block interaction with A9's in-memory overwrite (would need `is_plan_cache_valid` source to verify whether the post-assembly block re-runs the legacy AI on a Phase E-shaped plan).
