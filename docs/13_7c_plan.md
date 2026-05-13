# 13.7c Plan — Source-to-Template Adaptation Planning

작성: 2026-05-13

---

## 1. 성격

13.7c는 13.7a 완료 + 13.7b 진입 전 사이에 작게 끼는 stage. **AI hallucinate와 title-body mismatch를 막는 planning gate**.

| stage | 책임 | 상태 |
|-------|------|------|
| 13.7a | Chapter-grouped assembly (chapter boundary 보존) | done |
| **13.7c** | **Source-to-template adaptation planning (title/action 결정)** | **이 단계** |
| 13.7b | Multi-section analysis (모든 section 분석 + document-level merge) | 13.7c 후 |

13.7c는 **generation 품질 보장이 아니라 planning gate**. AI가 chapter별로 무엇을 보존하고 무엇을 adaptation했는지 evidence와 함께 명시하도록 강제한다.

**Template-first 흐름 (핵심)**:
- step 1: template chapter need 파악 (chapter intent가 frame)
- step 2: source-to-chapter evidence retrieval (source는 chapter need에 종속된 도구)
- source가 풍부해도 chapter need 외 내용은 사용 X
- source 주제가 문서 방향을 결정하지 않음 — chapter intent가 결정
- 14단계 KB/RAG가 광범위한 source를 줄 수 있으므로 13.7c가 chapter need 기준 필터 역할

### 1.1 진단된 문제

13.7a 완료 후 민원인+조달청 PDF (mismatch source) 실행 결과:

| chapter | title | LLM 결과 | 문제 |
|---------|-------|---------|------|
| Ch0 Ⅰ.목적 | 원문 그대로 유지 | "현 시스템의 보완유지" (정책품질관리 body) | **title-body mismatch + hallucinate** |
| Ch1 Ⅱ.추진배경 | 원문 그대로 유지 | "정책품질관리제도가 전 부처..." | 동일 |
| Ch2 Ⅲ.민원응대 기본방향 | 원문 그대로 유지 | "문제정책 관리시스템은..." | 동일 (민원 키워드 무시) |
| Ch3~Ch7 | 원문 그대로 유지 | 빈 응답 (llm_len 12~17) | preserve 자동 동작 |

**진짜 문제** (사용자 정정):
- 양식과 source 주제 mismatch 자체는 fail이 아니라 adaptation 대상 (원칙 3).
- 더 큰 문제는 title은 원문 보존 + body만 source 주제 → 의미 mismatch.
- AI가 hallucination을 자체적으로 막지 못함. evidence 강제 부재.

### 1.2 원칙 근거

- **원칙 3 (Template-first, source-filling)**: "template 제목 문자열을 무조건 보존하는 것이 아니라, template의 intent flow를 보존합니다. 제목은 새 source 주제에 맞게 adaptation할 수 있지만, chapter의 역할과 흐름은 유지해야 합니다."
- **원칙 21 (AI는 schema-constrained)**: "원문 evidence, 고정 output schema, confidence, ambiguity_flags, supporting_evidence, counter_evidence, evidence_samples를 요구합니다."
- **원칙 22 (AI/code 역할 분리)**: "코드는 fact extraction, sanity check, schema validation, hallucination detection, fallback 처리". 의미 분류 X.

---

## 2. 설계 원칙 (최종 10개)

1. **Template-first 흐름**. step 1은 template chapter need(frame), step 2는 source-to-chapter evidence retrieval(도구). source가 문서 방향 결정 금지. source 주제가 chapter intent를 덮어쓰면 안 됨.
2. **의미 판단은 AI**. code는 의미 판단 안 함.
3. **코드는 JSON/schema, 필수 필드, 명백한 계약 위반만 본다.**
4. **heuristic은 hard fail 금지.** token overlap, length ratio, substring match는 정책에 영향 X.
5. **evidence 관련 수치는 참고 metric으로만**. `_debug.reference_metrics`에 보관. warning 아님.
6. **preserve 강등은 명백한 경우만**: AI 호출 실패, parse 실패, schema 위반, 필수 evidence 없음, action 모순.
7. **title adaptation 허용**, 단 AI가 preserved/adapted/counter evidence를 명시해야 한다. adaptation은 chapter intent + source 구체화 형태이며, source 주제로 chapter intent를 대체 X.
8. **13.7c는 generation 품질 보장이 아니라 planning gate**. 의미 적합성의 final gate는 사용자 눈검증.
9. **broad source 유지**. source slice는 후속 stage 후보.
10. **13.7b 이후 chapter/section 확장 시 adaptation mapping은 부분 재실행/확장**. schema는 유지하되 target set이 늘어남.

