# 13.5 Region Action Plan Design

chapter route에서 attachment 삭제 문제를 해결하기 위한 region action plan 설계.

최종 수정: 2026-05-11

---

## 1. 문제 정의

### 현상 (민원인 관측)

| section | region type | remove | append | 결과 |
|---------|-----------|--------|--------|------|
| sec[0] | slot + chapter x8 | 322 | 65 | chapter body로 채워짐 |
| sec[1] | attachment | 85 | **0** | **secPr carrier만 남음** |
| sec[2] | attachment | 3 | **0** | **secPr carrier만 남음** |
| sec[3] | (secPr only) | 0 | 0 | 원래 비어있음 |
| sec[4] | attachment | 192 | **0** | **secPr carrier만 남음** |

### 근본 원인

chapter route의 assemble은 `header_indices` (보존 대상)에 속하지 않는 모든 paragraph를 삭제한다. `header_indices`는:

1. header_data로 텍스트가 설정된 role의 first idx (L912-919)
2. level-0 + non-title + no-children paragraphs (`_is_skip`) (L926-930)
3. 첫 번째 paragraph (L932-934)
4. secPr carrier paragraphs (L979-1005)
5. `preserve_indices` (shallow route에서만 전달) (L1007-1021)

attachment paragraphs는 1~4 어디에도 해당하지 않고, chapter route에서는 5가 None이다. → attachment 삭제.

---

## 2. 해결 구조: Region Action Plan

### 2.1 기본 원칙

target_unit_plan의 **모든 region을 한 번씩 방문**하여 action을 부여한다. "생성하지 않음"도 명시적 action이다.

### 2.2 Action 종류

| action | 의미 | preserve_indices에 포함? |
|--------|------|------------------------|
| `generate` | AI 2b가 새 content 생성 | 아니오 |
| `fill_slot` | header_data로 값 채움 | 아니오 (이미 header_indices 경로) |
| `preserve_original` | 원본 content 그대로 유지 | **예** |
| `skip_with_reason` | 명시적 건너뜀 (source 부족 등) | 아니오 |

### 2.3 Table Handling은 Region Action의 하위 정보

table은 독립 action이 아니라, region action 결정 후 해당 region 내 table에 대한 sub-policy:

| region action | table sub-policy | 설명 |
|--------------|-----------------|------|
| `preserve_original` | `preserved_with_region` | attachment 내 table → 원본 유지 |
| `generate` | `defer_table_filling` | chapter body 내 table → exemplar clone 유지, cell filling은 14-table |
| `fill_slot` | `not_applicable` | slot에 table 없음 (현재 양식 기준) |

### 2.4 Unit Type → Action 매핑 (chapter route)

| unit_type | action | 근거 |
|-----------|--------|------|
| `slot` | `fill_slot` | 2a header_data로 처리. 이미 header_indices에서 보존됨 |
| `chapter` | `generate` | 2b per-chapter generation |
| `shallow_block` | `preserve_original` | chapter-dominant 양식에서 shallow_block은 목차 등 보조 역할. level-0이면 이미 header_indices에서 보존됨. level-0이 아닌 경우 preserve_indices에 추가 |
| `attachment` | `preserve_original` | 원본 보존 |
| `table` | region의 상위 action 따름 | table은 독립 unit_type이 아닌 region 내 요소 |

### 2.5 Slot Policy 상세

**현재 보존 경로 (변경 없음):**
- slot paragraphs는 level=0 → `_is_skip`으로 `header_indices`에 포함
- 2a의 header_data로 텍스트 설정

**region action plan에서:**
- `action: "fill_slot"`, `in_preserve_set: false`
- `preserve_via_header: true` (header_indices 경로로 이미 보존)
- preserve_indices에 중복 추가하지 않음 — 기존 동작 변경 없음

**shallow route와의 차이:**
- shallow route: `compute_preserve_indices`가 slot indices를 preserve_indices에 포함
- chapter route: header_indices 경로로 이미 보존. preserve_indices에 넣지 않음
- 양쪽 모두 slot이 보존되는 결과는 동일. 경로만 다름

---

## 3. Schema

### compute_region_action_plan 출력

