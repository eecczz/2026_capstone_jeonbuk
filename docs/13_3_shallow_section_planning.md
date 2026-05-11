# 13.3 Shallow Section Planning

shallow 2b single-call에서 template 보고서 흐름이 무시되는 문제를 해결하는 설계서.

최종 수정: 2026-05-11

---

## 1. 문제 정의

### 현상

CC7 template의 원본 보고서 구조:
```
□ 개요
  ○ 운영기간/현황
□ 주간 AI 활용 현황
  ○ 사용자/질문건수/표/순위
□ 추진상황
  ○ 기능개선/출연기관/정부포상
□ 향후계획
  ○ 계획1/계획2
```

현재 shallow 2b 생성 결과:
```
□ 문제정책 관리제도       (source topic 1)
□ 외국국적동포 방문취업제   (source topic 2)
□ 문서관리카드            (source topic 3)
```

**template의 보고서 흐름 (개요→현황→추진→계획)이 무시됨.** AI가 source topic별로 section을 재구성함.

### 근본 원인

chapter route에는 2a(planning) → 2b(generation) 2단계가 있다. AI가 구조를 결정하는 단계와 content를 채우는 단계가 분리되어 있다.

shallow route에는 planning 단계가 없다. 2b가 구조 + content를 동시에 결정 → AI가 source 구조를 우선한다.

현재 prompt에 "소제목 의도 유지" 지시가 있지만, **어떤 소제목이 있는지 구체적으로 알려주지 않는다.** AI는 pattern의 role 정의만 보고, 실제 heading texts를 모른다.

### 문제의 핵심

AI에게 "이 양식의 흐름을 따르라"고 했지만, "이 양식의 흐름이 뭔지"를 안 알려줬다.

---

## 2. 사용 가능한 데이터

### 2.1 idx_full_texts (cache)

cache에 paragraph별 full text가 저장되어 있다 (11.2에서 추가, CACHE_SCHEMA_VERSION 4).

CC7 template의 heading texts:
```json
{
  "2": "□ 개    요",
  "5": "□ 주간 AI 활용 현황 (3.30.~4.5.)",
  "11": "□ 추진상황 (3.30. 주간)",
  "18": "□ 향후계획"
}
```

### 2.2 target_unit_plan의 shallow_block region (12.2)

```json
{
  "region_id": 1,
  "unit_type": "shallow_block",
  "paragraph_indices": [2, 3, 4, 5, 6, ..., 20],
  "internal_structure": {
    "has_substructure": true,
    "subregion_candidates": [
      {
        "role_id": "role_cluster_2",
        "candidate_type": "heading",
        "description": "대분류 섹션 제목"
      }
    ]
  }
}
```

`subregion_candidates`에서 `candidate_type == "heading"`인 role을 알 수 있다.

### 2.3 structure.paragraphs (1a cache)

```json
[
  {"idx": 2,  "role": "role_cluster_2", "level": 1, "marker": "□", "description": "..."},
  {"idx": 5,  "role": "role_cluster_2", "level": 1, "marker": "□", "description": "..."},
  {"idx": 11, "role": "role_cluster_2", "level": 1, "marker": "□", "description": "..."},
  {"idx": 18, "role": "role_cluster_2", "level": 1, "marker": "□", "description": "..."}
]
```

### 2.4 marker_policy_1f (cache)

role_cluster_2의 marker = "□", policy_type = "fixed_char". heading text에서 marker를 strip할 때 사용.

### 요약: 신규 AI 호출 불필요

4개 소스 (idx_full_texts, target_unit_plan, structure.paragraphs, marker_policy_1f) 모두 cache에 있다. section plan 추출은 **코드만으로 가능**하다.

---

## 3. 선택지 비교

### Option A: Prompt에 section plan 추가 (1회 호출 유지)

template heading texts를 2b prompt에 "이 섹션들을 따르라" 지시로 전달.