---

## 3. Schema

### 3.1 Chapter object `_debug.adaptation_decision`

13.7a chapter object schema에 `_debug.adaptation_decision` field 추가. 13.7a schema 자체는 변경 없음.

```python
{
  "chapter_idx": int,
  "source_chapter_idx": int,
  "original_title": str,
  "action": "generate" | "adapted_title_generate" | "preserve",
  "adapted_title": str | None,        # adapted_title_generate일 때만, 그 외 null

  # AI evidence (free-form, enum 강제 X)
  "preserved_aspects": [
    {"aspect": str, "template_evidence": str}
    # aspect: AI가 자유 텍스트로 어느 측면이 보존됐는지 (예: "structural role: 목적 절",
    #         "ordinal: Ⅰ", "sequence position: 도입부", 양식별로 다른 표현 가능)
    # template_evidence: template description / local_pattern / original_title에서 인용
    #                    (template 측 근거. source 측 근거는 supporting_evidence/adapted_aspects의 source_evidence)
  ],
  "adapted_aspects": [
    {"original": str, "adapted": str, "reason": str, "source_evidence": str}
    # original: original_title의 일부 또는 template 측 표현
    # adapted: adapted_title의 일부 또는 source 측 표현
    # source_evidence: source 인용 또는 mapping 근거
  ],
  "supporting_evidence": [str],       # source에서 인용 (substring 또는 paraphrase 허용)
  "counter_evidence": [str],          # adaptation 위험 신호 (AI self-reflection)
  "ambiguity_flags": [str],

  "adaptation_degree": "none" | "small" | "medium" | "large",
  # AI self-evaluation. debug-only. 자동 강등/hard fail/confidence 조정에 사용 X.

  "confidence": "high" | "medium" | "low",

  "preserve_reason": "source_gap" | "low_confidence" | "validation_failed"
                  | "adaptation_risk" | "plan_unavailable" | "other" | None,
  # action="preserve"일 때 필수, 그 외 null
  "preserve_reason_detail": str | None,
  # action="preserve"일 때 필수 (free text, 양식별 특수 case 대응)

  "_debug": {
    "reference_metrics": {
      # 참고 metric only — 다음 모든 사용 금지:
      #   · 정책 판단
      #   · preserve 강등
      #   · validation fail
      #   · hallucination 확정
      #   · confidence 조정
      # 좋은 paraphrase에서도 낮을 수 있으므로 낮다고 실패/환각 판단하지 않음.
      # 용도: broad source 한계 관찰, evidence trace 축적.
      # 측정값이 없으면 None (필수 X).
      "supporting_evidence_substring_match_ratio": float | None,
      # supporting_evidence가 source 원문에 substring match된 비율
      "generated_body_evidence_overlap_ratio": float | None,
      # generated body가 evidence hint의 source fragment를 포함한 비율 (broad source 한계 관찰용)
    },
    "validation_failures": [str],     # schema validation 실패 항목 (있으면 preserve 강등)
    "ai_retry_count": int,            # AI 호출 retry 횟수 (0~1)
  }
}
```

### 3.2 `_debug_payload["adaptation_plan"]` (전체 summary)

