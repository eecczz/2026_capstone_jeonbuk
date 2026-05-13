# 13.6 Plan — Per-Chapter Subtree + Multi-Section/Source Diagnostic

## 성격

13.6은 gate 단계. 확인된 gap 1개를 구현하고, 의심 항목 2개를 측정하여 13.7 범위를 확정한다.

| 항목 | 13.6 역할 | 13.7 이후 |
|------|----------|----------|
| **B: Per-chapter subtree extraction** | **구현** | 민원인 multi-section 연동 검증 |
| **A: Multi-section analysis coverage** | **blocker 후보 diagnostic** | 진단 결과 blocker면 구현 |
| **C: Source allocation** | **diagnostic** | 측정 결과 기반 구현 |

---

## B: Per-Template-Chapter Subtree Extraction — 구현

### 문제

현재 `_find_dominant_chapter_type`이 type 1개를 선택하여 모든 chapter에 동일 pattern/catalog을 적용한다. 조달청 실측에서 3개 chapter의 local pattern이 명백히 다름이 확인됨:

```
Ⅰ.평가:  17p, roles 6종 (c5/c6/c7/c9),    max_level=3
Ⅱ.여건:  17p, roles 7종 (c10/c11 등장),     max_level=4
Ⅲ.과제: 186p, roles 11종 (c12~c18 고유),    max_level=6
```

`chapter_types`에 type_1~type_4까지 4개 type이 정의되어 있지만, dominant 1개만 사용.

### 원인

- `extract_chapter_template_plan_seed`가 `dominant_chapter_type` 하나를 반환
- DB tool의 template-driven loop가 이 하나의 type에서 pattern/catalog을 가져옴
- per-chapter type mapping 또는 per-chapter subtree 추출이 없음

### 왜 지금 중요한가

- 대제목 흐름은 13.4b에서 보존됐지만, 내부 양식이 뭉개지면 chapter-local intent가 손실
- Ⅰ장(평가)에 Ⅲ장(과제) pattern이 들어가거나, 186p 규모의 Ⅲ장에 17p 규모의 Ⅰ장 pattern이 적용될 수 있음
- 조달청은 single-section이므로 A(multi-section) 없이 바로 구현+검증 가능

### 해결 방향

dominant chapter_type을 모든 chapter에 적용하는 대신, 각 chapter region의 실제 paragraph에서 local subtree를 직접 추출.

```
현재:  chapter_types → dominant_type → 모든 chapter에 같은 pattern/catalog
변경:  chapter region paragraphs → per-chapter local pattern/catalog → 각 chapter 고유 pattern
```

### 구현 범위

#### 1. `extract_per_chapter_pattern()` — hwpx_analyzer.py 신규 함수

**입력**: chapter region의 paragraph_indices + structure (paragraphs, idx_full_texts)

**로직**:
1. chapter region의 paragraph_indices 수집
2. 각 paragraph의 role, level, parent_idx 조회
3. title paragraph 식별 (region 첫 paragraph)
4. body paragraph들로 role hierarchy 구축:
   - parent_idx 기반 parent-child 관계 추출
   - 같은 role이 2회 이상 → repeatable 후보
   - children은 parent_idx가 해당 role의 paragraph를 가리키는 role들
5. local catalog 구축: 각 role의 첫 paragraph text를 exemplar로

**출력**:
```python
{
    "local_pattern": {
        "role_cluster_5": {"repeatable": False, "children": {}},
        "role_cluster_6": {"repeatable": True, "children": {"role_cluster_7": {...}}},
        ...
    },
    "local_catalog": {
        "role_cluster_5": {"exemplar": "...", "count": 1},
        "role_cluster_6": {"exemplar": "...", "count": 3},
        ...
    },
    "local_title_role": "role_cluster_4",
    "stats": {
        "role_count": 6,
        "max_depth": 3,
        "paragraph_count": 17,
    },
    "extraction_confidence": "high" | "medium" | "low",
    "fallback_to_dominant": False,
}
```

**confidence 판단**:
- high: parent_idx 기반 hierarchy 구축 성공, role 3종 이상
- medium: hierarchy 일부 불완전하지만 role 추출 성공
- low: parent_idx 데이터 부족 또는 role 1종 이하 → fallback 권장

**local_pattern schema 호환성** (필수 cheap check):
- local_pattern의 shape가 기존 `chapter_types[type_name]["pattern"]`과 동일해야 함
- `{role: {"repeatable": bool, "children": {child_role: {...}}}}` 형식 준수
- catalog role key와 pattern role key가 일치해야 함
- 구현 후 unit test에서 shape 비교: `set(local_pattern.keys())` vs 해당 chapter의 실제 role set
- fallback 시 기존 dominant pattern이 그대로 사용되는지 검증

