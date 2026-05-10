# 12.1 Marker Roundtrip Readiness Observation — Design (v2)

## 목적

content-only generation 전환(Phase 2) 전에, code 기반 marker strip → reattach가 정확한지 검증하는 debug-only observation.

**이 단계에서 하는 것:**
- production pipeline 변경 없음
- AI output schema 변경 없음
- strip → reattach → compare roundtrip으로 readiness 측정
- Phase 2 전환을 위한 evidence 수집

**이 단계에서 하지 않는 것:**
- 2b prompt 변경
- marker rewrite 변경
- assemble 변경
- validation hard gate

---

## 1. Module Location

### Decision: `app/backend/open_webui/utils/marker_separator.py` (신규)

| 기준 | `hwpx_analyzer.py` | 신규 `marker_separator.py` |
|------|--------------------|-----------------------------|
| 최종 책임 | analyzer = 분석/관측 | separator = marker rendering contract |
| Phase 2 연속성 | Phase 2 assemble 변경 시 analyzer와 무관 | Phase 2에서 assemble이 이 모듈의 reattach를 호출 |
| 파일 크기 | 9500줄+, 추가 부담 | 신규, 깨끗한 시작 |
| 기존 함수 재사용 | `analyze_marker_in_text` 있음 | strip 로직 일부 유사하지만 별도 구현 (roundtrip 비교 목적이 다름) |
| `_generate_sequence_marker` | hwp_generator 안 nested → import 불가 | 10줄 미만 재구현 (phase 2에서 single source of truth로 통합) |

**Phase 2 구조 예상:**
```
marker_separator.py
  +-- strip_marker(text, role, policy) → content
  +-- generate_marker(role, policy, sibling_index) → marker
  +-- reattach_marker(content, role, policy, sibling_index) → text
  +-- compare_roundtrip(original, stripped, reattached) → metrics

hwp_generator.py (assemble)
  +-- from marker_separator import reattach_marker  (Phase 2에서 사용)
```

---

## 2. Metric Definitions

### Primary Metrics (Phase 2 진입 기준)

| metric | 정의 | 의미 |
|--------|------|------|
| `content_preservation_rate` | strip 후 content가 원본에서 marker만 제거된 상태와 일치하는 비율 | strip이 content를 훼손하지 않는지 |
| `policy_marker_correctness_rate` | reattach가 생성한 marker가 policy + sibling_index 기준으로 맞는 비율 | code가 정확한 marker를 생성할 수 있는지 |

### Secondary Metrics (진단용)

| metric | 정의 | 의미 |
|--------|------|------|
| `original_exact_match_rate` | original text == reattached text 비율 | 참고용 (AI marker가 정확했는지) |
| `separator_exact_match_rate` | detected separator == policy separator 비율 | separator 정밀도 |
| `rewrite_equivalent_rate` | reattached == marker_rewrite 결과 비율 | 기존 rewrite와의 호환성 |

### 집계 단위

- 전체 (all items)
- `by_policy_type`: fixed_char, arabic_sequence, circled_sequence, star_depth, no_marker, ...
- `by_role_depth`: level 0~6
- `by_derived_mode_label`: chapter_generation, shallow_report, mixed (12.0 연결)
- `applicable_only`: star_depth/no_marker 제외한 항목만

---

## 3. Mismatch Taxonomy

| category | 의미 | severity |
|----------|------|----------|
| `ai_marker_wrong_but_policy_correct` | AI가 잘못된 marker를 넣었지만 reattach가 올바른 marker를 생성 | positive (reattach의 장점 증명) |
| `separator_only_difference` | content 동일, marker 동일, separator만 다름 | low (Phase 2에서 separator 정규화로 해결 가능) |
| `content_changed_during_strip` | strip이 content 일부를 잘못 제거 | high (content 훼손) |
| `marker_detection_failed` | marker가 있는데 감지 못함 | medium (strip 로직 개선 필요) |
| `policy_marker_generation_failed` | policy/sibling_index로 올바른 marker를 생성 못함 | high (reattach 로직 결함) |
| `sibling_index_mismatch` | AI의 sibling 순서와 tree 기반 sibling_index가 불일치 | medium (tree 정확도 문제) |
| `no_marker_false_positive` | no_marker role에서 content 시작부를 marker로 오인하여 strip | high (content 훼손) |
| `not_applicable_policy` | policy 정보 없음 또는 star_depth | skip (집계 제외) |