```python
{
  "source_topic": {
    "summary": str,                   # AI 1회 호출 결과
    "key_themes": [str],
    "main_headings": [str],
    "confidence": "high" | "medium" | "low",
    "evidence_samples": [str],
  },
  "chapter_count": int,
  "action_distribution": {
    "generate": int,
    "adapted_title_generate": int,
    "preserve": int,
  },
  "preserve_reason_distribution": {
    "source_gap": int,
    "low_confidence": int,
    "validation_failed": int,
    "adaptation_risk": int,
    "plan_unavailable": int,
    "other": int,
  },
  "validation_failure_count": int,
  "average_confidence": "high" | "medium" | "low" | None,
  "ai_calls": {
    "source_topic": {"raw_response_len": int, "retry_count": int},
    "chapter_mapping_batch": {"raw_response_len": int, "retry_count": int},
  },
  "batch_strategy": "single" | "split",  # split 시 split_count도 기록
  "batch_split_reason": str | None,
}
```

---

## 4. AI 호출

### 4.1 Source inventory extraction (1회) — template-first의 도구

**Input**:
- broad source 전문 (현재 `_broad_source`)

**Output schema** (JSON):
```python
{
  "summary": str,                 # source가 다루는 영역의 brief description (1~2 문장)
                                  # — chapter intent를 결정짓지 않음
  "available_topics": [str],      # source가 다루는 영역/키워드 (선택지로, 결정적 진술 X)
  "main_headings": [str],         # source의 주요 heading/section title 목록
  "confidence": "high" | "medium" | "low",
  "evidence_samples": [str],      # source에서 짧은 인용 (검색 도구로 사용됨)
}
```

**Template-first 원칙**:
- 이 단계는 source inventory를 정리하는 것일 뿐 source 주제를 frame으로 만들지 않음.
- AI에게 "이 source의 주제는 X다"라는 결정적 진술을 피하도록 prompt 지시.
- inventory는 다음 step 2 (chapter mapping)에서 도구로만 사용.
- 양식 도메인 가정 없음 (원칙 14).

**함수**: `build_source_inventory_prompt`, `parse_source_inventory_from_llm`

### 4.2 Chapter mapping batch (1회 또는 split) — template-first

**Template-first prompt 구조**:
- Step 1: chapter need 파악 (frame, 우선)
- Step 2: source evidence retrieval (도구, chapter need에 종속)
- Step 3: 각 chapter 결정

**Input (순서 중요)**:
- **chapter list (frame, 첫째)**: `{idx, original_title, description, local_pattern_summary, local_catalog_summary}` — chapter_template_plan_seed에서 derive. 이게 frame.
- **source_inventory (도구, 둘째)**: 4.1 결과. chapter need 매칭 도구로만 사용.
- **broad_source_preview (참조용)**: source의 일부, evidence retrieval 용.

prompt에 명시 지시:
- "chapter need에 매칭되는 source evidence만 사용"
- "source의 다른 풍부한 내용은 chapter need와 안 맞으면 무시"
- "title adaptation은 chapter intent를 보존하면서 source 구체화 (예: 'Ⅰ.목적' + 'X 정책' → 'X 정책 목적'). source 주제가 chapter intent를 대체하면 안 됨."

**Output schema** (JSON):
```python
{
  "chapter_decisions": [
    # 각 chapter에 대해 adaptation_decision의 AI-출력 부분 (3.1 schema의 AI 영역)
  ]
}
```

각 항목은 3.1 schema의 `action`, `adapted_title`, `preserved_aspects`, `adapted_aspects`, `supporting_evidence`, `counter_evidence`, `ambiguity_flags`, `adaptation_degree`, `confidence`, `preserve_reason`, `preserve_reason_detail` 채움.

**함수**: `build_adaptation_plan_prompt`, `parse_adaptation_plan_from_llm`

### 4.3 Batch split 정책

**기준**: 고정 chapter 수 아닌 **입력 크기 기준**.

- prompt char 길이 추정 (= 입력 token rough proxy)
- model context limit의 보수적 비율 (예: 60%) 넘으면 split
- 운영 마진이지 의미 판단 아님

Split 시:
- chapter list를 chunk로 나눠 batch 여러 번
- 각 batch에 source_topic + 해당 chunk만 포함
- chapter 간 consistency는 source_topic 공유로 부분 유지

debug:
- `batch_strategy: "split"`, `split_count: N`, `batch_split_reason: "input_char_threshold_exceeded"` 기록

