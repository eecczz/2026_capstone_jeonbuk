# Sprint 1: 11.2 + 11.2b + marker/content charPr — DB tool spec

분석 단계만 도입. 적용(2b prompt 전달 + assembly run split)은 Sprint 2/3.

서버측 analyzer 함수는 commit 완료. **DB tool은 사용자가 직접 수정 필요** (DB에 있어서 git 추적 X).

---

## 1. 신규/변경 함수 import (`hwpx_analyzer.py`)

```python
from open_webui.utils.hwpx_analyzer import (
    # 기존 — schema 바뀜 (cluster당 single call로 변경)
    STYLE_PROFILE_PROMPT,
    _collect_style_samples,
    build_style_profile_prompt,
    parse_style_profile_from_llm,

    # NEW Stage 11.2b
    EMPHASIS_LAYER_PROMPT,
    extract_marker_content_charpr_per_cluster,
    extract_emphasis_charpr_candidates,
    build_emphasis_layer_prompt,
    parse_emphasis_layer_from_llm,

    # 기존 (이미 import 되어 있을 것)
    extract_marker_policies,
    save_template_cache,
    load_template_cache,
)
```

---

## 2. 호출 위치 — A2 loop 직후, A6 (2a chapter_classify) 전

audit doc A2.8 (1f marker policy induction) 끝나고 `_marker_policies` 구성 후, A3 (Phase E) 전이 자연스러움. 양식 분석 자료(1a~1f + Phase E + Track C)가 모두 완료된 시점이지만, Phase E inline은 cluster 결정 후니까 사실 11.2/11.2b는 어디든 cluster 확정(A2.5) 이후면 OK. 추천: **A2 loop 완전 종료 직후 / cache save 직전**.

### 호출 흐름 (DB tool 추가 코드)