- 장점: 최소 변경, 신규 AI 호출 없음, 기존 schema/parser/validation/assemble 재사용
- 단점: AI가 prompt 지시를 무시할 수 있음 (현재 문제의 변형)
- 리스크 관리: compliance 검증을 추가하여 무시 여부 감지

### Option B: Shallow 2a (별도 AI 호출로 section plan 생성)

section plan을 AI가 생성. template heading texts + source 요약을 보고 "이 양식을 이 source에 맞게 재편성하라" 지시.

- 장점: AI가 source-aware하게 section 구조를 결정
- 단점: 추가 AI 호출 (latency + cost), 새 prompt/parser/validation 필요
- 적용 시점: Option A 실패 시

### Option C: Per-section 2b multi-call

section plan의 각 섹션에 대해 별도 2b 호출.

- 장점: section별 독립 생성, source allocation이 section 단위
- 단점: chapter route와 거의 동일 → shallow의 의미 감소, multi-call latency
- 적용 시점: Option B로도 부족할 때

### 결정: Option A 우선 시도

**근거**: 현재 문제는 "AI가 template 구조를 모른다"이지 "AI가 알면서도 무시한다"가 아니다. 구체적 heading texts를 제공하면 해결될 가능성이 높다. 실패하면 B로 escalate.

---

## 4. 추출 알고리즘

### extract_shallow_section_plan

```python
def extract_shallow_section_plan(
    target_unit_plan: dict,
    structure: dict,
    idx_full_texts: dict,
    marker_policies: dict | None = None,
) -> dict | None:
```

**입력:**
- `target_unit_plan`: 12.2에서 생성, cache에 저장
- `structure`: 1a~1f 분석 결과
- `idx_full_texts`: paragraph별 full text
- `marker_policies`: 1f marker policy (optional, marker strip용)

**알고리즘:**
1. `target_unit_plan.ai_plan.regions`에서 `unit_type == "shallow_block"` region을 찾는다
2. 해당 region의 `internal_structure.subregion_candidates`에서 `candidate_type == "heading"` role을 수집한다
3. region의 `paragraph_indices`에서 heading role인 paragraph만 필터링한다
4. 각 paragraph의 full text를 `idx_full_texts`에서 가져온다
5. marker를 strip하고 whitespace를 정규화한다

**출력:**
```json
{
  "heading_count": 4,
  "heading_role": "role_cluster_2",
  "headings": [
    {"idx": 2,  "text_raw": "□ 개    요", "text_clean": "개요"},
    {"idx": 5,  "text_raw": "□ 주간 AI 활용 현황 (3.30.~4.5.)", "text_clean": "주간 AI 활용 현황 (3.30.~4.5.)"},
    {"idx": 11, "text_raw": "□ 추진상황 (3.30. 주간)", "text_clean": "추진상황 (3.30. 주간)"},
    {"idx": 18, "text_raw": "□ 향후계획", "text_clean": "향후계획"}
  ]
}
```

### Marker stripping

```python
def _clean_heading_text(raw_text: str, marker: str = "") -> str:
    text = raw_text.strip()
    if marker and text.startswith(marker):
        text = text[len(marker):].strip()
    # Collapse runs of whitespace to single space
    return " ".join(text.split())
```

- "□ 개    요" → "개 요" (whitespace collapse, semantic은 "개요")
- "□ 주간 AI 활용 현황 (3.30.~4.5.)" → "주간 AI 활용 현황 (3.30.~4.5.)"
- "□ 추진상황 (3.30. 주간)" → "추진상황 (3.30. 주간)"
- "□ 향후계획" → "향후계획"

"개 요"가 "개요"가 아닌 점: 원문이 "개    요"로 장식 간격이 있다. whitespace collapse로 "개 요"가 됨. AI는 이를 "개요"로 이해한다. 과도한 text normalization은 피한다 (장식 간격 감지 heuristic은 fragile).

### 반환값이 None인 경우

1. shallow_block region이 없음 (chapter-dominant 양식 → section plan 불필요)
2. subregion_candidates에 heading이 없음 (flat shallow block)
3. heading role인 paragraph가 0개

