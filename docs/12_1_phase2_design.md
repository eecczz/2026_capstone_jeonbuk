# 12.1 Phase 2: Content-Only Generation + Normalized Reattach — Design (v2)

## 목적

AI가 content만 출력하고, marker는 code가 normalized policy 기반으로 부착하는 구조로 전환.
marker rewrite는 safety net으로 유지하며, rewrite가 개입하면 reattach 결함 신호로 로그.

---

## 전환 구조

```
[Phase 1 - 현재]
AI → "□ 과제 추진 현황" (marker + content)
  → _rewrite_marker(text, sib_idx) → 교정 (필요시)
  → assemble

[Phase 2]
AI → "과제 추진 현황" (content only)
  → sib_idx = _next_sibling_index(idx, role)  [ONCE]
  → reattach_with_normalized(content, policy, sib_idx) → "□ 과제 추진 현황"
  → _rewrite_marker(text, sibling_index_override=sib_idx) → safety net
  → compare: rewrite changed? → log conflict if yes
  → assemble
```

---

## 1. Sibling Index Single Source of Truth

### 원칙

- sibling_index는 assemble 내부에서 **1회만 계산**
- `_next_sibling_index(idx, role)` — counter를 1회 증가시키고 값 반환
- reattach와 rewrite 모두 이 값을 사용
- rewrite는 `sibling_index_override`를 받으면 내부 counter를 증가시키지 않음

### 구현

```python
# assemble_hwpx_hybrid 내부:

def _next_sibling_index(body_item_idx: int, role: str) -> int:
    """sibling_index를 계산하고 counter를 1회 증가. single source of truth."""
    node = _node_lookup.get(body_item_idx)
    ch_idx = _chapter_idx_lookup.get(body_item_idx)
    if _tree_available and node is not None and ch_idx is not None:
        parent_id = node.get("parent_id")
        counter_key = (ch_idx, parent_id, role)
        _sibling_counter[counter_key] = _sibling_counter.get(counter_key, 0) + 1
        return _sibling_counter[counter_key]
    else:
        _fallback_counter[role] = _fallback_counter.get(role, 0) + 1
        return _fallback_counter[role]

def _rewrite_marker(body_item_idx: int, role: str, text: str,
                    sibling_index_override: int | None = None) -> str:
    """
    marker_policy에 따라 text의 leading marker를 교체.
    sibling_index_override가 주어지면 내부 counter 사용하지 않음.
    """
    ...
    if sibling_index_override is not None:
        sib_idx = sibling_index_override
    else:
        # 기존 counter 로직 (Phase 1 호환)
        ...
```

### Assemble Loop (Phase 2)

```python
content_only_mode = True  # Phase 2 flag

for idx, item in enumerate(body_items):
    role = item["role"]
    text = item["text"]  # Phase 2: content only

    # 1. Sibling index (ONCE)
    sib_idx = _next_sibling_index(idx, role)

    # 2. Reattach (Phase 2 only)
    reattach_result = None
    if content_only_mode:
        from open_webui.utils.marker_separator import (
            generate_expected_marker_normalized, reattach_marker
        )
        expected = generate_expected_marker_normalized(role, policy, sib_idx)
        text = reattach_marker(text, expected["marker"], expected["separator"])
        reattach_result = expected

    # 3. Rewrite safety net (same sib_idx)
    rewritten = _rewrite_marker(idx, role, text, sibling_index_override=sib_idx)

    # 4. Conflict detection
    if rewritten != text:
        _phase2_conflicts.append({
            "item_idx": idx,
            "role": role,
            "sibling_index": sib_idx,
            "reattached": text[:80],
            "after_rewrite": rewritten[:80],
            "cause": _classify_rewrite_conflict(text, rewritten, reattach_result, role, policy),
        })

    final_text = rewritten
    # ... insert into HWPX
```

---

## 2. Content-Only Prompt 변경

### SECTION_FILL_PROMPT 마커 규칙 교체

현재:
```
## 마커 규칙 (format_rules 참조)
- marker_style: fixed → markers_sample의 첫 마커를 매번 사용
- marker_style: enumerate → markers_sample의 순서를 유지...
text 구성: marker + separator + 본문 내용
```