**local_catalog exemplar 제한**:
- exemplar text는 길이 제한을 둔다 (max ~200 chars, 기존 catalog 수준 유지)
- text 없는 paragraph는 exemplar에서 제외하거나 `"(empty)"` 명시
- table/attachment-like paragraph (is_tbl_box 등)는 exemplar에 포함하지 않거나 `"(table)"` 명시
- local_catalog는 generation guide이지 source content가 아님 — 원문을 과다하게 넣지 않음

#### 2. `extract_chapter_template_plan_seed()` 확장

기존 seed의 `{template_title, description, position, total_chapters, paragraph_count}`에 추가:
- `local_pattern`: per-chapter pattern dict (또는 None → fallback)
- `local_catalog`: per-chapter role exemplar catalog (또는 None → fallback)
- `local_title_role`: 해당 chapter의 title role
- `pattern_source`: `"per_chapter_subtree"` 또는 `"dominant_type_fallback"`

#### 3. DB tool template-driven loop 변경

현재:
```python
_seed_ch_type = _chapter_plan_seed.get("dominant_chapter_type", "")
_seed_type_info = chapter_types.get(_seed_ch_type, {})
_seed_pattern = _seed_type_info.get("pattern", {})
_seed_catalog = {r: full_role_catalog[r] for r in _seed_pattern_roles if r in full_role_catalog}
# → 모든 chapter에 동일
```

변경:
```python
for ch_idx, tpl_ch in enumerate(_seed_chapters):
    # per-chapter pattern/catalog 우선, 없으면 dominant fallback
    _ch_pattern = tpl_ch.get("local_pattern") or _seed_pattern
    _ch_catalog = tpl_ch.get("local_catalog") or _seed_catalog
    _ch_title_role = tpl_ch.get("local_title_role") or _seed_title_role
    _pattern_source = tpl_ch.get("pattern_source", "dominant_type_fallback")
    # build_section_fill_prompt에 _ch_pattern, _ch_catalog 전달
```

#### 4. debug 기록

per_chapter_status에 추가:
- `pattern_source`: per_chapter_subtree / dominant_type_fallback
- `local_role_count`, `local_max_depth`
- fallback 발생 시 reason

### subtree boundary 원칙

- 고정 level로 자르지 않음 (level==0 가정 금지)
- 13.6에서는 `target_unit_plan`의 chapter region paragraph_indices를 **initial boundary**로 사용
- 단, region boundary 자체도 완벽하다고 단정하지 않음. 아래 항목을 debug warning으로 검증:
  - region 내부 paragraph 누락 (region에 속해야 할 paragraph가 빠진 경우)
  - 다음 chapter 침범 (region paragraphs가 인접 chapter의 범위와 겹치는 경우)
  - parent/level 불일치 (region 내 paragraph의 parent_idx가 region 밖을 가리키는 경우)
- boundary 문제가 확인되면 13.7에서 boundary detection을 재검토
- region 내부에서 parent_idx 기반으로 hierarchy 재구성

### fallback

- `extraction_confidence == "low"` → dominant chapter_type pattern/catalog 사용
- fallback 발생 시 debug에 `pattern_source: "dominant_type_fallback"` + reason 기록
- fallback은 regression 방지용 — 기존 동작 그대로

### 검증

| 양식 | 확인 항목 |
|------|----------|
| **조달청** | Ⅰ/Ⅱ/Ⅲ 각각 다른 local_pattern 추출. pattern_source=per_chapter_subtree. role_count 6/7/11, max_depth 3/4/6 |
| **조달청** | assembly fail=0, generation 품질 regression 없음 |
| **민원인** | per-chapter pattern 추출 동작 (multi-section이므로 section0 범위 chapter만) |
| **CC7** | shallow route 불변 (per-chapter subtree 미적용) |

### 하지 않을 것

- 2b prompt format 변경 (pattern/catalog 형식은 기존 그대로, 내용물만 per-chapter)
- chapter_types 삭제/변경 (fallback + debug용으로 유지)
- subtree boundary 별도 AI 호출 (target_unit_plan region이 이미 boundary)
- multi-section chapter subtree (A diagnostic 결과에 따라 13.7)
- source allocation 변경 (C diagnostic 결과에 따라 13.7)

### 13.7로 넘길 것

- 민원인 multi-section에서 per-chapter subtree가 section 경계를 넘는 경우 처리
- per-chapter pattern의 repeatable 판단 정밀화 (grammar 교차)
- per-chapter pattern 기반 source volume 추정

