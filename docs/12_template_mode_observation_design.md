# 12.0 Template Unit Observation — Design (v7 / C안 Final)

## Overview

12.0은 기존 2a/2b pipeline을 변경하지 않고, template이 어떤 target unit에 적합한지 **debug-only로 관측**하는 단계.

핵심 구조: **unit observation 중심, mode label은 derived convenience summary**

- AI가 관련 unit type별 observed_role을 판단 (primary output)
- derived_mode_label은 unit observations에서 code가 기계적으로 도출 (policy switch 아님)
- pipeline_fit은 code-only로 기존 2a와의 호환성 진단

---

## 1. Module & Naming

### File: `app/backend/open_webui/utils/template_observer.py` (신규)

### Function Names

| 함수 | 역할 |
|------|------|
| `observe_template_units(structure, pipeline_context=None)` | orchestrator / entry point |
| `extract_template_unit_features(structure)` | facts 추출 |
| `build_template_unit_prompt(features)` | AI prompt 구성 |
| `parse_template_unit_observation_from_llm(raw)` | schema parse |
| `validate_unit_observation(features, observation)` | sanity check |
| `derive_mode_label(unit_observations)` | derived label 도출 |
| `compute_pipeline_fit(derived_label, unit_observations, pipeline_context)` | 2a fit 진단 |

### Cache Field: `template_unit_observation`

### Debug File: `13_template_unit_observation.json`

### observation_scope: `"template_unit_observation"`

---

## 2. Architecture

```
observe_template_units(structure, pipeline_context=None)
   |
   [1] extract_template_unit_features(structure)
   |
   [2] cache check:
   |     HIT + observer_version match + valid -> use cached
   |     else:
   |       build_template_unit_prompt(features)
   |       AI call (temperature=0)
   |       parse_template_unit_observation_from_llm(raw)
   |       if fail -> JSON extraction -> retry -> still fail -> empty observation
   |       validate_unit_observation(features, observation)
   |       derive_mode_label(unit_observations)
   |       save to cache
   |
   [3] if pipeline_context:
   |     compute_pipeline_fit(derived_label, unit_observations, pipeline_context)
   |
   [4] assemble final output dict
```

---

## 3. Unit Type Vocabulary

| unit_type | 의미 |
|-----------|------|
| `chapter` | chapter tree 단위 생성 (깊은 hierarchy) |
| `shallow_block` | 얕은 block/bullet 단위 채움 |
| `table` | 표 셀 채우기 중심 |
| `slot` | 고정 위치 slot 채움 (제목, 날짜, 기관명) |
| `section` | HWPX 물리 section이 content boundary로도 의미 있는 경우만 |
| `attachment` | 붙임/첨부 영역 |

확장 시 vocabulary 추가만으로 처리. schema 구조 변경 불필요.

### section unit 제한 원칙

- section은 물리 section이 **content boundary로도 의미 있을 때만** 평가
- 단순 페이지/레이아웃 구분이면 unit_observation에 넣지 않음
- "multi-section이니까 section unit이다"는 성립하지 않음
- section은 chapter/shallow_block 등과 성격이 다를 수 있으므로 신중하게 평가

---

## 4. Unit Observation Schema

### observed_role

| 값 | 의미 | 의미하지 않는 것 |
|----|------|------------------|
| `strong_candidate` | 이 unit type에 대한 structural evidence가 강함. 12.2에서 우선 검토할 후보 | "이 unit으로 반드시 생성하라" |
| `moderate_candidate` | 부분적 evidence 있음. 12.2에서 조건부 검토 대상 | "보조 unit으로 확정" |
| `background_signal` | 약한 signal이 있으나 dominant하지 않음. 참고용 | "무시해도 된다" |
| `not_indicated` | 현재 features 기준으로 이 unit의 근거가 약함 | "절대 사용 금지" |

핵심: 이 값들은 독립적 signal 강도이지 unit 간 상대적 ranking이 아님. 두 unit이 모두 `strong_candidate`일 수 있음 (mixed 양식).

### Per-unit schema