```python
{
    "actions": [
        {
            "region_id": 0,
            "unit_type": "slot",
            "action": "fill_slot",
            "paragraph_indices": [0, 1, 2],
            "paragraph_count": 3,
            "in_preserve_set": False,
            "preserve_via_header": True,
            "table_policy": "not_applicable",
            "reason": "slot — filled by 2a header_data, preserved via header_indices",
        },
        {
            "region_id": 1,
            "unit_type": "shallow_block",
            "action": "preserve_original",
            "paragraph_indices": [3],
            "paragraph_count": 1,
            "in_preserve_set": False,
            "preserve_via_header": True,
            "table_policy": "not_applicable",
            "reason": "shallow_block in chapter-dominant template, level-0 already in header_indices",
        },
        {
            "region_id": 2,
            "unit_type": "chapter",
            "action": "generate",
            "paragraph_indices": [4, 5, ..., 20],
            "paragraph_count": 17,
            "in_preserve_set": False,
            "preserve_via_header": False,
            "table_policy": "defer_table_filling",
            "reason": "chapter body — 2b generation target",
        },
        {
            "region_id": 5,
            "unit_type": "attachment",
            "action": "preserve_original",
            "paragraph_indices": [191, 192, ..., 291],
            "paragraph_count": 101,
            "in_preserve_set": True,
            "preserve_via_header": False,
            "table_policy": "preserved_with_region",
            "reason": "attachment content preserved as-is",
        },
    ],
    "preserve_indices": [191, 192, ..., 291],
    "summary": {
        "total_regions": 6,
        "actions": {
            "generate": {"count": 3, "paragraphs": 220},
            "fill_slot": {"count": 1, "paragraphs": 3},
            "preserve_original": {"count": 2, "paragraphs": 102},
            "skip_with_reason": {"count": 0, "paragraphs": 0},
        },
        "coverage": "all_regions_visited",
        "overlap_warnings": [],
    },
}
```

### preserve_indices 계산 규칙

preserve_indices에 들어가는 조건:
1. `in_preserve_set == True` (action이 `preserve_original`)
2. AND `preserve_via_header == False` (header_indices 경로로 이미 보존되지 않는 경우)

조건 2가 중요: level-0 slot/shallow_block은 header_indices에서 이미 보존되므로 preserve_indices에 중복 추가하지 않는다. attachment는 대부분 level > 0이므로 preserve_indices에 추가해야 한다.

---

## 4. 변경 파일 / 최소 Diff

| 파일 | 변경 | 비고 |
|------|------|------|
| `hwpx_analyzer.py` | `compute_region_action_plan()` 함수 추가 | target_unit_plan 순회 → action 부여 → preserve_indices 계산 |
| DB tool | chapter route 블록에서 `compute_region_action_plan` 호출 → preserve_indices → assemble 전달 | shallow route는 기존 `compute_preserve_indices` 유지 |
| `hwp_generator.py` | **변경 없음** | preserve_indices 파라미터 이미 존재. assemble 로직 불변 |
| `source_block_adapter.py` | **변경 없음** | `compute_preserve_indices` 유지 (shallow route 전용) |

### DB tool 변경 범위

```python
# chapter route 블록 (기존 assemble 호출 전에 추가)
from open_webui.utils.hwpx_analyzer import compute_region_action_plan

_region_plan = compute_region_action_plan(
    _tup, structure, idx_map=idx_map,
)
_chapter_preserve = set(_region_plan.get("preserve_indices", []))

# assemble 호출에 preserve_indices 추가
result = assemble_hwpx_hybrid(
    template_path, structure, content_data,
    removed_indices=removed_indices,
    idx_map=idx_map,
    chapter_trees=_valid_trees,
    content_only_mode=True,
    preserve_indices=_chapter_preserve if _chapter_preserve else None,
)

# debug에 기록
_debug_payload["region_action_plan"] = _region_plan
```

### 예상 diff 규모

- `hwpx_analyzer.py`: +80~100 lines (함수 1개)
- DB tool: +10 lines (import + 호출 + debug)
- 총: ~110 lines

---

## 5. 검증 기준

### Cheap Check