---

## 5. Validation (hard fail 정책)

### 5.1 Hard fail 항목 (명백한 case만)

action별로 적용 조건이 다름. 표의 "적용 action" 열 참조.

| 항목 | 적용 action | 처리 |
|------|------------|------|
| JSON parse 실패 | all | retry 1회, 실패 시 전체 chapter `action="preserve"`, `preserve_reason="plan_unavailable"` |
| 필수 공통 필드 누락 (`action`, `chapter_idx`, `confidence`, `counter_evidence`, `ambiguity_flags`) | all | retry 1회, 실패 시 해당 chapter `preserve_reason="validation_failed"` |
| `action` 값 enum 외 | all | 해당 chapter `preserve_reason="validation_failed"` |
| `adapted_title_generate`인데 `adapted_title=null` 또는 빈 string | adapted_title_generate | 해당 chapter `preserve_reason="validation_failed"` |
| `generate`인데 `adapted_title`이 `original_title`과 다름 | generate | 해당 chapter `preserve_reason="validation_failed"` |
| `preserve`인데 `preserve_reason=null` 또는 `preserve_reason_detail=null` | preserve | 해당 chapter `preserve_reason_detail="missing_reason"` 자동 채움 + validation_failures에 기록 (preserve 자체는 정당, 강등 X) |
| `supporting_evidence`가 비어있는데 `confidence="high"` | **generate, adapted_title_generate만** | 해당 chapter `preserve_reason="validation_failed"` |
| `preserved_aspects` 또는 `adapted_aspects`가 비어있음 | **adapted_title_generate만** | 해당 chapter `preserve_reason="validation_failed"` |
| `counter_evidence` 필드 자체가 누락 (key 없음) | all | retry 1회, 실패 시 `preserve_reason="validation_failed"` |
| AI 호출 fail (network, rate limit) | all | 전체 chapter `preserve_reason="plan_unavailable"` |
| chapter object 계약 위반 (`action="preserve"`인데 body_items 있음 등) | all | 해당 chapter `preserve_reason="validation_failed"` |

**preserve action 특별 사항**:
- `supporting_evidence`가 비어있어도 OK ("source 근거 부족해서 preserve"는 high confidence일 수 있음)
- `adapted_title`, `adapted_aspects`, `preserved_aspects`도 비어도 OK (생성 안 함이라 자연)
- 단 `preserve_reason`, `preserve_reason_detail`, `counter_evidence` (또는 `ambiguity_flags`)은 비어있으면 안 됨
- 즉 preserve의 검증 초점은 **이유 명시**이지 source evidence 강제 X

### 5.2 Hard fail 아닌 것 (heuristic, debug-only)

다음은 **validation에 영향 없음**. `_debug.reference_metrics`에 측정값만:

- `supporting_evidence_substring_match_ratio` 낮음 (paraphrase 정상)
- `generated_body_evidence_overlap_ratio` 낮음 (broad source 한계)
- `adapted_title`이 `original_title`과 token overlap 낮음 (source domain 변화 정상)
- `adapted_title` 길이가 짧거나 김 (좋은 제목/나쁜 제목 판단 불가)
- `preserved_aspects`가 추상적 (의미 판단)
- `adaptation_degree="large"` + `confidence="high"` (AI 판단 존중)

### 5.3 Retry 정책

- AI 호출 1회 + retry 1회 (parse 실패 / schema 위반 시).
- retry도 실패하면 fallback (전체 또는 chapter별 preserve 강등).
- `_debug.ai_retry_count`에 기록.

---

## 6. 2b prompt 변경

### 6.1 변경 내용

chapter loop에서 2b LLM 호출 시:
- `action="generate"`: 기존대로 `original_title` + broad source. 변경 없음.
- `action="adapted_title_generate"`: `adapted_title` 사용. 추가로 prompt에 명시:
  - `adapted_title` (변경된 chapter title)
  - `original_title` (참고용)
  - `preserved_aspects` (template evidence 인용)
  - `adapted_aspects` (mapping 근거)
  - `supporting_evidence` (source 인용 hint)