---

## 4. star_depth / no_marker 처리

| policy_type | 처리 | match denominator |
|-------------|------|-------------------|
| star_depth | skip (strip/reattach 하지 않음) | **제외** |
| no_marker | strip 시도하되 "아무것도 안 strip" 확인 | **포함** (false positive 감지 중요) |
| 기타 | 정상 roundtrip | **포함** |

### no_marker role 검증

```python
# no_marker role: marker가 없으므로 strip이 아무것도 건드리지 않아야 함
if policy_type == "no_marker":
    # strip result의 detected_marker가 ""이어야 정상
    # 만약 무언가를 marker로 잡았으면 → false_positive
    if strip_result["detected_marker"]:
        category = "no_marker_false_positive"  # content 훼손 위험
```

---

## 5. Separator Handling

### Phase 1 방침

- separator를 "완벽히 고치기"는 Phase 1 scope가 아님
- 하지만 debug에 반드시 기록
- `separator_exact_match`와 `separator_normalized_match` 분리

### Separator 감지 규칙

```python
KNOWN_SEPARATORS = [" ", "\t", ". ", ") ", ": ", "  "]  # 관측된 패턴

def detect_separator(text_after_marker: str) -> str:
    """marker 직후 문자열에서 separator를 추정."""
    for sep in sorted(KNOWN_SEPARATORS, key=len, reverse=True):
        if text_after_marker.startswith(sep):
            return sep
    if text_after_marker and text_after_marker[0] in (" ", "\t"):
        return text_after_marker[0]
    return ""  # no separator (marker가 content에 직접 붙어있음)
```

### Debug 기록

```json
{
  "detected_separator": " ",
  "policy_separator": " ",
  "separator_exact_match": true,
  "separator_normalized_match": true
}
```

---

## 6. 12.0 연결

### 직접 정책 연결: 없음

marker strip/reattach 로직은 unit type에 무관하게 동일.

### 간접 slice 가능성: 열어둠

per-item debug에 아래 metadata 포함 → 나중에 unit별 slice 가능:

```json
{
  "item_id": 5,
  "role": "role_cluster_8",
  "role_depth": 4,
  "chapter_idx": 1,
  "policy_type": "star_depth",
  "...roundtrip fields..."
}
```

12.0의 `derived_mode_label`은 debug summary에만 포함 (per-item에는 불필요):

```json
{
  "summary": {
    "template_derived_mode": "chapter_generation",
    "...aggregate metrics..."
  }
}
```

---

## 7. Phase 2 Decision Gate (재정의)

### Phase 2 진입 필수 조건

| 조건 | threshold | 이유 |
|------|-----------|------|
| `content_preservation_rate` (applicable items) | ≥ 99% | strip이 content를 훼손하면 안 됨 |
| `policy_marker_correctness_rate` (applicable items) | ≥ 95% | reattach가 올바른 marker를 생성해야 함 |
| `no_marker_false_positive_count` | = 0 | no_marker role에서 content 훼손 없어야 함 |
| `content_changed_during_strip` count | = 0 | strip이 content를 변경하면 안 됨 |

### Phase 2 진입 참고 (필수 아님)

| metric | 의미 |
|--------|------|
| `original_exact_match_rate` | AI marker 정확도 참고 (낮으면 reattach 전환의 가치가 큼) |
| `separator_exact_match_rate` | separator 정규화 필요 여부 |
| `ai_marker_wrong_but_policy_correct` count | reattach 전환의 benefit 수치화 |

---

## 8. Debug Output Schema

### `14_marker_roundtrip_readiness.json`