이 경우 section plan 없이 기존 shallow prompt 그대로 실행. section plan은 **있으면 사용, 없으면 생략** — hard requirement가 아니다.

---

## 5. Prompt 통합 설계

### build_section_fill_prompt 변경

```python
def build_section_fill_prompt(
    ...,
    shallow_mode: bool = False,
    section_plan: dict | None = None,   # 신규 파라미터
) -> list[dict]:
```

### Prompt에 추가할 section plan 블록

`shallow_mode=True`이고 `section_plan`이 있을 때, 기존 shallow mode 지시 뒤에 삽입:

```
## Template Section Plan (이 양식의 보고서 흐름)

이 양식은 다음 {N}개 섹션으로 구성됩니다:
{headings_list}

**중요 규칙:**
- 위 섹션 순서와 구조를 따르세요. 소스의 주제별로 재구성하지 마세요.
- 소스의 여러 주제를 위 보고서 흐름 안에 요약·통합하세요.
- 각 섹션 제목은 위 구조를 기반으로 하되, 소스 주제에 맞게 표현을 바꿀 수 있습니다.
- 날짜·기간·고유 주제명 등 양식 원본에 특화된 표현은 소스 내용에 맞게 바꾸세요.
- 소스에 해당 내용이 없는 섹션은 간략 요약으로 채우거나, 해당 role의 item을 최소한으로 생성하세요. 섹션을 통째로 생략하지 마세요.
```

`{headings_list}` 예시:
```
1. 개 요
2. 주간 AI 활용 현황 (3.30.~4.5.)
3. 추진상황 (3.30. 주간)
4. 향후계획
```

### 기존 shallow mode 지시와의 관계

기존 5항 지시를 유지하되, item 5 ("소제목 의도 유지")를 section plan이 있으면 대체한다.

변경 전 (item 5):
> 소제목 의도 유지: 양식의 소제목에는 문서 구조 역할과 원본 주제 표현이 섞여 있을 수 있습니다. 구조 역할은 유지하고, 원본 주제에 특화된 표현은 새 소스 주제에 맞게 바꾸세요. 소스의 주제 수만큼 섹션 구조를 재편하지 말고, 양식의 보고서 흐름 안에 소스 내용을 요약·통합하세요.

변경 후:
> 위의 **Template Section Plan**을 따르세요.

기존 item 5는 "section plan이 뭔지 모르면서 의도를 유지하라"는 추상 지시. section plan이 있으면 구체적 heading 목록이 이를 대체한다. section plan이 없으면(None) 기존 item 5 그대로 유지 (fallback).

---

## 6. Compliance 검증 (debug-only)

### 목적

AI가 section plan을 따랐는지 사후 확인. **hard gate가 아니라 debug 관측**.

### 검증 항목

```json
{
  "section_plan_compliance": {
    "plan_heading_count": 4,
    "generated_heading_count": 3,
    "heading_role": "role_cluster_2",
    "count_match": false,
    "generated_headings_preview": ["개요", "추진상황", "향후계획"],
    "missing_estimate": 1,
    "order_plausible": true
  }
}
```

### 검증 방법

`process_section_fill_result` 이후, 생성된 body_items에서:
1. heading_role과 같은 role을 가진 item 수를 센다 → `generated_heading_count`
2. plan_heading_count와 비교 → `count_match`
3. heading item의 content text를 `generated_headings_preview`로 기록
4. 완전 일치를 요구하지 않음 — 개수와 순서가 대략 맞는지만 관측

### compliance가 낮으면?

- 1~2회 실행에서 낮으면: watch (AI randomness 가능)
- 3회 이상 반복 실패: Option B (별도 planning AI 호출)로 escalate
- escalation 판단은 수동 (자동 gate 아님)

---

## 7. 구현 범위

### 변경 파일