- `action="preserve"`: 2b 미호출. region 전체 preserve (13.7a 안전장치).

### 6.2 broad source 처리

- broad source는 2b prompt에 그대로 포함 (변경 X).
- `supporting_evidence`는 hint이지 source slice 아님.
- LLM이 evidence hint 따르도록 prompt에 명시:
  - "다음 evidence는 chapter에 매칭된 source 인용입니다. 이를 우선 참고하여 body를 생성하세요."

### 6.3 한계 (debug)

- 2b LLM이 evidence hint를 따르는지 보장 안 됨 (broad source 한계).
- `_debug.reference_metrics.generated_body_evidence_overlap_ratio`로 측정 (debug-only).
- 낮으면 broad source의 한계 evidence. 후속 stage 우선순위 자료.

---

## 7. 13.7c 한계 (명시)

| 한계 | 후속 대응 |
|------|----------|
| AI 의미 판단 의존, hallucination 100% 차단 불가 | 사용자 눈검증이 의미적 정확성의 final gate. 발견 시 stage 조정. |
| broad source 그대로 (chapter별 source slice 없음) | source slice는 별도 stage 후보 (13.7c 이후 또는 14 이후). |
| Coverage validation 없음 (generated가 source 얼마나 반영했는지) | 15 Source Evidence/Coverage에서 다룸. |
| nested chapter / multi-section 확장 미적용 | 13.7b 이후 부분 재실행 (chapter 수 확장만). |
| 정량적 hard rule 없음 → AI over-confidence 위험 | counter_evidence + ambiguity_flags 강제 + 사용자 눈검증으로 통제. |
| 양식 evidence 1~3개로 13.7c 효과 측정 | 추가 양식 누적 후 정책 조정. |

→ **13.7c는 planning gate**. generation 품질 final 책임은 13.7c 외부.

---

## 8. 검증 조건

### 8.1 기능적 (구현)

- adaptation_plan AI 호출 2회 (source topic + chapter mapping batch) 작동
- batch split 동적 기준 작동 (필요 시)
- chapter object `_debug.adaptation_decision`에 schema 채워짐
- `_debug_payload["adaptation_plan"]` summary 채워짐
- 2b prompt에 `adapted_title` + evidence hint 전달 (adapted_title_generate일 때)
- preserve 강등 작동 (hard fail 항목별)

### 8.2 구조적 (regression, 양식 3개)

- 양식 3개 invariant_violations=0 (13.7a 검증 유지)
- assembly fail=0
- shallow route (CC7) 영향 0 — 13.7c는 chapter route만 적용, shallow는 미진입
- 13.7a chapter object schema 호환 (section_id 등)

### 8.3 의미적 (사용자 눈검증 + claude debug 분석)

| 양식 + source | 기대 결과 | 측정 |
|--------------|----------|------|
| 민원인 + 조달청 PDF (mismatch) | Ch0~Ch2 hallucinate 감소 OR preserve 강등 | (a) production 출력 검사 (사용자 눈검증) + (b) action 분포 (`adapted_title_generate` 또는 `preserve` 비율 증가) + (c) preserve_reason 분포 |
| 민원인 + 민원인 PDF (정답, 사용자 보유 시) | 대부분 `generate` (adaptation 없음) | action 분포에서 `generate` ≈ 100% |
| 조달청 + 조달청 PDF (정상) | 3 chapter 전부 `generate`, adaptation 0 | `adapted_title_generate=0`, `preserve=0` |
| CC7 + 조달청 PDF (shallow) | 13.7c 미진입, shallow path 그대로 | adaptation_plan=None |

### 8.4 Debug field 검증 (action별 차별화)

**모든 action 공통 (필수)**:
- `action`, `confidence`, `counter_evidence`, `ambiguity_flags`, `adaptation_degree` 채워짐
- (`counter_evidence`는 비어있을 수 있지만 필드 자체는 존재)

**`generate` 추가 요구**:
- `supporting_evidence` 비어있지 않음 (또는 confidence ≠ "high")
- `adapted_title` = `original_title` (계약)