```json
{
  "unit_type": "chapter",
  "observed_role": "strong_candidate",
  "assessment_summary": "깊은 hierarchy(5 levels)와 repeatable body roles가 관측되어 chapter tree 단위 생성의 주요 후보. grammar path가 4 roles 깊이로 tree 분할에 충분한 구조.",
  "evidence_fields": ["max_observed_level", "depth_distribution", "grammar_summary.deepest_path_roles"],
  "evidence_values": "max_level=5, level_3+=40 paragraphs, deepest_path 4 roles",
  "risks": [],
  "counter_signals": []
}
```

### assessment_summary 규칙

- 2~3문장 이내. evidence에 기반한 관측 요약.
- 12.2 AI가 읽고 context를 이해할 수 있게 작성.
- 일반 문서 지식이 아닌 features 기반.
- 확정/지시 표현 금지 ("이 unit으로 채워야 한다" X).
- 관측/후보 표현 허용 ("...의 주요 후보", "...에 적합한 구조로 보임").

---

## 5. Input Features

v6와 동일. 변경점:

```json
"structural_signals": {
  "has_header_region": true,
  "has_approval_line": false,
  "has_attachment_region": false,
  "has_slot_like_region": false,
  "semantic_tag_observation": {
    "source": "heuristic",
    "used_for_unit_decision": false,
    "dominant_tags": ["body_paragraph", "supporting_note"]
  },
  "title_role_count": 2,
  "header_paragraph_descriptions": ["문서 제목", "작성일자", "기관명"]
}
```

---

## 6. AI Prompt

```python
TEMPLATE_UNIT_OBSERVATION_PROMPT = """당신은 구조화된 template facts를 해석하는 schema-constrained observer입니다.

아래에 양식(template)의 구조 분석 결과가 주어집니다.
이 양식의 content를 채울 때 어떤 target unit이 후보로 관측되는지 평가하세요.

## target unit types

- chapter: 깊은 role hierarchy가 있어 chapter tree 단위 생성이 자연스러운 구조
- shallow_block: 얕은 구조로 bullet/block 단위 채움이 자연스러운 구조
- table: 표(table) 셀 채우기가 주요 작업인 구조
- slot: 고정 위치(제목, 날짜, 기관명 등)를 채우는 것이 의미 있는 구조
- section: HWPX 물리 section이 content boundary로도 의미 있는 구조 (단순 레이아웃 구분이면 평가 불필요)
- attachment: 붙임/첨부 영역이 별도 채움 단위인 구조

## observed_role 정의

- strong_candidate: 이 unit에 대한 structural evidence가 강함 (다음 단계에서 우선 검토 대상)
- moderate_candidate: 부분적 evidence 있음 (조건부 검토 대상)
- background_signal: 약한 signal이 있으나 dominant하지 않음 (참고용)
- not_indicated: 현재 features 기준으로 근거가 약함

이 값은 독립적 signal 강도입니다. 두 unit이 모두 strong_candidate일 수 있습니다.
이 값은 planning 확정값이 아닙니다. 후속 단계에서 독립적 planning이 별도 수행됩니다.

## 핵심 규칙

1. **제공된 features에서 직접 관찰 가능한 근거만 사용하세요.**
2. **일반 문서 지식으로 추측하지 마세요.**
   - 금지: "업무계획서는 보통 chapter 단위가 적합하다"
   - 금지: "보고서라서 shallow이다"
   - 금지: 문서명/기관명/정책명 기반 판단
3. **features의 구조 수치/분포/signal만 evidence로 사용하세요.**
4. **특정 숫자를 threshold rule처럼 사용하지 마세요.**
   - 금지: "body count가 40 이상이므로 chapter가 적합하다"
   - 허용: "body 70개로 단일 생성에 과하며, 깊은 hierarchy와 함께 분할 단위가 자연스러워 보인다"
5. **관련 있는 unit type만 평가하세요.** 6개 전부 의무 평가 아님.
   - features에서 근거를 찾을 수 있는 unit만 unit_observations에 포함
   - 모든 unit을 채우지 마세요
6. **not_assessed_units는 선택적입니다.** 아래 경우에만 기록:
   - features에 해당 unit 관련 signal이 있었지만 unit_observations에 포함하지 않은 경우
   - 누락으로 오해될 수 있어 이유를 남길 필요가 있는 경우
   - 모든 미평가 unit을 나열하지 마세요
7. **section은 content boundary로 의미 있을 때만 평가하세요.**
   - 단순 multi-section(페이지 나눔)이면 section unit으로 보지 않음
   - section 내 content가 독립적이고 section 단위 채움이 자연스러울 때만 평가
8. **evidence_fields에는 features의 필드명을 사용하세요.** (dot notation 허용)
9. **assessment_summary는 다음 단계 AI가 읽을 수 있게 작성하세요.** 2~3문장, evidence 기반, 확정 표현 금지.
10. **semantic_tag_observation은 보조 참고만 가능합니다.** (heuristic 기반이므로 단독 근거 금지)

## 출력 형식

반드시 아래 JSON만 출력하세요.

```json
{
  "unit_observations": [
    {
      "unit_type": "unit type name",
      "observed_role": "strong_candidate | moderate_candidate | background_signal | not_indicated",
      "assessment_summary": "2~3문장 관측 요약 (다음 단계 AI가 읽을 context)",
      "evidence_fields": ["features 필드명"],
      "evidence_values": "핵심 값 요약",
      "risks": ["이 unit 사용 시 위험 요소 (있으면)"],
      "counter_signals": ["이 unit에 불리한 signal (있으면)"]
    }
  ],
  "not_assessed_units": [
    {
      "unit_type": "unit type name",
      "reason": "미평가 이유 (signal 있었지만 제외한 경우에만)"
    }
  ],
  "cross_unit_concerns": ["여러 unit에 걸친 관측/우려 (있으면)"],
  "ambiguity_flags": ["판단이 모호한 지점 (있으면)"]
}
```
"""
```