| 파일 | 변경 |
|------|------|
| `hwpx_analyzer.py` | `extract_shallow_section_plan()` 함수 추가. `build_section_fill_prompt()`에 `section_plan` 파라미터 + prompt 블록 추가. |
| DB tool | shallow route 블록에서 `extract_shallow_section_plan` 호출 → `build_section_fill_prompt`에 결과 전달. compliance debug 기록. |

### 변경하지 않는 파일

| 파일 | 이유 |
|------|------|
| `hwp_generator.py` | assemble 로직 변경 없음 (preserve_indices, table skip 그대로) |
| `source_block_adapter.py` | source 관련 변경 없음 |
| `template_observer.py` | template observation 변경 없음 |
| `target_unit_planner.py` | target_unit_plan 구조 변경 없음 |

### 캐시 영향

없음. `extract_shallow_section_plan`은 cache 이후 단계에서 cache 데이터를 읽기만 한다. 캐시 삭제 불필요.

### DB tool 변경 범위

```python
# 기존 shallow route block에 추가:
from open_webui.utils.hwpx_analyzer import extract_shallow_section_plan

# section plan 추출 (cache 데이터 사용)
_section_plan = extract_shallow_section_plan(
    target_unit_plan, structure, idx_full_texts,
    marker_policies=structure.get("marker_policy_1f")
)

# 2b prompt 구성 시 전달
prompt = build_section_fill_prompt(
    ...,
    shallow_mode=True,
    section_plan=_section_plan,
)

# debug에 기록
_debug_payload["shallow_section_plan"] = _section_plan
```

---

## 8. 성공 기준

### 필수 (blocker)

1. **CC7 생성 결과가 template 보고서 흐름을 따른다** — 개요/현황/추진/계획 구조가 보존되고, source topic별로 재구성되지 않는다.
2. **조달청 regression 없음** — chapter route 동작 불변, grammar pass, assemble fail=0.
3. **section plan이 None일 때 기존 동작 유지** — section plan이 없는 양식(chapter-dominant)에서 기존 코드에 영향 없음.

### 관측 (watch)

4. **compliance 수치** — generated_heading_count가 plan과 ±1 이내.
5. **heading text adaptation** — topic-specific heading이 source에 맞게 바뀌었는지 (자동 판정 불가, 수동 확인).
6. **민원인 regression** — chapter route 동작 유지.

### 불필요

- heading text 완전 일치 (adaptation 허용)
- compliance hard gate (debug-only)
- 다른 양식 추가 검증 (현재 양식 3개로 충분)

---

## 9. 비판적 검토

### 비판 1: Option A가 충분한가?

**우려**: 현재도 prompt에 "소제목 의도 유지"라고 적혀 있는데 AI가 무시한다. heading texts를 추가해도 무시할 수 있다.

**반박**: 현재 prompt는 추상적 지시 ("의도를 유지하라")이지만 **구체적 대상을 알려주지 않는다**. AI는 pattern에서 role_cluster_2가 heading임은 알지만, "개요/현황/추진/계획"이라는 실제 구조는 모른다. 구체적 heading list를 제공하면 "follow this structure" 지시가 실체를 갖는다.

**남은 리스크**: AI가 heading list를 보고도 source topic으로 재구성할 가능성은 있다 (특히 source가 template 구조와 완전히 다를 때). 이 경우 compliance 검증에서 잡히고 Option B로 escalate.

### 비판 2: subregion_candidates 의존 (AI output)

**우려**: `subregion_candidates`는 12.2 AI가 생성한 필드. heading role 식별을 잘못하면 section plan이 엉뚱해진다.

**반박**:
- heading role 식별은 비교적 안정적 — CC7에서 role_cluster_2가 모든 □ heading을 커버하는 것은 1a~1e clustering의 결과이며, 12.2 AI가 아닌 code-driven.
- `subregion_candidates`의 `candidate_type: "heading"`은 AI 판단이지만, role이 heading인지는 has_children, level, text_type 등 code-level evidence에 기반한다.
- fallback: subregion_candidates가 없거나 heading이 없으면 section plan = None → 기존 동작 유지.