```python
# ── Stage 11.2 + 11.2b — Style Profile + Inline Emphasis Layer ──
# 위치: A2 loop 종료 직후 (section_results 완성, _marker_policies 구성 후)
#       A2.12 incremental cache save 직전

_section0_sr = section_results.get(0) or {}
_section0_structure = _section0_sr.get("structure") or structure
_section0_paragraphs = _section0_structure.get("paragraphs") or []
_section0_idx_full_texts = _section0_sr.get("idx_full_texts") or _idx_full_texts
_section0_light_xml = _all_sections[0][1] if _all_sections else _section_light_xml

# 11.1 semantic_tag 사용 가능 시 전달 (이미 _debug_payload에 있을 것)
_semantic_tags = _debug_payload.get("structural_intent", {}).get("paragraphs") or []

# marker_policies (extract_marker_policies가 1f 결과 + 1a fallback merge)
_marker_policies = extract_marker_policies(
    _section0_paragraphs,
    marker_policy_1f=_section0_structure.get("marker_policy_1f") or {},
)

# ── 11.2 본체 ──
# style namespace cache check
_style_cache = load_template_cache(_cache_key, namespace='style') or {}
_style_cache_valid = (
    _style_cache.get("cache_schema_version") == STYLE_CACHE_SCHEMA_VERSION
    and _style_cache.get("cluster_signature") == _cluster_signature(_section0_paragraphs)
)
# (cluster_signature는 cluster_id set + count 정도의 가벼운 식별자 — 양식 동일성 검증)

if _style_cache_valid:
    _style_profiles = _style_cache.get("style_profiles", {})
    _emphasis_layers_by_cluster = _style_cache.get("emphasis_layers", {})
    _marker_content_charpr = _style_cache.get("marker_content_charpr", {})
    _from_style_cache = True
else:
    _from_style_cache = False
    _style_profiles = {}                   # cluster_id → 11.2 결과 dict
    _emphasis_layers_by_cluster = {}       # cluster_id → 11.2b 결과 dict
    
    # 11.2 sample 수집 (전수 sample + dedup + budget fallback)
    _style_samples = _collect_style_samples(
        _section0_paragraphs,
        _section0_idx_full_texts,
        semantic_tags=_semantic_tags,
        sample_text_char_budget=80000,
    )
    
    # cluster당 single AI call
    for _cluster_entry in _style_samples:
        _cluster_id = _cluster_entry["role"]
        _msgs_sp = build_style_profile_prompt(_cluster_entry)
        try:
            _llm_sp = await _call_llm(
                _msgs_sp,
                task_name=f"hwpx_style_profile_{_cluster_id}",
            )
            _parsed_sp = parse_style_profile_from_llm(_llm_sp)
        except Exception as e:
            log.warning(f"11.2 호출 실패 {_cluster_id}: {e}")
            _parsed_sp = {
                "role": _cluster_id,
                "content_style_rules_for_generation": [],
                "additional_observations": f"AI call failed: {e}",
                "_parse_status": "ai_call_failed",
                "_evidence_missing_rule_count": 0,
            }
        # 메타 보존
        _parsed_sp["_raw_count"] = _cluster_entry.get("raw_count", 0)
        _parsed_sp["_dedup_count"] = _cluster_entry.get("dedup_count", 0)
        _parsed_sp["_selected_count"] = _cluster_entry.get("selected_count", 0)
        _parsed_sp["_sampling_method"] = _cluster_entry.get("sampling_method", "all")
        _parsed_sp["_raw_measurements"] = _cluster_entry.get("raw_measurements", {})
        _style_profiles[_cluster_id] = _parsed_sp
    
    # ── 11.2b 본체 ──
    # marker/content charPr 분리 (code only)
    _marker_content_charpr = extract_marker_content_charpr_per_cluster(
        _section0_paragraphs,
        _section0_light_xml,
        _marker_policies,
    )
    
    # emphasis charpr candidates (code only)
    _emphasis_candidates = extract_emphasis_charpr_candidates(
        _section0_paragraphs,
        _section0_light_xml,
        marker_content_charpr=_marker_content_charpr,
    )
    
    # cluster별 emphasis가 있는 cluster만 11.2b AI 호출
    for _cluster_id, _em_cand_entry in _emphasis_candidates.items():
        # _style_samples에서 해당 cluster sample 가져오기
        _cluster_entry = next((e for e in _style_samples if e["role"] == _cluster_id), None)
        if _cluster_entry is None:
            continue
        _msgs_em = build_emphasis_layer_prompt(
            _cluster_entry,
            _em_cand_entry,
            _section0_light_xml,
        )
        try:
            _llm_em = await _call_llm(
                _msgs_em,
                task_name=f"hwpx_emphasis_layer_{_cluster_id}",
            )
            _parsed_em = parse_emphasis_layer_from_llm(_llm_em, _em_cand_entry)
        except Exception as e:
            log.warning(f"11.2b 호출 실패 {_cluster_id}: {e}")
            _parsed_em = {
                "role": _cluster_id,
                "emphasis_layers": _em_cand_entry.get("emphasis_charpr_candidates", []),
                "additional_observations": f"AI call failed: {e}",
                "_parse_status": "ai_call_failed",
                "_evidence_missing_rule_count": 0,
            }
        _parsed_em["_base_charpr_id"] = _em_cand_entry.get("base_charpr_id", "0")
        _parsed_em["_total_paragraphs_in_cluster"] = _em_cand_entry.get("total_paragraphs_in_cluster", 0)
        _parsed_em["_emphasis_present_paragraph_count"] = _em_cand_entry.get("emphasis_present_paragraph_count", 0)
        _emphasis_layers_by_cluster[_cluster_id] = _parsed_em
    
    # ── style namespace cache 저장 ──
    save_template_cache(
        _cache_key,
        {
            "cache_schema_version": STYLE_CACHE_SCHEMA_VERSION,  # NEW const, 예: 1
            "cluster_signature": _cluster_signature(_section0_paragraphs),
            "style_profiles": _style_profiles,
            "emphasis_layers": _emphasis_layers_by_cluster,
            "marker_content_charpr": _marker_content_charpr,
        },
        namespace='style',
    )

# ── debug payload ──
_debug_payload["style_profile"] = {
    "from_cache": _from_style_cache,
    "profiles": _style_profiles,
    "cluster_count": len(_style_profiles),
}
_debug_payload["emphasis_layer"] = {
    "from_cache": _from_style_cache,
    "marker_content_charpr": _marker_content_charpr,
    "emphasis_layers": _emphasis_layers_by_cluster,
    "emphasis_cluster_count": len(_emphasis_layers_by_cluster),
}
```

### helper: `_cluster_signature`

```python
def _cluster_signature(paragraphs: list[dict]) -> str:
    """양식의 cluster 구성을 식별하는 가벼운 signature.
    1e clustering 결과가 바뀌었는지 빠르게 검증."""
    from collections import Counter
    cnt = Counter(p.get("role", "") for p in paragraphs if p.get("role"))
    return ",".join(f"{k}:{v}" for k, v in sorted(cnt.items()))
```

---

## 3. `STYLE_CACHE_SCHEMA_VERSION` 상수 추가

`hwpx_analyzer.py`의 `CACHE_SCHEMA_VERSION = 11` 옆에 신규 추가:

```python
CACHE_SCHEMA_VERSION = 11        # main cache
STYLE_CACHE_SCHEMA_VERSION = 1   # NEW: 11.2 + 11.2b namespace='style' cache
```

→ 11.2/11.2b schema 변경 시 STYLE_CACHE_SCHEMA_VERSION만 bump. main cache 영향 X.

---

## 4. `analysis_only_mode` valve

`generate_document_hwp_local` valve 선언부에 추가:

```python
class Valves(BaseModel):
    # 기존 valves...
    analysis_only_mode: bool = Field(
        default=False,
        description=(
            "True 설정 시 양식 분석(1a~11.2 + 11.2b)까지만 진행하고 "
            "본문 생성(2a/2b/assembly) 전에 종료. "
            "rules debug 검증용. cache + debug 파일은 정상 생성됨."
        ),
    )
```

### cutoff 위치

A6 (2a chapter_classify) 호출 직전:

```python
# A5 (Phase E → chapter_types overwrite) 종료 후, A6 진입 직전
if valves.analysis_only_mode:
    log.info(
        f"[analysis_only_mode] 분석 완료, 본문 생성 skip. "
        f"cache_key={_cache_key}, style_profiles={len(_style_profiles)}, "
        f"emphasis_layers={len(_emphasis_layers_by_cluster)}"
    )
    
    # debug 파일 dump (write_stage_debug_files 호출 — A19에서 했던 거 미리)
    try:
        from open_webui.utils.hwpx_analyzer import write_stage_debug_files
        write_stage_debug_files(_debug_payload)
    except Exception as e:
        log.warning(f"debug dump 실패: {e}")
    
    # /tmp/hwpx_debug_last.json도 write
    try:
        with open("/tmp/hwpx_debug_last.json", "w") as f:
            json.dump(_debug_payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"debug_last.json dump 실패: {e}")
    
    return {
        "status": "analysis_only",
        "cache_key": _cache_key,
        "cluster_count": len(_section0_paragraphs and set(p.get("role","") for p in _section0_paragraphs)),
        "style_profile_cluster_count": len(_style_profiles),
        "emphasis_layer_cluster_count": len(_emphasis_layers_by_cluster),
        "debug_dir": "/tmp/hwpx_debug/",
        "debug_last_path": "/tmp/hwpx_debug_last.json",
    }
# else: 평소 흐름 — A6 chapter_classify로 진행
```

---

## 5. debug 파일 추가

`write_stage_debug_files` (`hwpx_analyzer.py:8439`)에 다음 write 추가 — 또는 DB tool에서 직접 write:

```python
# 12b_style_profile.json — 기존 자리 재활용 (위 _sp_data 분기 대체)
_write("12b_style_profile.json", debug_payload.get("style_profile"))

# 12c_marker_content_charpr.json — NEW
_write("12c_marker_content_charpr.json", debug_payload.get("emphasis_layer", {}).get("marker_content_charpr"))

# 12d_emphasis_layers.json — NEW
_write("12d_emphasis_layers.json", debug_payload.get("emphasis_layer", {}).get("emphasis_layers"))
```

---

## 6. A19.3 기존 `pass` 제거 (선택)

audit C4.2의 "Style profile AI calls disabled — literal pass"는 이제 dead. 제거 가능.

---

## 7. 사용자 사용 흐름

```
# 1. valve 켜기
valves.analysis_only_mode = True

# 2. 양식 업로드 → 실행
서버: 1a~11.2 + 11.2b 분석 → main cache + style cache + debug 저장 → return analysis_only

# 3. debug 확인
cat /tmp/hwpx_debug/12b_style_profile.json    # 11.2 rules
cat /tmp/hwpx_debug/12c_marker_content_charpr.json  # marker/content 분리
cat /tmp/hwpx_debug/12d_emphasis_layers.json  # 11.2b emphasis rules

# 4. rules 안 좋으면
- prompt 수정 (STYLE_PROFILE_PROMPT or EMPHASIS_LAYER_PROMPT)
- STYLE_CACHE_SCHEMA_VERSION bump
- main cache 안 건드림 → 1a~1f 재실행 없음
- 양식 다시 실행 → 11.2/11.2b만 재호출

# 5. 만족 시 valve off → 본문 생성까지 진행 (Sprint 2 도입 후)
```

---

## 8. Sprint 2/3에서 할 일 (참고)

- **Sprint 2 (적용 Phase 1)**: 2b prompt에 `_style_profiles[role].content_style_rules_for_generation` 전달. assembly marker reattach에서 `_marker_content_charpr[cluster].marker_charpr_id` / `content_charpr_id` 두 run 분리.
- **Sprint 3 (적용 Phase 2)**: 2b prompt에 `_emphasis_layers_by_cluster[role].emphasis_layers[].rules_for_generation` + `[[em1]]...[[/em1]]` markup instruction. assembly markup parser + run split.

---

## 9. 검증 체크리스트 (Sprint 1 완료 후)

- [ ] `analysis_only_mode=True` 실행 시 분석만 완료
- [ ] `/tmp/hwpx_debug/12b_style_profile.json` 생성 — cluster마다 rules + observations
- [ ] `/tmp/hwpx_debug/12c_marker_content_charpr.json` 생성 — marker_cp/content_cp 분리된 cluster
- [ ] `/tmp/hwpx_debug/12d_emphasis_layers.json` 생성 — emphasis 있는 cluster의 layer rules
- [ ] `/tmp/hwpx_cache/<hash>.json` (main) + `<hash>_style.json` (style) 두 파일 존재
- [ ] 두 번째 실행 시 style cache hit → AI 호출 0
- [ ] STYLE_PROFILE_PROMPT 수정 + STYLE_CACHE_SCHEMA_VERSION bump → style cache만 invalidate, main cache hit 유지
- [ ] 양식 3개(조달청/민원인/CC7) 모두 정상 분석 (cluster 1개도 fail 없음)