---

## A: Multi-Section Analysis Coverage — blocker 후보 diagnostic

### 문제

`extract_section_xml()`이 section0만 반환. sections 1-4는 분석되지 않음. 13.5에서 unanalyzed section preserve safety로 보존 중.

### diagnostic 목적

**"non-section0에 생성 대상이 있는가?"** 를 확인하여 blocker 여부를 결정.

- 전부 attachment/reference → **not a blocker** (preserve가 정답, 13.5 안전장치 유지)
- 생성/수정 대상이 있음 → **blocker** → 13.7에서 구현

### diagnostic 구현

#### `diagnose_multi_section()` — hwpx_analyzer.py 신규 함수

**입력**: HWPX file path

**로직**:
1. HWPX zip에서 모든 section XML 파일 목록 추출
2. 각 section에 대해:
   - 파일 크기 (chars)
   - body-level paragraph 수 (top-level `<hp:p>` count)
   - 텍스트 있는 paragraph 비율
   - table 수
   - 첫 몇 paragraph의 text preview (10개)
   - section 이름에서 역할 추정 (section0=본문, section1+=붙임 가능성)

**출력**:
```python
{
    "section_count": 5,
    "sections": [
        {
            "name": "Contents/section0.xml",
            "index": 0,
            "chars": 1877547,
            "body_paragraph_count": 322,
            "text_paragraph_ratio": 0.85,
            "table_count": 12,
            "text_preview": ["첫 문단...", "둘째 문단...", ...],
            "likely_role": "main_body",
            "confidence": "high",
            "evidence": ["largest section", "first section", "most paragraphs"],
            "ambiguity_flags": [],
        },
        {
            "name": "Contents/section1.xml",
            "index": 1,
            "chars": 251566,
            "body_paragraph_count": 85,
            ...
            "likely_role": "attachment_or_reference",
            "confidence": "medium",
            "evidence": ["non-first section", "smaller than section0", "high table ratio"],
            "ambiguity_flags": ["paragraph count > 50 — could contain body content"],
        },
        ...
    ],
    "analysis_coverage": {
        "analyzed": [0],
        "unanalyzed": [1, 2, 3, 4],
    },
    "blocker_signal": "none" | "weak" | "strong",
    "blocker_reason": "...",
}
```

**likely_role 원칙**:
- `likely_role`은 heuristic signal이지 확정 정책이 아님
- "section1 이상은 attachment" 같은 규칙은 하드코딩 정책이 아니라 관측 signal로만 사용
- 각 section에 confidence + evidence + ambiguity_flags를 반드시 포함
- diagnostic 결과는 generation/assemble policy에 바로 연결하지 않고, 13.7 판단 근거로만 사용

#### debug 기록

- `_debug_payload["multi_section_diagnostic"]` — 매 실행 기록
- multi-section 양식에서만 의미 있음 (single-section은 skip)

#### blocker 판단 기준

| 신호 | 판단 |
|------|------|
| non-section0이 전부 attachment/reference (짧거나 표/붙임 위주) | **not a blocker** |
| non-section0에 본문급 텍스트가 있고 생성 대상으로 보임 | **blocker → 13.7** |
| 판단 불가 | **ambiguity → 추가 양식 수집 후 재판단** |

### 검증

- 민원인: 5개 section diagnostic 실행, section1/2/4가 attachment 판정되는지 확인
- 조달청: single-section → diagnostic skip
- CC7: section 수 확인

### 하지 않을 것

- 1a pipeline 변경
- structure merge 로직
- section-aware target_unit_plan
- section-aware assembly 변경
- 13.5 `analyzed_sections` 제거

### blocker 확인 시 13.7 작업

- `extract_section_xml()` → 모든 section 반환
- section별 1a 분석 (토큰 비용 고려 — 축소/요약 전략 필요)
- document-level structure merge
- section-aware target_unit_plan
- section-aware assembly (생성 content를 올바른 section에 배치)

---

## C: Source Allocation — diagnostic

### 문제

13.4b broad source fallback: 각 chapter에 전체 source 전달. chapter 8개 × 전체 source = 토큰 8배.

### diagnostic 목적

1. **broad source 토큰 비용 측정**: 실제 LLM 호출당 input token 수
2. **split 불안정도 측정**: 같은 source + 같은 template에서 `split_source_by_chapters`가 몇 가지 다른 결과를 내는지
3. **per-chapter source relevance 측정**: 각 chapter가 broad source 중 실제로 사용하는 비율

### diagnostic 구현