---

## 7. Mode Label Derivation (code-only)

```python
def derive_mode_label(unit_observations: list[dict]) -> dict:
    """
    unit_observations에서 기계적으로 mode label을 도출.

    IMPORTANT: 이 label은 debug convenience summary이다.
    - generation/validation/assemble route를 직접 결정하지 않는다.
    - pipeline 분기에 사용하지 않는다.
    - 12.2에서 독립적 target_unit planning을 수행한다.
    - 사람이 빠르게 양식 성격을 파악하기 위한 요약일 뿐이다.
    """
    strong = [u for u in unit_observations if u.get("observed_role") == "strong_candidate"]
    moderate = [u for u in unit_observations if u.get("observed_role") == "moderate_candidate"]
    strong_types = {u["unit_type"] for u in strong}
    moderate_types = {u["unit_type"] for u in moderate}

    # Derivation rules (기계적, threshold 없음)
    if not strong and not moderate:
        label = "unknown"
        rule = "no_strong_or_moderate_candidates"
        derived_from = []
    elif strong_types == {"chapter"}:
        label = "chapter_generation"
        rule = "chapter_only_strong"
        derived_from = ["chapter"]
    elif "chapter" not in strong_types and strong_types & {"shallow_block", "table", "slot"}:
        label = "shallow_report"
        rule = "non_chapter_units_strong"
        derived_from = sorted(strong_types)
    elif "chapter" in strong_types and (strong_types - {"chapter", "slot"}):
        # chapter + 다른 content unit이 모두 strong
        label = "mixed"
        rule = "chapter_and_other_content_units_both_strong"
        derived_from = sorted(strong_types)
    elif "chapter" in strong_types and strong_types <= {"chapter", "slot"}:
        # chapter + slot만 strong (slot은 header 용도이므로 chapter_generation)
        label = "chapter_generation"
        rule = "chapter_strong_with_slot_only"
        derived_from = sorted(strong_types)
    elif not strong and moderate:
        if "chapter" in moderate_types and not (moderate_types - {"chapter", "slot"}):
            label = "chapter_generation"
            rule = "chapter_moderate_only"
            derived_from = sorted(moderate_types)
        elif "chapter" not in moderate_types:
            label = "shallow_report"
            rule = "non_chapter_moderate"
            derived_from = sorted(moderate_types)
        else:
            label = "mixed"
            rule = "multiple_moderate_including_chapter"
            derived_from = sorted(moderate_types)
    else:
        label = "unknown"
        rule = "unclassifiable_combination"
        derived_from = sorted(strong_types | moderate_types)

    # Confidence (based on signal clarity)
    if len(strong) >= 1 and len(strong) <= 2:
        confidence = "high"
    elif len(strong) >= 3:
        confidence = "medium"  # too many strong -> ambiguous
    elif moderate:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "label": label,
        "is_policy_switch": False,
        "derivation_rule": rule,
        "derived_from_units": derived_from,
        "confidence_level": confidence,
        "note": "debug convenience summary; does NOT determine generation/validation/assemble route; 12.2 performs independent planning",
    }
```

