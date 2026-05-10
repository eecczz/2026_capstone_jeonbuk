# 12.1 Marker Suffix / Separator Normalization — Design

## 현재 상태 (실측 데이터 기반)

### Separator 패턴 관측

| 양식 | role | policy_type | markers | separator | 실제 텍스트 | 분석 |
|------|------|-------------|---------|-----------|-----------|------|
| 조달청 | c4 | roman_sequence | Ⅰ,Ⅱ,Ⅲ | ` . ` | `Ⅰ . 추진성과 및 평가` | dot이 marker suffix인데 separator로 밀림 |
| 민원인 | c4 | roman_sequence | Ⅰ,Ⅱ,Ⅲ | ` . ` | `Ⅰ . 목 적` | 동일 문제 |
| 민원인 | c25 | arabic_sequence | 1,2,3 | `. ` | `1. "휴대용 영상기록..."` | dot이 separator에 포함됨 |
| 민원인 | c26 | korean_sequence | 가.,나.,다. | ` ` | `가. 영상과 음성의 녹화` | **dot이 marker에 이미 포함!** (올바름) |
| 민원인 | c7 | arabic_sequence | 1,2,3 | ` ` | `1 추진배경` | dot 없는 arabic (정상) |
| 조달청 | c15 | num_paren_sequence | 1),2) | ` ` | - | `)` 이미 marker에 포함 (올바름) |
| 전부 | - | fixed_char | □,○,※ | ` ` | `□ 과제` | 정상 |

### 핵심 발견

1. **korean_sequence**: `가.`, `나.` — dot이 **이미 marker에 포함**. 올바름.
2. **num_paren_sequence**: `1)`, `2)` — paren이 **이미 marker에 포함**. 올바름.
3. **roman_sequence**: `Ⅰ`, `Ⅱ` — dot이 marker에 **빠져있고** separator `" . "`에 포함됨. **문제.**
4. **arabic_sequence (민원인 c25)**: `1`, `2` — dot이 separator `". "`에 포함됨. **문제.**
5. **arabic_sequence (민원인 c7)**: `1`, `2` — dot 없음, separator `" "`. **정상.**

즉 문제는 **특정 role에서 dot/punctuation이 marker가 아닌 separator로 밀렸다**는 것.

### 원인

HWPX 텍스트 extraction에서 run boundary가 marker와 dot 사이에 있으면:
- Run 1: `Ⅰ` (marker character)
- Run 2: `.` (dot, 별도 run)
- Run 3: ` 추진성과...` (content)

이것을 concat하면 `Ⅰ. 추진성과`이지만, run 경계에 공백을 삽입하면 `Ⅰ . 추진성과`가 됨.
`marker_policy_1f`가 이 extraction 결과를 학습하므로 marker=`Ⅰ`, separator=` . `로 저장.

---

## 설계 원칙

### 사람이 보는 marker 단위 = code rendering 단위

| 패턴 | 자연스러운 해석 |
|------|----------------|
| `Ⅰ. 제목` | marker=`Ⅰ.`, separator=` ` |
| `1. 제목` | marker=`1.`, separator=` ` |
| `1) 제목` | marker=`1)`, separator=` ` |
| `가. 제목` | marker=`가.`, separator=` ` |
| `(1) 제목` | marker=`(1)`, separator=` ` |
| `□ 제목` | marker=`□`, separator=` ` |
| `1 제목` (dot 없음) | marker=`1`, separator=` ` |

**규칙: punctuation(`.`, `)`, `(`...`)`)이 번호/마커와 붙어서 하나의 display unit을 형성하면 marker suffix에 포함.**
**separator는 marker display unit과 content 사이의 whitespace만.**

---

## 방안 비교

### A안: marker_policy_1f 자체를 수정

- `save_template_cache` 전에 marker normalization 삽입
- `markers: ["Ⅰ", "Ⅱ"]` → `markers: ["Ⅰ.", "Ⅱ."]`, `separator: " "`
- CACHE_SCHEMA_VERSION bump 필요 (기존 cache 무효화)

**장점**: single source of truth가 policy 자체에 있음
**단점**: cache 전체 무효화, 1f AI 결과 재생성 필요, 기존 marker rewrite와 호환성 깨짐

### B안: marker_separator.py 내부에서 normalized form 생성

- marker_policy_1f는 그대로 유지
- `marker_separator.py`에서 policy를 읽을 때 normalization layer 적용
- `normalize_marker_policy(policy) → {marker_with_suffix, separator_normalized}`

**장점**: 기존 cache/rewrite에 영향 없음, 점진적 전환 가능
**단점**: 두 곳에 "진짜 marker"가 다르게 표현됨 (policy vs normalized)

### C안: marker_policy에 `suffix` 필드 추가