```json
{
  "schema_version": 1,
  "phase": "roundtrip_readiness_observation",
  "debug_only": true,

  "summary": {
    "template_derived_mode": "chapter_generation",
    "total_items": 66,
    "applicable_items": 51,
    "skipped_items": 15,
    "skipped_reasons": {"star_depth": 15},

    "content_preservation_rate": 1.0,
    "policy_marker_correctness_rate": 0.961,
    "original_exact_match_rate": 0.941,
    "separator_exact_match_rate": 0.98,

    "by_policy_type": {
      "fixed_char": {
        "count": 20,
        "content_preserved": 20,
        "policy_marker_correct": 20,
        "original_exact_match": 20
      },
      "arabic_sequence": {
        "count": 5,
        "content_preserved": 5,
        "policy_marker_correct": 4,
        "original_exact_match": 4
      },
      "no_marker": {
        "count": 26,
        "content_preserved": 26,
        "false_positives": 0
      }
    },

    "by_role_depth": {
      "depth_1": {"count": 10, "policy_correct": 10},
      "depth_2": {"count": 20, "policy_correct": 19},
      "depth_3": {"count": 15, "policy_correct": 14},
      "depth_4": {"count": 6, "policy_correct": 6}
    },

    "mismatch_taxonomy": {
      "ai_marker_wrong_but_policy_correct": 2,
      "separator_only_difference": 1,
      "content_changed_during_strip": 0,
      "marker_detection_failed": 0,
      "policy_marker_generation_failed": 1,
      "sibling_index_mismatch": 0,
      "no_marker_false_positive": 0
    }
  },

  "mismatches": [
    {
      "item_id": 12,
      "role": "role_cluster_10",
      "role_depth": 3,
      "chapter_idx": 1,
      "policy_type": "arabic_sequence",
      "category": "ai_marker_wrong_but_policy_correct",
      "original_preview": "3) 추진 방안",
      "content_preview": "추진 방안",
      "reattached_preview": "2) 추진 방안",
      "detail": "AI marker=3, policy expected=2 (sibling_index=2)",
      "separator_detected": ") ",
      "separator_policy": " ",
      "separator_match": false
    }
  ],

  "phase2_readiness": {
    "content_preservation_met": true,
    "policy_correctness_met": true,
    "no_false_positives_met": true,
    "no_content_damage_met": true,
    "overall_ready": true,
    "blockers": [],
    "warnings": ["separator_exact_match < 100%: normalize needed before Phase 2"]
  }
}
```

---

## 9. Function Design

```python
# marker_separator.py

def strip_marker(text: str, role: str, policy: dict) -> dict:
    """
    text에서 leading marker를 제거.

    Returns:
        {
            "original": str,
            "content": str,
            "detected_marker": str,
            "separator": str,
            "strip_method": "policy_match" | "no_marker_role" | "no_policy" | "not_applicable",
            "content_preserved": bool,  # True if no content was accidentally eaten
        }
    """

def generate_expected_marker(role: str, policy: dict, sibling_index: int) -> dict:
    """
    policy + sibling_index 기반으로 기대 marker를 생성.

    Returns:
        {
            "marker": str,
            "policy_type": str,
            "sibling_index": int,
            "generation_method": "from_markers_list" | "sequence_formula" | "fixed" | "no_marker",
            "success": bool,
        }
    """

def reattach_marker(content: str, marker: str, separator: str) -> str:
    """marker + separator + content 조합. 단순 concat."""
    if not marker:
        return content
    return f"{marker}{separator}{content}"

def compare_roundtrip(
    original: str,
    strip_result: dict,
    expected_marker_result: dict,
    reattached: str,
    policy: dict,
) -> dict:
    """
    roundtrip 비교. 다중 metric 산출.

    Returns:
        {
            "original_exact_match": bool,
            "content_preserved": bool,
            "policy_marker_correct": bool,
            "separator_exact_match": bool,
            "separator_normalized_match": bool,
            "mismatch_category": str | None,
            "detail": str,
        }
    """

def build_marker_roundtrip_debug(
    items: list[dict],
    marker_policies: dict,
    chapter_trees: list | None = None,
    derived_mode_label: str = "",
) -> dict:
    """
    전체 items에 대해 roundtrip 실행 + 집계.
    sibling_index는 chapter_trees에서 계산 (tree 가용 시).
    tree 미가용 시 fallback counter 사용.

    Returns: full debug dict (위 schema)
    """
```