### Label 값 정의

| label | 의미 | 조건 |
|-------|------|------|
| `chapter_generation` | chapter tree 중심 | chapter만 strong (+ slot은 허용) |
| `shallow_report` | 얕은 unit 중심 | chapter 외 content unit이 strong |
| `mixed` | 여러 content unit이 함께 strong | chapter + 다른 content unit 모두 strong |
| `unknown` | 판단 불가 | strong/moderate 없음 또는 분류 불가 |

---

## 8. Validation Layer

```python
def validate_unit_observation(features: dict, observation: dict) -> dict:
    blockers = []
    warnings = []
    confidence_downgrade = False

    units = observation.get("unit_observations", [])
    not_assessed = observation.get("not_assessed_units", [])

    valid_top_keys = set(features.keys())
    valid_roles = {"strong_candidate", "moderate_candidate", "background_signal", "not_indicated"}
    valid_unit_types = {"chapter", "shallow_block", "table", "slot", "section", "attachment"}

    # ── Blockers ──

    # B1: unit_observations 비어있음
    if not units:
        blockers.append("no_unit_observations")

    # B2: strong/moderate candidate의 evidence가 전부 hallucinated
    for u in units:
        if u.get("observed_role") in ("strong_candidate", "moderate_candidate"):
            fields = u.get("evidence_fields", [])
            if fields:
                hallucinated = [f for f in fields if f.split(".")[0] not in valid_top_keys]
                if len(hallucinated) == len(fields):
                    blockers.append(f"fully_hallucinated_evidence: {u.get('unit_type')}")

    # ── Warnings ──

    # W1: 개별 hallucinated field
    for u in units:
        for f in u.get("evidence_fields", []):
            if f.split(".")[0] not in valid_top_keys:
                warnings.append(f"hallucinated_field: {f} in {u.get('unit_type')}")

    # W2: strong_candidate인데 assessment_summary 비어있음
    for u in units:
        if u.get("observed_role") == "strong_candidate" and not u.get("assessment_summary"):
            warnings.append(f"strong_without_summary: {u.get('unit_type')}")

    # W3: strong_candidate의 evidence에 hallucination -> confidence downgrade
    for u in units:
        if u.get("observed_role") == "strong_candidate":
            fields = u.get("evidence_fields", [])
            hallucinated = [f for f in fields if f.split(".")[0] not in valid_top_keys]
            if hallucinated:
                confidence_downgrade = True
                warnings.append(f"strong_has_hallucinated_evidence: {u.get('unit_type')}")

    # W4: unknown unit_type
    for u in units:
        if u.get("unit_type") not in valid_unit_types:
            warnings.append(f"unknown_unit_type: {u.get('unit_type')}")

    # W5: unknown observed_role
    for u in units:
        if u.get("observed_role") not in valid_roles:
            warnings.append(f"unknown_observed_role: {u.get('observed_role')}")

    # W6: evidence_values 비어있음
    for u in units:
        if u.get("observed_role") in ("strong_candidate", "moderate_candidate"):
            if not u.get("evidence_values"):
                warnings.append(f"empty_evidence_values: {u.get('unit_type')}")

    return {
        "valid": len(blockers) == 0,
        "blockers": blockers,
        "warnings": warnings,
        "confidence_downgrade": confidence_downgrade,
    }
```

---

## 9. Pipeline Fit Diagnostics