Phase 2:
```
## 마커 규칙

**마커는 자동으로 부착됩니다. text에 마커를 넣지 마세요.**

- text에는 순수 본문 내용만 작성하세요.
- 마커(□, ○, Ⅰ., 1., 가., ➊ 등)를 text 앞에 붙이지 마세요.
- 들여쓰기(공백/탭)도 넣지 마세요.
- 소스의 원래 마커도 제거하세요.

text 구성: 본문 내용만
- 올바른 예: "과제 추진 현황"
- 잘못된 예: "□ 과제 추진 현황", "Ⅰ. 추진 현황", "  과제"

각 role의 markers_sample은 해당 role의 성격을 이해하기 위한 참고 정보입니다.
마커 자체는 후처리에서 자동 부착됩니다.
```

---

## 3. Normalized Reattach

### normalize_marker_for_reattach (단순 원칙)

```python
def normalize_marker_for_reattach(policy: dict) -> dict:
    """
    원칙:
    - punctuation(. ) : ;)이 separator에 있으면 → marker suffix로 이동
    - separator → whitespace만
    - 판단 애매하면 원본 유지
    """
    markers = policy.get("markers", [])
    separator = policy.get("separator", " ")

    if not separator or separator.isspace():
        return {
            "markers_normalized": list(markers),
            "separator_normalized": separator or " ",
            "suffix_detected": "",
            "normalization_applied": False,
        }

    sep_stripped = separator.strip()
    suffix = ""
    if sep_stripped in (".", ")", ":", ";"):
        suffix = sep_stripped
    elif sep_stripped.startswith("."):
        suffix = "."
    elif sep_stripped.startswith(")"):
        suffix = ")"

    if suffix:
        return {
            "markers_normalized": [m + suffix for m in markers],
            "separator_normalized": " ",
            "suffix_detected": suffix,
            "normalization_applied": True,
        }

    # 판단 애매 → 원본 유지
    return {
        "markers_normalized": list(markers),
        "separator_normalized": separator,
        "suffix_detected": "",
        "normalization_applied": False,
    }
```

### generate_expected_marker_normalized

```python
def generate_expected_marker_normalized(role: str, policy: dict, sibling_index: int) -> dict:
    """normalized marker + separator 생성."""
    norm = normalize_marker_for_reattach(policy)
    policy_type = policy.get("policy_type", "")
    style = policy.get("style", "")
    markers = norm["markers_normalized"]
    suffix = norm["suffix_detected"]

    if policy_type in ("no_marker", "star_depth"):
        return {"marker": "", "separator": " ", "success": True,
                "normalization_applied": False, "suffix": ""}

    if not markers:
        return {"marker": "", "separator": " ", "success": False,
                "normalization_applied": norm["normalization_applied"], "suffix": suffix}

    if style == "fixed":
        marker = markers[0]
    elif sibling_index <= len(markers):
        marker = markers[sibling_index - 1]
    else:
        base = _generate_sequence_marker(policy_type, sibling_index, policy.get("markers", []))
        marker = base + suffix if suffix else base

    return {
        "marker": marker,
        "separator": norm["separator_normalized"],
        "success": bool(marker),
        "normalization_applied": norm["normalization_applied"],
        "suffix": suffix,
    }
```

---

## 4. Rewrite Conflict Classification

```python
def _classify_rewrite_conflict(reattached: str, rewritten: str,
                                reattach_result: dict, role: str, policy: dict) -> str:
    """rewrite가 reattach 결과를 변경한 원인 분류."""
    if not reattach_result:
        return "ai_still_included_marker"

    # rewrite의 expected와 reattach의 marker가 다른 경우
    # (이론상 같은 sib_idx를 공유하므로 발생하면 안 됨)
    # normalization 차이일 가능성
    reattach_marker_used = reattach_result.get("marker", "")
    if reattach_marker_used and reattach_marker_used not in rewritten:
        return "normalization_policy_mismatch"

    # separator 차이
    reattach_sep = reattach_result.get("separator", " ")
    if reattached.replace(reattach_sep, " ") == rewritten.replace(" ", " "):
        return "separator_only_difference"

    # 기타
    return "unknown_rewrite_conflict"
```

### Conflict Cause Taxonomy