---

## 10. Sibling Index 계산

Phase 1에서는 marker_rewrite가 이미 계산한 sibling_index를 **재사용**할 수 있음.

`_marker_rewrite_log`에 `sibling_index`가 이미 기록됨 (assemble 단계에서). 이 값을 roundtrip 검증의 입력으로 사용하면:
- tree 재계산 불필요
- marker_rewrite와 동일 조건에서 비교 가능

```python
# DB tool에서:
# assemble 후 _marker_rewrite_log가 있음
# 각 log entry에 sibling_index, role, policy_type, detected_marker, expected_marker, stripped_content 있음
# → build_marker_roundtrip_debug의 입력으로 직접 전달 가능
```

이렇게 하면 별도 tree traversal 없이 기존 rewrite log를 roundtrip 검증의 source로 활용.

---

## 11. Blocker / Watch / Later

### Blocker (Phase 2 진입 불가)

| 조건 | 의미 |
|------|------|
| `content_preservation_rate` < 99% | strip이 content를 훼손 |
| `policy_marker_correctness_rate` < 80% | reattach가 심각하게 부정확 |
| `no_marker_false_positive` > 0 | no_marker content 훼손 |
| `content_changed_during_strip` > 0 | strip 로직 결함 |

### Watch

| 조건 | 의미 |
|------|------|
| `policy_marker_correctness_rate` 80~95% | marker 생성 로직 개선 필요 |
| `separator_exact_match_rate` < 90% | separator 정규화 필요 |
| 특정 policy_type에서만 mismatch 집중 | 해당 policy 개선 대상 |

### Later

| 항목 | 시점 |
|------|------|
| Phase 2 (prompt 변경 + assemble reattach) | Phase 1 gate 통과 후 |
| separator 정규화 | Phase 2 진입 시 |
| `_generate_sequence_marker` 통합 | Phase 2에서 marker_separator.py가 single source of truth |
| marker rewrite retirement | Phase 2 안정화 후 |

---

## 12. Implementation Scope (Phase 1)

### Do

1. `marker_separator.py` 신규 생성
   - `strip_marker(text, role, policy)`
   - `generate_expected_marker(role, policy, sibling_index)`
   - `reattach_marker(content, marker, separator)`
   - `compare_roundtrip(original, strip_result, expected_marker, reattached, policy)`
   - `build_marker_roundtrip_debug(items_with_rewrite_log, marker_policies, derived_mode_label)`
2. `write_stage_debug_files`에 `14_marker_roundtrip_readiness.json` 추가
3. DB tool에 `build_marker_roundtrip_debug` 호출 (assemble 후, rewrite_log 활용)

### Do Not

| 항목 | 이유 |
|------|------|
| 2b prompt 변경 | Phase 2 |
| assemble에 reattach 삽입 | Phase 2 |
| marker rewrite 변경/제거 | Phase 3 |
| validation hard gate | Phase 2 안정화 후 |
| `_generate_sequence_marker` 추출/리팩토링 | Phase 2에서 통합 |
| star_depth strip 시도 | skip |
| cache schema 변경 | 불필요 |
| AI 호출 | 모든 계산 code-only |

### 검증 기준

- 3개 양식에서 roundtrip debug 출력 확인
- content_preservation_rate ≥ 99% 여부
- policy_marker_correctness_rate 측정
- mismatch taxonomy 분포 확인
- no_marker false positive = 0 확인

---

## 13. Test Strategy

```
1. 구현: marker_separator.py + debug output + DB tool 호출
2. 3개 양식 한 번에 검증 (이미 cache HIT이므로 빠름)
3. 결과 분석:
   - content_preservation_rate
   - policy_marker_correctness_rate
   - mismatch taxonomy
   - phase2_readiness 판정
4. blocker 있으면 수정, 없으면 12.1 Phase 1 완료
5. Phase 2 진입 여부 결정
```