- 기존 `markers: ["Ⅰ"]`, `separator: " . "` 유지
- 새 필드: `suffix: "."`, `normalized_separator: " "`
- Phase 2 reattach에서: `marker + suffix + normalized_separator + content`

**장점**: 기존 호환 유지 + normalized form 명시
**단점**: suffix 계산 로직 필요, policy schema 확장

---

## 추천: B안 (normalization layer in marker_separator.py)

### 이유

1. **기존 cache/rewrite 영향 없음**: marker_policy_1f, marker rewrite, analyze_marker_in_text 모두 그대로 동작
2. **Phase 2에서만 사용**: reattach_marker가 호출될 때 normalized form 사용
3. **점진적 전환**: Phase 2 안정화 후, 원하면 policy 자체를 normalize로 migration 가능
4. **CACHE_SCHEMA_VERSION 미변경**: cache 무효화 불필요
5. **marker rewrite와 충돌 없음**: Phase 2에서 rewrite가 퇴역할 때까지 공존 가능

### Normalization Rules

```python
SUFFIX_PATTERNS = {
    # policy_type → suffix extraction rule
    "roman_sequence": ".",       # Ⅰ. Ⅱ. Ⅲ.
    "arabic_sequence": None,     # case-by-case (separator에 dot이 있으면 suffix)
    "korean_sequence": None,     # already in markers (가. 나. 다.)
    "num_paren_sequence": None,  # already in markers (1) 2))
    "circled_sequence": None,    # no suffix
    "circled_num_sequence": None,
    "circled_pua_sequence": None,
    "fixed_char": None,          # no suffix
    "star_depth": None,
}

def normalize_marker_for_reattach(policy: dict) -> dict:
    """
    policy의 marker + separator를 normalized form으로 변환.
    원본 policy는 변경하지 않음. 새 dict 반환.

    Returns:
        {
            "markers_normalized": ["Ⅰ.", "Ⅱ.", "Ⅲ."],  # suffix 포함
            "separator_normalized": " ",                   # whitespace only
            "suffix_detected": ".",
            "normalization_applied": True,
        }
    """
    policy_type = policy.get("policy_type", "")
    markers = policy.get("markers", [])
    separator = policy.get("separator", " ")

    # Case 1: separator에 punctuation이 포함된 경우 → suffix로 이동
    suffix = ""
    sep_normalized = separator

    if separator and not separator.isspace():
        # separator에서 leading/trailing whitespace와 punctuation 분리
        sep_stripped = separator.strip()
        if sep_stripped in (".", ")", ":", ";"):
            suffix = sep_stripped
            # separator에서 suffix 제거 → 남은 whitespace만
            sep_normalized = separator.replace(sep_stripped, "", 1)
            if not sep_normalized:
                sep_normalized = " "  # 최소 공백 보장
        elif sep_stripped.startswith(".") or sep_stripped.startswith(")"):
            suffix = sep_stripped[0]
            sep_normalized = separator.replace(sep_stripped, "", 1) or " "

    # Case 2: policy_type별 known suffix
    if not suffix and policy_type == "roman_sequence":
        # roman은 거의 항상 dot suffix
        if separator and "." in separator:
            suffix = "."
            sep_normalized = separator.replace(".", "", 1).strip() or " "

    # Markers에 suffix 부착
    if suffix:
        markers_normalized = [m + suffix for m in markers]
    else:
        markers_normalized = list(markers)

    # Sep normalized: whitespace only로 정규화
    if sep_normalized and not sep_normalized.isspace():
        sep_normalized = " "  # fallback

    return {
        "markers_normalized": markers_normalized,
        "separator_normalized": sep_normalized.rstrip() + " " if sep_normalized.strip() == "" else " ",
        "suffix_detected": suffix,
        "normalization_applied": bool(suffix),
        "original_markers": markers,
        "original_separator": separator,
    }
```

### 적용 위치

```python
# marker_separator.py에서 reattach 시:
def reattach_marker(content: str, marker: str, separator: str) -> str:
    """marker + separator + content. normalized marker/separator를 받음."""
    ...

# Phase 2 assemble에서:
normalized = normalize_marker_for_reattach(policy)
marker = normalized["markers_normalized"][sibling_index - 1]  # or generate
sep = normalized["separator_normalized"]
text = reattach_marker(content, marker, sep)
```

---

## 검증: 3개 양식 예상 결과

### 조달청 role_cluster_4 (roman_sequence)

| 필드 | 기존 | normalized |
|------|------|-----------|
| markers | `["Ⅰ", "Ⅱ", "Ⅲ"]` | `["Ⅰ.", "Ⅱ.", "Ⅲ."]` |
| separator | `" . "` | `" "` |
| reattach 결과 | `Ⅰ . 제목` | `Ⅰ. 제목` |