| cause | 의미 | severity |
|-------|------|----------|
| `normalization_policy_mismatch` | normalized marker와 rewrite expected가 다름 | medium — normalization rule 검토 |
| `separator_only_difference` | marker 동일, separator만 다름 | low — Phase 3에서 자연 해결 |
| `ai_still_included_marker` | AI가 content-only를 안 따르고 marker 포함 | medium — prompt 강화 또는 strip |
| `sibling_index_mismatch` | sib_idx 공유 실패 (발생하면 안 됨) | high — 구현 버그 |
| `unknown_rewrite_conflict` | 분류 불가 | medium — 개별 조사 |

---

## 5. Debug Output

### `14_marker_reattach_result.json` (Phase 2)

```json
{
  "schema_version": 2,
  "phase": "content_only_reattach",
  "debug_only": false,

  "summary": {
    "total_items": 54,
    "applicable_items": 34,
    "skipped_items": 20,
    "reattach_applied": 34,
    "rewrite_safety_net_applied": 0,
    "rewrite_conflict_count": 0,
    "normalization_applied_count": 3,
    "ai_marker_residual_count": 0
  },

  "normalization_log": [
    {
      "role": "role_cluster_4",
      "original_policy": {"markers": ["Ⅰ", "Ⅱ"], "separator": " . "},
      "normalized_policy": {"markers": ["Ⅰ.", "Ⅱ."], "separator": " "},
      "normalization_applied": true,
      "suffix_detected": "."
    }
  ],

  "rewrite_conflicts": [],

  "per_item_sample": [
    {
      "item_idx": 0,
      "role": "role_cluster_4",
      "sibling_index": 1,
      "sibling_index_source": "tree_counter",
      "ai_content": "추진성과 및 평가",
      "reattached": "Ⅰ. 추진성과 및 평가",
      "after_rewrite": "Ⅰ. 추진성과 및 평가",
      "rewrite_changed": false,
      "normalization_applied": true
    }
  ]
}
```

### Debug에 반드시 포함 (요구사항 반영)

| 필드 | 목적 |
|------|------|
| `sibling_index` (per item) | sibling_index가 실제로 무엇이었는지 |
| `sibling_index_source` | "tree_counter" or "fallback_counter" |
| `rewrite_changed` | rewrite가 개입했는지 |
| `rewrite_conflict.cause` | 개입 원인 분류 |
| `normalization_applied` (per role) | 어떤 role에 normalization이 적용됐는지 |
| `original_policy` + `normalized_policy` | Phase 1/2 기준 차이 추적 |

---

## 6. Phase 2 성공 기준

| 기준 | threshold | 의미 |
|------|-----------|------|
| `rewrite_conflict_count` | = 0 | reattach가 완벽히 동작 |
| `rewrite_conflict_count` | ≤ 2 | acceptable, 개별 수정 |
| `rewrite_conflict_count` | > 2 | blocker — reattach/normalization 결함 |
| `ai_marker_residual_count` | = 0 | AI가 content-only를 따름 |
| `ai_marker_residual_count` | > 0 | prompt 강화 또는 strip fallback 필요 |
| 최종 HWPX 품질 | 기존 대비 regression 없음 | 사람이 결과 문서 확인 |

---

## 7. AI Marker Residual 처리

Phase 2에서 AI가 여전히 marker를 포함해서 출력할 수 있음 (prompt를 안 따르는 경우).

### 감지

```python
# reattach 전에 content에 marker가 있는지 체크
strip_check = strip_marker(content, role, policy)
if strip_check["detected_marker"]:
    # AI가 marker를 포함함 → strip 후 reattach
    content = strip_check["content"]
    ai_marker_residual_count += 1
    log.warning(f"[PHASE2] AI marker residual: {role} '{strip_check['detected_marker']}'")
```

이렇게 하면 AI가 marker를 넣더라도 strip → reattach로 정상 처리됨. 단 이 경우는 debug에 기록.

---

## 8. 구현 범위 (한 묶음)

### Do

1. `marker_separator.py`에 추가:
   - `normalize_marker_for_reattach(policy)`
   - `generate_expected_marker_normalized(role, policy, sibling_index)`
   - (기존 `strip_marker`, `reattach_marker` 활용)