| 항목 | 확인 방법 |
|------|----------|
| 조달청: region_action_plan이 생성되는지 | `_debug_payload["region_action_plan"]` 존재 |
| 조달청: slot=fill_slot, chapter=generate | action 필드 확인 |
| 조달청: shallow_block(1p, 목차)이 preserve_via_header=True | level-0 확인 |
| 조달청: attachment 없음 → preserve_indices 비어있음 | preserve_indices=[] |
| 조달청: 기존 동작 불변 (grammar, assembly) | fail=0 |

### E2E (민원인)

| 항목 | 확인 방법 |
|------|----------|
| 민원인: attachment 101p가 preserve_indices에 포함 | preserve_indices 크기 ≈ 101 |
| 민원인: attachment-bearing section에 content 보존 | section[1,2,4]에서 secPr carrier 외에 attachment content 존재 |
| 민원인: chapter body는 preserve 아님 | generate action의 paragraphs가 preserve에 없음 |
| 민원인: assembly fail=0 | 기존과 동일 |
| 민원인: grammar 통과 | 기존과 동일 |

### E2E (CC7 shallow)

| 항목 | 확인 방법 |
|------|----------|
| CC7: shallow route 기존 `compute_preserve_indices` 경로 유지 | region_action_plan 없음 (shallow는 미적용) |
| CC7: section plan seed 동작 불변 | seed heading_count=4, compliance match=True |

### Overlap Warning 검증

| 항목 | 확인 방법 |
|------|----------|
| generation 대상과 preserve 대상이 겹치지 않음 | overlap_warnings=[] |
| 모든 region이 action을 받았음 | coverage="all_regions_visited" |

---

## 6. 하지 않을 것

| 항목 | 이유 |
|------|------|
| table cell filling | 14-table |
| source allocation redesign | 13.7 |
| shallow route `compute_preserve_indices` 대체 | 기존 검증 경로 유지 |
| section-aware append 변경 | 9.2b / later |
| assemble 내부 로직 수정 | preserve_indices 전달만으로 충분 |
| 2a/2b prompt 변경 | 불필요 |
| HWP 본문에 annotation 삽입 | production 위험 |
| per-paragraph action 계산 | region 단위로 충분 |

---

## 7. 비판적 검토

### 7.1 shallow_block in chapter route: preserve_original이 항상 맞는가?

조달청의 shallow_block(1p, 목차)은 level-0이라 이미 header_indices에서 보존된다. `in_preserve_set: false, preserve_via_header: true`.

**만약 level > 0인 shallow_block이 chapter route에 있으면?** 이 경우 header_indices에 안 들어가므로 삭제될 수 있다. `in_preserve_set: true`로 해서 보존해야 한다.

**현재 3개 양식에서 이 케이스 없음.** shallow_block이 chapter route에 공존하는 건 조달청뿐이고 level-0이다. 구현에서는 level-0 여부를 체크하여 `preserve_via_header` 판단.

### 7.2 idx_map 적용 순서

`compute_preserve_indices`는 idx_map을 적용하여 AI idx → real idx 변환한다. `compute_region_action_plan`도 동일하게 적용해야 한다.

target_unit_plan의 `paragraph_indices`는 AI idx (truncated XML 기준). assemble의 `header_indices`는 real idx. 변환 필수.

### 7.3 target_unit_plan이 없는 경우

cache가 12.2 이전 버전이면 `target_unit_plan`이 없을 수 있다. 이 경우:
- `compute_region_action_plan` → None 반환
- chapter route: 기존 동작 (preserve_indices=None)
- fallback 명확, 기존 동작 유지

### 7.4 region paragraph_indices가 실제 template과 불일치하는 경우

12.2 AI가 잘못된 paragraph_indices를 부여했을 수 있다. 이 경우 preserve_indices에 존재하지 않는 idx가 들어간다. assemble에서는 `header_indices`에 해당 idx가 있지만 실제 paragraph가 없으므로 그냥 무시된다 (해 없음).

overlap_warnings에 "preserve index out of range" 체크 추가 가능.

---

## 8. 구현 순서

| # | 작업 | 검증 |
|---|------|------|
| 1 | `compute_region_action_plan()` 함수 작성 | mock data unit test |
| 2 | DB tool chapter route에 호출 추가 | 조달청 cheap check |
| 3 | 민원인 e2e | attachment 보존 확인 |
| 4 | CC7 e2e | shallow route 불변 확인 |
| 5 | 커밋 | - |

---

최종 수정: 2026-05-11