### 민원인 role_cluster_25 (arabic_sequence, sep=". ")

| 필드 | 기존 | normalized |
|------|------|-----------|
| markers | `["1", "2", "3"]` | `["1.", "2.", "3."]` |
| separator | `". "` | `" "` |
| reattach 결과 | `1. 제목` → `1. 제목` | 동일 (이 경우 기존도 렌더링 OK) |

### 민원인 role_cluster_26 (korean_sequence)

| 필드 | 기존 | normalized |
|------|------|-----------|
| markers | `["가.", "나.", "다."]` | `["가.", "나.", "다."]` (변경 없음) |
| separator | `" "` | `" "` |

이미 dot이 marker에 포함 → normalization 불필요. `suffix_detected = ""`, `normalization_applied = False`.

### 민원인 role_cluster_7 (arabic_sequence, sep=" ")

| 필드 | 기존 | normalized |
|------|------|-----------|
| markers | `["1", "2", "3"]` | `["1", "2", "3"]` (변경 없음) |
| separator | `" "` | `" "` |

Separator에 punctuation 없음 → normalization 불필요.

---

## Phase 1 roundtrip과의 호환

Phase 1의 roundtrip은 **기존 policy 기준**으로 실행됨 (normalized 아님).
Phase 2에서는 **normalized policy 기준**으로 reattach.

이 차이가 문제가 되는가?

**문제 없음.** 이유:
- Phase 1은 readiness 검증용 (이미 통과)
- Phase 2에서는 normalized를 사용하므로, Phase 1 결과와 직접 비교 불필요
- Phase 2 검증은 "normalized reattach 결과가 원본 HWPX 렌더링과 일치하는가"로 별도 수행

---

## 기존 marker rewrite와의 관계

| 시점 | marker rewrite | marker_separator normalization |
|------|----------------|-------------------------------|
| Phase 1 (현재) | 활성 (기존 동작) | roundtrip 관측만 |
| Phase 2 | 활성 (safety net) | reattach에서 normalized marker 사용 |
| Phase 3 | 퇴역 | single source of truth |

Phase 2에서 두 시스템이 공존:
- AI가 content만 출력 → `reattach_marker`가 normalized marker 부착
- marker rewrite는 "이미 부착된 marker와 expected가 일치하는지" 확인하는 safety net
- rewrite가 변경하는 경우 = reattach에 버그가 있다는 신호 → 로그로 추적

---

## Blocker / Watch / Later

### Blocker (없음)

현재 normalization은 Phase 2 구현의 일부. Phase 1은 이미 통과했으므로 blocker 아님.

### Watch

| 항목 | 설명 |
|------|------|
| HWPX run boundary 공백 삽입 | extraction layer에서 run 경계에 공백이 삽입되는 규칙 미확정. normalization은 결과만 보고 추론. |
| arabic_sequence dot 유무 구분 | 같은 policy_type에서 c7(dot 없음)과 c25(dot 있음)가 공존. separator 내용으로 구분해야 함. |
| generate_expected_marker에 suffix 부착 | sequence formula 생성 시 suffix도 함께 부착해야 함 (e.g., `str(n) + "."`) |

### Later

| 항목 | 시점 |
|------|------|
| policy 자체에 suffix/normalized marker 저장 (A안) | Phase 3 이후, cache migration 시 |
| extraction layer 공백 규칙 정리 | CC9 (Layout Fidelity) |

---

## 구현 범위

### Do (Phase 2 준비 as part of Phase 2)

1. `marker_separator.py`에 `normalize_marker_for_reattach(policy)` 함수 추가
2. `generate_expected_marker`를 normalized markers 기반으로 확장
3. `reattach_marker`가 normalized marker + normalized separator를 받도록 연결
4. Phase 2 검증 시 "normalized reattach 결과 vs 원본 렌더링" 비교

### Do Not

- marker_policy_1f 수정
- CACHE_SCHEMA_VERSION 변경
- marker rewrite 로직 변경
- 1f AI prompt 변경
- extraction layer 수정

---

## Summary

| 결정 | 내용 |
|------|------|
| 어디까지 marker인가 | punctuation이 번호와 붙어 display unit이면 marker suffix (Ⅰ., 1., 가., 1)) |
| separator는 | whitespace만 (공백, 탭) |
| 기존 policy 바꾸나 | 아니오. marker_separator.py 내부에서 normalization layer |
| Phase 2 reattach contract | `normalize_marker_for_reattach(policy) → {markers_normalized, separator_normalized}` |
| 기존 roundtrip 호환 | Phase 1은 기존 기준, Phase 2는 normalized 기준. 별도 검증. |
| 언제 구현 | Phase 2 구현의 일부로 함께 (별도 단계 아님) |