**`adapted_title_generate` 추가 요구**:
- `adapted_title` 비어있지 않음
- `preserved_aspects` 비어있지 않음
- `adapted_aspects` 비어있지 않음
- `supporting_evidence` 비어있지 않음 (또는 confidence ≠ "high")

**`preserve` 추가 요구**:
- `preserve_reason` 채워짐 (enum 6개 중 하나)
- `preserve_reason_detail` 채워짐 (free text)
- `counter_evidence` 또는 `ambiguity_flags` 중 하나는 비어있지 않음
- `supporting_evidence`, `adapted_title`, `adapted_aspects`, `preserved_aspects`는 비어도 됨

**`_debug_payload["adaptation_plan"]` 요구**:
- `source_topic`, `action_distribution`, `preserve_reason_distribution`, `validation_failure_count` 채워짐

**Reference metrics (debug-only, 검증 조건 아님)**:
- 측정값이 있으면 기록, 없으면 None (필수 X)
- 정책 판단에 사용 X — 낮다고 실패/환각 판단 X
- broad source 한계 관찰용
- 완료 조건의 일부가 아니므로 비어있어도 13.7c 완료 가능

---

## 9. 13.7b 이후 부분 재실행 정책

13.7c는 **top-level section0 chapter 기준** planning gate.

13.7b 후 변동:
- section1~4 분석되어 chapter 수 추가 (예: section4 "제2장" 본문성)
- nested chapter 도입 가능성

부분 재실행:
- 기존 chapter 1~N의 adaptation_decision 유지
- 새 chapter (N+1~M)만 신규 mapping
- chapter_template_plan_seed의 chapter list 확장에 맞춰 13.7c re-run

Schema 유지:
- chapter object `_debug.adaptation_decision` schema 그대로
- chapter_idx 기준 mapping (region_id 안정 가정)

확장 시점에 별도 결정 — 13.7b 완료 후 ROADMAP 업데이트.

---

## 10. 구현 순서

| # | 작업 | 위치 | 규모 |
|---|------|------|------|
| 1 | `extract_source_topic()` AI 호출 + schema | hwpx_analyzer.py | ~80 lines |
| 2 | `compute_adaptation_plan()` AI 호출 batch + schema | hwpx_analyzer.py | ~150 lines |
| 3 | `validate_adaptation_decision()` (5.1 hard fail 항목) | hwpx_analyzer.py | ~100 lines |
| 4 | `compute_reference_metrics()` (5.2 debug-only) | hwpx_analyzer.py | ~50 lines |
| 5 | Batch split 동적 기준 (4.3) | hwpx_analyzer.py | ~30 lines |
| 6 | DB tool: adaptation_plan 호출 + per-chapter decision 적용 | DB tool | ~80 lines |
| 7 | DB tool: 2b prompt에 adapted_title + evidence hint 전달 | DB tool | ~30 lines |
| 8 | `build_chapter_object` 확장 (adaptation_decision 필드 attach) | hwpx_analyzer.py | ~20 lines |
| 9 | 검증 (민원인/조달청/CC7) | - | - |
| 10 | ROADMAP/handoff/13.7 계획서 patch | - | - |

---

## 11. 하지 않을 것

| 항목 | 사유 |
|------|------|
| intent_role enum 강제 (purpose/background/...) | 원칙 2 (하드코딩) + 원칙 14 (over-fit) 위배 |
| source 주제로 chapter 방향 결정 | 원칙 3 (Template-first) 위배. source는 chapter need 매칭 도구이지 frame 아님 |
| source의 풍부한 내용을 chapter need와 무관하게 모든 chapter에 펼치기 | 원칙 3 위배. 14단계 KB/RAG가 광범위한 source 줄 수 있으므로 13.7c가 chapter need 기준 필터 역할 |
| token overlap / length / substring을 hard fail로 | 원칙 21 (AI 의미 책임) — paraphrase 정상 |
| adaptation_degree="large" → 자동 강등 | AI self-evaluation 존중 (사용자 정책) |
| chapter 수 고정 batch split 기준 | 운영 하드코딩 — 입력 크기 동적 기준 사용 |
| Coverage validation (generated가 source 얼마나 반영) | 15 Source Evidence/Coverage |
| Chapter-specific source slice | 후속 stage (broad source 유지) |
| nested chapter / multi-section 확장 | 13.7b |
| heuristic을 warning으로 표기 | `_debug.reference_metrics` (참고 metric, warning 아님) |
| section role classification | 원칙 (13.7b에서도 금지) |
| chapter title 보존 강제 (template title 무조건 유지) | 원칙 3 (template intent flow 보존이지 문자열 보존 아님) |
| 13.7c에서 generation 품질 final 책임 | 13.7c는 planning gate, 의미 적합성 final gate는 사용자 눈검증 |

