# 13.5 Implementation Plan — Region Action Plan + Attachment Preserve

## 현재 상태 요약

| 항목 | 상태 |
|------|------|
| 선행 완료 | 13.4b (template-driven chapter loop) — 커밋 6b0a468 |
| 설계서 | `docs/13_5_region_action_plan_design.md` — 스키마/검증기준 확정 |
| 문제 | chapter route에서 attachment paragraph가 전부 삭제됨 |
| 근본 원인 | `assemble_hwpx_hybrid`에 `preserve_indices=None` 전달 → header_indices에 없는 paragraph 전부 remove |

---

## 변경 파일 3개

### 1. `hwpx_analyzer.py` — `compute_region_action_plan()` 신규 (+~80 lines)

**위치**: `_find_dominant_chapter_type()` 바로 아래 (L9877), `extract_shallow_section_plan_seed` 위.

**함수 시그니처**:
```python
def compute_region_action_plan(
    target_unit_plan: dict,
    structure: dict,
    idx_map: dict | None = None,
) -> dict | None:
```

**입력**:
- `target_unit_plan` (`_tup`): regions 리스트 포함. region마다 `unit_type`, `paragraph_indices`, `region_id`
- `structure`: paragraph별 `level` 조회용
- `idx_map`: AI idx → real idx 변환

**로직** (region 단위 순회):
```
for each region in target_unit_plan.regions:
    unit_type = region.unit_type

    if unit_type == "slot":
        action = "fill_slot"
        in_preserve_set = False
        preserve_via_header = True       # header_indices 경로로 이미 보존
        table_policy = "not_applicable"

    elif unit_type == "chapter":
        action = "generate"
        in_preserve_set = False
        preserve_via_header = False
        table_policy = "defer_table_filling"

    elif unit_type == "attachment":
        action = "preserve_original"
        in_preserve_set = True
        preserve_via_header = False
        table_policy = "preserved_with_region"

    elif unit_type == "shallow_block":
        # level-0이면 header_indices에서 이미 보존됨
        all_level_0 = all paragraphs in region have level == 0
        if all_level_0:
            action = "preserve_original"
            in_preserve_set = False
            preserve_via_header = True
        else:
            action = "preserve_original"
            in_preserve_set = True
            preserve_via_header = False
        table_policy = "not_applicable"

    else:
        action = "skip_with_reason"
        reason = f"unknown unit_type: {unit_type}"
```

**preserve_indices 계산**:
```python
preserve_indices = []
for action_entry in actions:
    if action_entry["in_preserve_set"]:
        preserve_indices.extend(action_entry["paragraph_indices"])
```

**idx_map 적용**: `paragraph_indices`를 real idx로 변환한 후 action에 기록.

**반환값**: 설계서 Section 3의 스키마 그대로.

**None 반환 조건**: `target_unit_plan`이 None이거나 regions 비어있으면.

### 2. DB tool — chapter route에 호출 추가 (+~10 lines)

**import 추가** (L231 부근):
```python
from open_webui.utils.hwpx_analyzer import (
    ...
    compute_region_action_plan,   # 추가
)
```

**호출 위치**: Step 5 assemble 직전 (L1586~1593 사이).

**코드**:
```python
# ── 13.5: Region Action Plan — attachment preserve ──
_region_plan = None
_chapter_preserve = None
if not _shallow_done and _tup:
    _region_plan = compute_region_action_plan(_tup, structure, idx_map=idx_map)
    if _region_plan:
        _pi = _region_plan.get("preserve_indices", [])
        _chapter_preserve = set(_pi) if _pi else None
        _debug_payload["region_action_plan"] = _region_plan
```

**assemble 호출 변경** (L1593):
```python
result = assemble_hwpx_hybrid(
    template_path, structure, content_data,
    removed_indices=removed_indices, idx_map=idx_map,
    chapter_trees=_valid_trees,
    content_only_mode=True,
    preserve_indices=_chapter_preserve,   # 추가
)
```

### 3. `hwp_generator.py` — 변경 없음

`preserve_indices` 파라미터는 이미 존재하고 동작 검증 완료 (shallow route에서 사용 중).

---

## 건드리지 않는 것

| 항목 | 이유 |
|------|------|
| shallow route의 `compute_preserve_indices` | 기존 검증 경로 유지. CC7에서 동작 확인됨 |
| `assemble_hwpx_hybrid` 내부 로직 | preserve_indices 전달만으로 충분 |
| 2a/2b prompt | 불필요 |
| `source_block_adapter.py` | shallow route 전용, chapter route와 무관 |
| 캐시 | 13.5는 캐시 이후 단계 |

---

## 검증 순서

### Step 1: 조달청 cheap check (AI 호출 필요, ~2분)

| 확인 항목 | 기대값 |
|----------|--------|
| `region_action_plan` debug 존재 | O |
| slot action = `fill_slot` | O |
| chapter action = `generate` | O |
| shallow_block preserve_via_header | True (level-0) |
| preserve_indices | `[]` (attachment 없음) |
| coverage | `all_regions_visited` |
| overlap_warnings | `[]` |
| assembly fail | 0 (regression 없음) |

### Step 2: 민원인 e2e (AI 호출 필요, ~5분)

| 확인 항목 | 기대값 |
|----------|--------|
| attachment region action = `preserve_original` | O |
| preserve_indices 크기 | ~280 (sec[1]+sec[2]+sec[4] attachment paragraphs) |
| assembly fail | 0 |
| section[1,2,4]에 content 존재 | secPr carrier만이 아닌 실제 content |

### Step 3: CC7 shallow 불변

| 확인 항목 | 기대값 |
|----------|--------|
| `region_action_plan` debug | 없음 (shallow route는 미적용) |
| 기존 `compute_preserve_indices` 경로 유지 | O |
| assembly fail | 0 |

---

## 위험 요소 + 대응

### R1: level 조회 실패
paragraph에 `level` 필드가 없는 경우 → `level` 기본값 0으로 처리 (보수적: header_indices에서 보존된다고 가정).

### R2: idx_map에 없는 idx
target_unit_plan의 paragraph_indices 중 idx_map에 없는 idx → idx 그대로 사용 (기존 `compute_preserve_indices`와 동일 패턴).

### R3: region에 paragraph_indices 누락
12.2 AI가 부여 실패한 경우 → 해당 region은 `skip_with_reason`, overlap_warning에 기록.

### R4: generate와 preserve 겹침
한 paragraph가 chapter region과 attachment region에 동시 소속 → overlap_warnings에 기록. 현재 3개 양식에서 이 케이스 없음 (12.2에서 배타적 region 할당).

---

## 구현 순서

| # | 작업 | 예상 시간 | 검증 |
|---|------|----------|------|
| 1 | `compute_region_action_plan()` 함수 작성 | 10분 | 코드 리뷰 |
| 2 | DB tool import + 호출 + assemble preserve_indices 전달 | 5분 | 코드 리뷰 |
| 3 | 조달청 cheap check | 2분 | debug 확인 |
| 4 | 민원인 e2e | 5분 | attachment 보존 확인 |
| 5 | CC7 shallow 불변 | 2분 | shallow route 유지 확인 |
| 6 | 커밋 + push | 1분 | - |

---

작성: 2026-05-12