```python
def compute_pipeline_fit(derived_label: dict, unit_observations: list, pipeline_context: dict | None) -> dict:
    if not pipeline_context:
        return {"has_2a_data": False}

    label = derived_label.get("label", "unknown")
    conflicts = []
    ch_count = pipeline_context.get("chapter_count", 0)
    concentration = pipeline_context.get("source_concentration_ratio", 0)
    underfill = pipeline_context.get("underfill_candidates", [])

    # unit_observations에서 추가 정보 참조
    chapter_obs = next((u for u in unit_observations if u.get("unit_type") == "chapter"), None)
    non_chapter_strong = [u for u in unit_observations
                         if u.get("unit_type") != "chapter"
                         and u.get("unit_type") != "slot"
                         and u.get("observed_role") == "strong_candidate"]

    if label == "unknown":
        conflicts.append({
            "type": "label_undetermined",
            "severity": "watch",
            "detail": "derived label is unknown; legacy 2a fit cannot be confidently assessed"
        })

    elif label == "shallow_report":
        if ch_count >= 3:
            conflicts.append({
                "type": "shallow_template_multi_chapter_2a",
                "severity": "watch",
                "detail": f"template is shallow_report but 2a produced {ch_count} chapters — legacy pipeline likely mismatched"
            })
        if concentration and concentration > 0.8:
            conflicts.append({
                "type": "shallow_template_source_imbalanced",
                "severity": "watch",
                "detail": f"source_concentration={concentration:.3f} — source split may not be meaningful for shallow template"
            })

    elif label == "mixed":
        # mixed는 chapter도 후보이므로 multi-chapter 자체는 문제가 아님
        # 문제는 non-chapter unit이 현 pipeline에서 무시될 위험
        if non_chapter_strong:
            unit_names = [u["unit_type"] for u in non_chapter_strong]
            conflicts.append({
                "type": "mixed_template_non_chapter_units_ignored",
                "severity": "watch",
                "detail": f"non-chapter strong candidates {unit_names} exist but legacy 2a/2b pipeline only handles chapter-based generation"
            })
        if concentration and concentration > 0.7:
            conflicts.append({
                "type": "mixed_template_source_allocation_risk",
                "severity": "watch",
                "detail": f"source_concentration={concentration:.3f} — mixed template may need per-unit source allocation"
            })

    elif label == "chapter_generation":
        if ch_count == 1:
            conflicts.append({
                "type": "chapter_template_single_chapter_2a",
                "severity": "watch",
                "detail": "template supports chapter generation but 2a produced only 1 chapter"
            })
        if len(underfill) >= 2:
            conflicts.append({
                "type": "chapter_template_underfill",
                "severity": "watch",
                "detail": f"{len(underfill)} underfill chapters — source allocation issue"
            })

    return {
        "has_2a_data": True,
        "observed_2a_chapter_count": ch_count,
        "source_concentration_ratio": concentration,
        "underfill_candidates": underfill,
        "overfill_candidates": pipeline_context.get("overfill_candidates", []),
        "primary_conflict": conflicts[0]["type"] if conflicts else "none",
        "conflict_details": conflicts,
    }
```

---

## 10. Debug Output Schema