**보강 가능**: structure.paragraphs의 level/has_children으로 code-level cross-check 추가 가능. 하지만 현재 데이터에서 식별 오류가 관측되지 않았으므로 premature.

### 비판 3: Heading text의 date/topic-specific 부분

**우려**: "주간 AI 활용 현황 (3.30.~4.5.)" — 날짜 부분을 AI가 어떻게 처리하는가? 그대로 복사하면 잘못된 날짜가 출력된다.

**반박**: prompt에 "날짜·기간·고유 주제명은 소스 내용에 맞게 바꾸세요"를 명시한다. AI는 이런 adaptation에 강하다. 날짜를 그대로 복사하면 compliance debug에서 heading text를 확인 가능.

**남은 리스크**: source에 날짜가 없으면 AI가 template 날짜를 그대로 쓸 수 있다. 이는 content 품질 문제이지 구조 문제가 아니므로 watch.

### 비판 4: heading 수와 source content의 불일치

**우려**: template이 4 section인데 source가 2개 주제만 다루면? 빈 section이 생긴다.

**반박**: prompt에 "소스에 해당 내용이 없는 섹션은 간략 요약으로 채우거나 최소한으로 생성하세요. 섹션을 통째로 생략하지 마세요"를 명시한다. 빈 section보다 구조 보존이 우선. source가 부족하면 요약/placeholder가 들어가는 것이 source topic별 재구성보다 낫다.

**대안**: compliance에서 generated_heading_count < plan_heading_count면 warning. 반복되면 Option B (AI가 source-aware하게 section plan을 adapt).

### 비판 5: 일반화 가능성 — 다른 shallow 양식에서도 동작하는가?

**우려**: CC7에 과적합된 설계가 아닌가?

**검증**:
- 알고리즘은 role_cluster 번호를 하드코딩하지 않는다 (subregion_candidates에서 heading role을 동적으로 가져옴).
- heading text를 하드코딩하지 않는다 (idx_full_texts에서 동적으로 가져옴).
- shallow_block region이 없으면 None 반환 (chapter-dominant 양식에 영향 없음).
- **추가 양식이 나왔을 때 검증 필요하지만, 현재 설계에 양식 과적합 요소는 없다.**

### 비판 6: compliance 검증의 실효성

**우려**: heading_count만 비교해서는 "형식만 맞추고 내용이 엉뚱한" 경우를 못 잡는다.

**맞다.** content 품질 검증은 이 단계의 범위 밖이다. compliance 검증은 구조적 skeleton만 확인하며, content 적합성은 수동 확인이 필요하다. 이는 15단계(source coverage)에서 체계적으로 다룰 문제.

### 비판 7: "개 요" 같은 장식 간격 문제

**우려**: "개    요" → "개 요"가 되는데, AI가 "개 요"를 heading으로 출력하면 깨진 텍스트처럼 보인다.

**반박**: AI는 "개 요"를 보고 "개요"(overview)로 이해하며, 생성 시 자연스러운 "개요"를 출력할 가능성이 높다. 실제 출력 heading text는 AI가 결정하므로 입력의 장식 간격이 출력에 전파되지 않는다. marker reattach가 출력 marker를 처리하므로, heading text 자체는 content-only.

---

## 10. 실패 시 Fallback

### Option A 실패 판정 기준

- CC7에서 3회 실행 중 2회 이상 section plan 무시 (source topic별 재구성 지속)
- compliance: generated_heading_count가 plan_heading_count의 절반 미만

### Fallback: Option B (Shallow 2a)

1. 별도 AI 호출로 section plan 생성
   - 입력: template heading texts + source 요약 (source_blocks의 heading_path)
   - 출력: adapted section plan (heading texts를 source에 맞게 변형)
   - schema: `{sections: [{heading: str, intent: str, source_block_ids: list}]}`
2. 생성된 section plan을 2b prompt에 전달 (Option A와 동일한 prompt 구조)
3. 추가 비용: AI 1회 호출 (latency + cost)

### Fallback: Option C (Per-section 2b)