chapter template-driven loop의 debug에 추가:
```python
"source_diagnostic": {
    "broad_source_length": len(_broad_source),
    "per_chapter_input_tokens_estimate": len(_broad_source) / 4,  # rough
    "total_estimated_tokens": len(_broad_source) / 4 * len(_seed_chapters),
    "split_available": bool(source_sections),
    "split_section_lengths": [len(s) for s in source_sections] if source_sections else [],
}
```

별도 함수 없이 debug field 추가로 충분.

### 판단 기준

| 신호 | 판단 |
|------|------|
| 총 토큰 < 100K | 비효율이지만 tolerable → watch |
| 총 토큰 100K~500K | 개선 가치 있음 → 13.7 |
| 총 토큰 > 500K 또는 LLM 호출 실패 | **blocker → 13.7 우선** |

**주의**: token threshold는 rough diagnostic signal이다.
- hard gate가 아니라 13.7 우선순위 판단 참고 지표
- 실제 LLM 호출 실패, 중복 생성, 누락, 품질 저하와 함께 종합 판단
- 수치만으로 hard decision하지 않음

### 하지 않을 것

- source_blocks → chapter 매핑 구현
- allocation policy 변경
- split_source_by_chapters 수정
- 2a prompt 변경

### 13.7 작업 (diagnostic 결과에 따라)

- source_blocks → per-chapter allocation
- chapter-local pattern 기반 source relevance scoring
- broad source fallback 대체

---

## 구현 순서

| # | 작업 | 유형 | 예상 규모 | 검증 |
|---|------|------|----------|------|
| 1 | `extract_per_chapter_pattern()` 함수 작성 | B 구현 | +80~100 lines | unit test on 3 cached templates |
| 2 | `extract_chapter_template_plan_seed()` 확장 | B 구현 | +20 lines | seed에 local_pattern/catalog 포함 확인 |
| 3 | DB tool per-chapter pattern/catalog 전달 | B 구현 | +15 lines | 조달청 e2e |
| 4 | 조달청 검증: 3 chapter 각각 다른 pattern | B 검증 | - | role_count/depth 차이 확인 |
| 5 | 민원인 검증: per-chapter 동작 + attachment 보존 유지 | B 검증 | - | regression 없음 |
| 6 | CC7 검증: shallow 불변 | B 검증 | - | shallow route 유지 |
| 7 | `diagnose_multi_section()` 함수 작성 | A diagnostic | +50 lines | 민원인 diagnostic |
| 8 | source diagnostic debug field 추가 | C diagnostic | +10 lines | 조달청 debug 확인 |
| 9 | diagnostic 결과 분석 → 13.7 scope 확정 | gate | - | ROADMAP 업데이트 |
| 10 | 커밋 + push | - | - | - |

### 13.6 완료 조건

1. B 구현/검증 완료 (조달청 per-chapter pattern 차이 확인, 3개 양식 regression 없음)
2. A diagnostic 결과 기록 (`multi_section_diagnostic` debug, blocker 여부 판정)
3. C diagnostic 결과 기록 (`source_diagnostic` debug, 토큰 비용 측정)
4. 13.7 후보 작업의 우선순위 업데이트 (A/C blocker 판정 결과 반영)
5. ROADMAP.md 업데이트 (13.6 완료, 13.7 scope 확정)

---

## 원칙 준수 확인

| 원칙 | 준수 여부 |
|------|----------|
| 하드코딩 금지 | O — level/role_cluster 번호 기준 없음. target_unit_plan region + parent_idx 기반 |
| 책임 분리 | O — subtree 추출(analyzer) / generation(2b) / assembly 분리 유지 |
| debug/검증 가능성 | O — pattern_source, local_role_count, extraction_confidence 기록 |
| 최종 구조 | O — per-chapter subtree는 dominant type보다 최종 구조에 가까움. chapter_types는 fallback |
| AI 자유도 제한 | O — 2b는 per-chapter pattern/catalog schema 안에서 생성 |
| 불확실 → 측정 | O — A/C는 diagnostic 먼저, blocker 확인 후 구현 |
| 임시 땜질 금지 | O — B는 confirmed gap 해결. A/C는 측정 기반 판단 |

---

## 하지 않을 것 (전체)

- table cell filling (14-table)
- KB 연동 (14)
- source coverage validation (15)
- 2a prompt 대규모 변경
- production HWP 본문에 메모 삽입
- 문서별/기관별 하드코딩
- section 번호 하드코딩
- role_cluster 번호 하드코딩
- 1a pipeline 변경 (A diagnostic만)
- source allocation policy 변경 (C diagnostic만)

---

작성: 2026-05-12