```json
{
  "schema_version": 1,
  "observer_version": "v0.1",
  "debug_only": true,
  "observation_scope": "template_unit_observation",

  "cache_status": {
    "observation_cache_hit": false,
    "observation_cache_written": true,
    "observer_version_matched": false,
    "cache_warning": null
  },

  "features": { "... full features dict ..." },

  "ai_observation": {
    "unit_observations": [
      {
        "unit_type": "chapter",
        "observed_role": "strong_candidate",
        "assessment_summary": "깊은 hierarchy(5 levels)와 repeatable body roles 관측. chapter tree 분할에 충분한 grammar path(4 roles). 주요 generation unit 후보.",
        "evidence_fields": ["max_observed_level", "depth_distribution", "grammar_summary.deepest_path_roles"],
        "evidence_values": "max_level=5, level_3+=40, deepest_path 4 roles",
        "risks": [],
        "counter_signals": []
      },
      {
        "unit_type": "slot",
        "observed_role": "moderate_candidate",
        "assessment_summary": "header 영역에 3개 고정 slot(제목, 날짜, 기관명) 관측. 전체 양식이 slot 중심은 아니나 header filling에 적합.",
        "evidence_fields": ["structural_signals.has_header_region", "structural_signals.header_paragraph_descriptions"],
        "evidence_values": "header region with 3 descriptions",
        "risks": [],
        "counter_signals": []
      }
    ],
    "not_assessed_units": [
      {
        "unit_type": "table",
        "reason": "table_signal.table_detected=true but table_ratio=0.023 — table 비중이 낮아 content unit 후보로 보기 어려움"
      }
    ],
    "cross_unit_concerns": [],
    "ambiguity_flags": []
  },

  "ai_call_info": {
    "attempts": 1,
    "success": true,
    "error": null,
    "raw_output_length": 580
  },

  "validation_result": {
    "valid": true,
    "blockers": [],
    "warnings": [],
    "confidence_downgrade": false
  },

  "derived_mode_label": {
    "label": "chapter_generation",
    "is_policy_switch": false,
    "derivation_rule": "chapter_strong_with_slot_only",
    "derived_from_units": ["chapter", "slot"],
    "confidence_level": "high",
    "note": "debug convenience summary; does NOT determine generation/validation/assemble route; 12.2 performs independent planning"
  },

  "pipeline_fit_diagnostics": {
    "has_2a_data": true,
    "observed_2a_chapter_count": 3,
    "source_concentration_ratio": 0.12,
    "underfill_candidates": [],
    "overfill_candidates": [],
    "primary_conflict": "none",
    "conflict_details": []
  },

  "risk_assessment": {
    "blocker_items": [],
    "watch_items": [],
    "later_items": []
  }
}
```

---

## 11. Cache Strategy

### Cache Field: `template_unit_observation`

```python
CURRENT_OBSERVER_VERSION = "v0.1"

cache_data["template_unit_observation"] = {
    "observer_version": "v0.1",
    "unit_observations": [...],
    "not_assessed_units": [...],
    "cross_unit_concerns": [...],
    "ambiguity_flags": [...],
    "derived_mode_label": {...},
    "validation_result": {...},
    "features_snapshot": {...},
    "features_snapshot_scope": "debug/repro only; not used by generation or assemble",
}
```

### Cache HIT 조건

```python
cached = structure.get("template_unit_observation")
cache_valid = (
    cached
    and cached.get("observer_version") == CURRENT_OBSERVER_VERSION
    and cached.get("validation_result", {}).get("valid", False)
)
```

---

## 12. AI Failure & Retry

| 상황 | 처리 |
|------|------|
| AI 호출 실패 | 1회 retry. 재실패 -> empty observation |
| JSON parse 실패 | code extraction (fence strip, brace range). 실패 -> 1회 retry |
| Required field 누락 | empty observation (retry 없음) |
| Validation blocker | observations 비움, label = unknown/undetermined |

Empty observation: `unit_observations: []`, `not_assessed_units: [{"unit_type": "all", "reason": "ai_call_failed"}]`

---

## 13. Expected Results (3 Templates)

### 조달청 업무계획

| unit | observed_role | key evidence |
|------|---------------|--------------|
| chapter | strong_candidate | depth=5, repeatable roles, deep grammar |
| slot | moderate_candidate | header region (3 slots) |

derived_mode_label: `chapter_generation` (rule: `chapter_strong_with_slot_only`)

### 민원인 위법행위 대응지침

| unit | observed_role | key evidence |
|------|---------------|--------------|
| chapter | strong_candidate | depth=3~4, grammar hierarchy |
| section | background_signal | multi-section(5) but chapter dominates content |
| slot | moderate_candidate | header slots |

derived_mode_label: `chapter_generation` (rule: `chapter_strong_with_slot_only`)

### CC7 AI 운영상황 보고

| unit | observed_role | key evidence |
|------|---------------|--------------|
| shallow_block | strong_candidate | depth<=2, flat structure |
| table | strong_candidate | high table ratio |
| chapter | not_indicated | depth too shallow |
| slot | moderate_candidate | header slots |