2. `hwp_generator.py` assemble 수정:
   - `_next_sibling_index()` 추출 (기존 counter 로직을 별도 helper로)
   - `_rewrite_marker`에 `sibling_index_override` 파라미터 추가
   - `content_only_mode` flag + reattach 호출 삽입
   - AI marker residual strip 처리
   - rewrite conflict 감지 + 로그

3. `hwpx_analyzer.py` SECTION_FILL_PROMPT 마커 규칙 교체

4. debug output: `14_marker_reattach_result.json` (schema v2)

5. DB tool: content_only_mode 활성화 (valve 또는 코드 상수)

### Do Not

- marker_policy_1f 수정
- CACHE_SCHEMA_VERSION 변경
- marker rewrite 제거 (safety net으로 유지)
- star_depth reattach (skip 유지)
- validation hard gate

---

## 9. 전환 순서

```
1. marker_separator.py에 normalize/generate_normalized 추가
2. hwp_generator.py에 _next_sibling_index 추출 + _rewrite_marker override 추가
3. hwp_generator.py에 content_only_mode + reattach + conflict detection 추가
4. SECTION_FILL_PROMPT 마커 규칙 교체
5. 서버 배포 + 워커 재시작
6. DB tool content_only_mode 활성화
7. 3개 양식 검증:
   - AI가 content-only를 따르는지 (ai_marker_residual_count)
   - reattach로 marker가 실제 붙는지 (reattach_applied count)
   - rewrite_conflict_count = 0
   - ai_marker_residual_count = 0 (또는 최소)
   - 최종 HWPX에서 marker 누락 없는지
   - Ⅰ., 1., 가., 1) 등 marker가 렌더링상 자연스러운지
   - 기존 대비 regression 없는지 (성공/실패 수, 문서 구조)
```

---

## 10. Rollback Plan

### Soft Rollback (긴급 완화)

- `content_only_mode = False` (assemble에서 reattach 비활성화)
- prompt는 content-only 상태 유지 (AI가 marker 안 넣음)
- rewrite safety net에 의존하여 marker 부착
- **완전한 Phase 1 복귀가 아님** — rewrite allowlist 밖 policy(roman_sequence 등)는 marker 미부착 가능
- 사용 시점: Phase 2 결과에서 reattach 결함만 있고, AI content-only는 정상일 때

### Full Rollback (완전 복귀)

- `content_only_mode = False`
- SECTION_FILL_PROMPT를 Phase 1 마커 규칙으로 원복 (AI가 marker 포함 출력)
- 완전한 기존 production flow 복귀
- 사용 시점: prompt 실패(AI가 content-only를 안 따름) 또는 심각한 regression

### 구현 시 flag 설계

```python
# hwp_generator.py assemble 또는 DB tool에서:
MARKER_CONTENT_MODE = "content_only"  # "content_only" | "marker_included"

# SECTION_FILL_PROMPT 구성 시:
if MARKER_CONTENT_MODE == "content_only":
    # Phase 2 마커 규칙 (마커 넣지 마세요)
else:
    # Phase 1 마커 규칙 (마커 포함 출력)
```

full rollback 시 `MARKER_CONTENT_MODE = "marker_included"`로 전환하면 prompt와 assemble 모두 Phase 1으로 복귀.

---

## 11. Blocker / Watch / Later

### Blocker

| 조건 | 의미 |
|------|------|
| rewrite_conflict_count > 5 | reattach/normalization 심각 결함 |
| 최종 HWPX에서 marker 누락 | reattach 미동작 |
| AI가 content-only를 전혀 안 따름 (>50% residual) | prompt 근본 실패 |

### Watch

| 조건 | 의미 |
|------|------|
| rewrite_conflict 1~2건 | 특정 role normalization 이슈 |
| separator_only_difference | Phase 3에서 rewrite 퇴역 시 자동 해결 |
| AI marker residual 1~3건 | prompt 미세 조정 필요 |
| normalization 애매 케이스 | 원본 유지하고 관측 |

### Later

| 항목 | 시점 |
|------|------|
| Phase 3 (rewrite retirement) | conflict_count=0 안정 확인 후 |
| policy 자체 normalized 저장 | Phase 3 이후 |
| star_depth reattach | 별도 검토 |
| separator 완전 정규화 | rewrite 퇴역 시 |