- section plan의 각 섹션을 별도 2b 호출로 생성
- chapter route와 유사한 구조
- shallow의 "단일 body" 특성을 포기하는 것이므로, 정말 필요한 경우에만

### Escalation 순서

A (prompt에 heading list) → 실패 → B (shallow 2a + 2b) → 실패 → C (per-section 2b)

---

## 11. 하지 않을 것

| 항목 | 이유 |
|------|------|
| heading text의 structural vs topic-specific 자동 분류 | AI가 판단. code hard-rule 아님 |
| heading text 완전 정규화 (장식 간격 제거) | fragile heuristic. AI가 이해하므로 불필요 |
| compliance hard gate | debug-only. false positive 위험 |
| section plan 없는 양식에 강제 적용 | None이면 기존 동작 유지 |
| per-section source allocation | 13.1 deferred. shallow는 broad source |
| 2b prompt의 나머지 부분 수정 | section plan 블록 추가만. 기존 content-only, pattern, role 지시 변경 없음 |
| table cell filling | 14-table 단계 |

---

## 12. 구현 순서

| # | 작업 | 검증 | 비고 |
|---|------|------|------|
| 1 | `extract_shallow_section_plan()` 함수 작성 | unit test 수준: CC7 cache 데이터로 4개 heading 추출 확인 | hwpx_analyzer.py |
| 2 | `build_section_fill_prompt(section_plan=...)` 수정 | prompt 문자열에 section plan 블록 포함 확인 | hwpx_analyzer.py |
| 3 | DB tool에 section plan 추출 + 전달 코드 추가 | shallow route에서 section_plan이 debug에 기록되는지 확인 | DB tool |
| 4 | CC7 실행 | generated content가 개요/현황/추진/계획 구조를 따르는지 확인 | e2e |
| 5 | 조달청 실행 | regression 없음 확인 (chapter route 불변) | e2e |
| 6 | compliance debug 추가 | section_plan_compliance 필드가 debug에 기록되는지 확인 | hwpx_analyzer.py 또는 DB tool |
| 7 | 커밋 | section plan 추출 + prompt 통합 + compliance debug | - |

**cheap check (구현 전):**
- CC7 cache에 idx_full_texts가 있는지 확인 → 있음 (CACHE_SCHEMA_VERSION 4)
- target_unit_plan에 subregion_candidates가 있는지 확인 → 있음 (12.2)

---

## 13. DB tool 변경 시 위험 관리

- DB tool 변경은 서버 재시작 불필요 (exec으로 매번 로드)
- 웹 UI에서 도구 편집기 열면 변경이 날아갈 수 있음 → 핵심 로직은 서버 .py에
- `extract_shallow_section_plan`은 서버 코드 (hwpx_analyzer.py) → 안전
- DB tool에는 호출 + 결과 전달만 (최소한의 glue code)

---

## 14. 전체 흐름 (section plan 적용 후)

```
[cache에서 로드]
  structure, idx_full_texts, target_unit_plan, marker_policy_1f

[routing]
  should_use_shallow_route(target_unit_plan)
    → shallow route 진입

[section plan 추출] ← 신규
  extract_shallow_section_plan(target_unit_plan, structure, idx_full_texts, marker_policy_1f)
    → {heading_count: 4, headings: [{idx, text_raw, text_clean}, ...]}

[2b prompt 구성]
  build_section_fill_prompt(..., shallow_mode=True, section_plan=section_plan)
    → system prompt에 "Template Section Plan" 블록 포함

[2b LLM 호출]
  → content-only body items (marker 없음)

[결과 처리]
  process_section_fill_result(..., shallow_mode=True)
    → body_items, grammar validation

[compliance 검증] ← 신규
  heading_role items count vs plan heading_count
  → debug에 section_plan_compliance 기록

[assemble]
  assemble_hwpx_hybrid(..., preserve_indices=preserve_indices)
  → table text skip, slot/attachment 보존
```

---

최종 수정: 2026-05-11