derived_mode_label: `shallow_report` (rule: `non_chapter_units_strong`)
pipeline_fit: `shallow_template_multi_chapter_2a` watch

---

## 14. Blocker / Watch / Later

### Blocker

| 조건 | 의미 |
|------|------|
| 3개 양식에서 기대 derived_mode_label 불일치 | derivation rule 또는 prompt 문제 |
| validation blocker | AI output 품질 문제 |
| features 추출 exception | code 버그 |
| 3개 양식 모두 label = unknown | AI 판단 회피 |

### Watch

| 조건 | 의미 |
|------|------|
| confidence = medium | 모호하거나 features 부족 |
| validation warning | prompt 개선 후보 |
| pipeline_fit conflict | 2a 정책 검토 재료 |
| not_assessed에 signal 있는 unit 누락 | prompt 지침 보완 |

### Later

| 항목 | 다음 단계 |
|------|----------|
| planning_contract 확정 | 12.2 |
| target_unit extraction (독립) | 12.2 |
| unit observation 기반 route 설계 | 12.1+ |
| unit vocabulary 확장 | 새 양식 관측 시 |

---

## 15. derived_mode_label 사용 원칙 (강제)

이 원칙은 schema, code 주석, 문서에 모두 명시한다:

1. **derived_mode_label은 debug convenience summary다.** 사람이 빠르게 양식 성격을 파악하기 위한 요약.
2. **generation/validation/assemble route를 직접 결정하지 않는다.**
3. **`if label == "shallow_report": ...` 같은 pipeline 분기를 만들지 않는다.**
4. **실제 다음 단계 판단은 unit_observations, evidence, risks를 기준으로 한다.**
5. **12.2에서 독립적 target_unit planning을 수행한다.** 12.0 label에 의존하지 않음.

---

## 16. Decision Gates

### 12.0 Complete

- [ ] 3개 양식 unit_observations 기대 일치
- [ ] derived_mode_label 기대 일치
- [ ] validation blocker 없음
- [ ] CC7에서 pipeline_fit conflict 감지
- [ ] cache HIT 재사용 확인
- [ ] AI 실패 시 unknown fallback 확인

### 12.0 -> 12.1

- 12.0 완료
- CC7에서 "chapter: not_indicated" + "shallow_block/table: strong" evidence 확보
- marker/content separation이 unit observation과 무관하게 동작 가능한지 판단

### 12.1 -> 12.2

- 12.1 완료
- 12.0 unit_observations에서 target_unit seed 추출 가능한지 확인
- 12.2는 12.0을 seed로만 사용하고 독립적 planning 수행

---

## 17. Implementation Scope

### Do

1. `template_observer.py` 신규 생성 (전체 구조)
2. Cache에 `template_unit_observation` 필드 추가
3. `13_template_unit_observation.json` debug output 추가
4. DB tool에 `observe_template_units` 호출 + AI task 추가

### Do Not

| 항목 | 이유 |
|------|------|
| 2a prompt 수정 | scope 밖 |
| unit observation 기반 pipeline 분기 | debug-only |
| planning_contract 확정 | 12.2 |
| marker/content schema | 12.1 |
| validation hard gate | 관측 충분 후 |
| CACHE_SCHEMA_VERSION bump | observer_version으로 분리 |
| code heuristic fallback | unknown이 더 정직 |
| 전체 unit type survey 강제 | 관련 unit만 |

---

## 18. Test Strategy

```
1. cheap check: 3개 양식 실측 수치 확인 -> features 추출 로직 확정
2. 한 묶음 구현: template_observer.py + cache + debug file + DB tool
3. 3개 양식 한 번에 검증:
   - unit_observations observed_role 기대 일치
   - derived_mode_label 기대 일치
   - evidence가 features를 정확히 참조
   - assessment_summary가 evidence 기반
   - validation blocker 없음
   - CC7에서 pipeline_fit conflict 감지
   - cache HIT 재사용
4. 조정:
   - observed_role 불일치 -> prompt 개선
   - hallucination -> prompt 규칙 강화
   - not_assessed 누락 -> prompt 지침 추가
   - derivation rule 문제 -> rule 로직 수정
```