---

## 12. 원칙 준수 확인

| 원칙 | 13.7c |
|------|-------|
| 1. 임시 땜질 X, 최종 구조 | O — adaptation planning은 generation의 핵심 책임. evidence-driven은 양식 일반화 가능. |
| 2. 하드코딩 X | O — enum 강제 X (intent_role, role_cluster 번호, 양식 고유 문구 미사용) |
| 3. Template-first, source-filling | O — title adaptation은 원칙 3 직접 적용. evidence 없는 hallucination 차단. |
| 4. Chapter-local pattern preservation | O — local_pattern, local_catalog를 AI mapping input으로 사용. preserved_aspects로 보존 명시. |
| 5. Multi-section section-aware | 부분 — 13.7c는 top-level 기준, 13.7b에서 multi-section 확장 |
| 6. 책임 분리 | O — planning(13.7c) / allocation(별도 stage) / coverage(15) / assembly(13.7a) |
| 7. 로그/검증 가능성 | O — reference_metrics, validation_failures, retry_count 명시 |
| 8. blocker/watch/later | O — broad source / coverage / hallucination은 명시적 한계 |
| 9. AI 자유도 줄이기 | O — schema-constrained output, evidence 강제. enum은 폐기. |
| 10. AI 호출 수 < 품질 | O — 2회 추가 (source topic + chapter mapping), batch로 효율 |
| 11. 출력 schema 안정화 | O — adaptation_decision schema 명시, JSON-constrained |
| 13. validation hard gate 신중 | O — hard fail은 명백 case만, heuristic은 hard fail X |
| 14. 근거 있는 일반화 | O — enum 폐기, 양식 evidence에서 chapter intent derive |
| 16. 측정 후 구현 | O — 13.7a debug에서 hallucinate/title-body mismatch evidence 확보 후 진입 |
| 18. 측정은 결정 미루기 X | O — 13.7c 진입은 evidence 확인 후 |
| 19. 설계 검토와 구현 분리 | O — 본 설계서 작성 후 구현 |
| 20. 관측용 코드도 최종 구조 | O — reference_metrics는 debug-only지만 schema 일관 |
| 21. AI는 제한된 schema 적극 활용 | O — supporting/counter/ambiguity/preserved/adapted_aspects 명시 강제 |
| 22. AI/code 역할 분리 | O — AI 의미 판단, code 형식/계약 검증 |
| 23. allocation과 coverage 분리 | O — 13.7c는 planning (mapping decision), coverage는 15 |
| 25. production / debug 구분 | O — production 출력에 "source 부족" 메모 X, debug에만 |
| 26. Route별 검증 경로 존중 | O — chapter route만 적용, shallow route 미진입 |
| 27. 명시적 완료 조건 | O — §8 검증 조건 명시 |

---

## 13. 핵심 차이 — 13.7a/13.7b와의 분리

| stage | 다루는 것 | 다루지 않는 것 |
|-------|----------|---------------|
| 13.7a | Chapter boundary 보존 (assembly path), chapter object schema, region preserve | source-template adaptation, multi-section analysis |
| **13.7c** | **chapter별 source-template adaptation 결정 (action, adapted_title, evidence)** | source slice, multi-section, coverage validation |
| 13.7b | Multi-section analysis, document-level merge, (section, region, chapter) 확장 | adaptation (13.7c 결과 활용) |

3 stage는 책임 분리 (원칙 6).

---

작성: 2026-05-13
