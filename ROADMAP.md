# HWPX Generation Pipeline Roadmap

이 파일은 HWPX 파이프라인의 단계별 로드맵입니다.
각 단계에서 무엇을 했고, 무엇이 남았고, 다른 단계에서 기억해야 할 것이 무엇인지 기록합니다.

최종 수정: 2026-05-10

---

## 단계 요약

| 단계 | 이름 | 상태 | 비고 |
|------|------|------|------|
| 0 | 관측/디버그 기반 | done | 디버그 인프라 |
| 1 | Template Factual Tree | done | 1a~1f, 캐시 포함 |
| 2 | Role Clustering | done | 1e canonical clustering |
| 3 | Grammar Induction | done | extract_template_grammar |
| 4 | Chapter Type / 2a Planning | done | chapter_types, 2a prompt |
| 5 | Role Semantics Schema 정리 | done | text_type, per_type_semantics |
| 6 | 2b Generation 안정화 | done | 6.5 marker, 6.6 header slot |
| 7 | Validation Contract + Cache Gate | done | 11_validation_summary, 05b cache gate |
| 8 | Generation Tree / AST | done | parent_id, title root, 8-infra |
| 9 | Assemble Engine | done (1차 안정화) | section_info, secPr, tree split. watch: append 정책, chapter→section mapping |
| 10 | Source Allocation | done (10.0+10.1) | decision log, allocation summary |
| **11** | **Structural Intent / Role Semantics** | **next** | semantic_tag, polysemous 관측 |
| 12 | Generation Schema Redesign | not started | marker/content 분리, source_refs |
| 13 | Source Evidence / Coverage | not started | source coverage validation |
| 14 | Shallow Template / Slot-Filling | not started | 간단한 양식 fast path |
| 15 | Open Notebook Source Planning | not started | source block, table contract |
| 16 | Internal AI Transition | not started | 외부→내부 AI 전환 |

---

## 의존성

```
0~7: 기초 (완료)
  |
  8: Tree/AST ──────────────┐
  |                         |
  9: Assemble ──┐           |
  |             |           |
 10: Source ────┤    11: Role Semantics
  |             |           |
  |             └───────────┤
  |                         |
  |                    12: Schema Redesign
  |                         |
  |                    13: Source Evidence
  |                         |
  |                    14: Slot-Filling
  |                         |
  |                    15: Open Notebook
  |                         |
  |                    16: Internal AI
```

- 9, 10, 11은 8 이후 병렬 가능
- 12는 8 + 10(최소) + 11 결정 완료 후
- 13은 12 (source_refs) 완료 후
- 14는 12 이후
- 15는 13 이후
- 16은 최후

---

## Stage 0: 관측/디버그 기반 — done

### 완료
- `/tmp/hwpx_debug/` 분리 디버그 파일 체계 (01~11 + 99_summary)
- `/tmp/hwpx_debug_last.json` 하위 호환
- `write_stage_debug_files()` 통합 출력 함수

### 다른 단계에서 기억할 것
- 새 디버그 파일 추가 시 `write_stage_debug_files`에 추가하면 됨
- 파일 번호: 01~05c 분석, 06~07b 2a, 08~09b 2b+validation, 10 assemble, 11 contract, 12+ 미래용
- DB tool 변경 없이 서버 코드만으로 debug 확장 가능

---

## Stage 1: Template Factual Tree — done

### 완료
- 1a: 양식 문단 분석 (role, description, level, parent_idx, marker)
- 1b: 역할 후보 추출
- 1c: 레벨/부모 트리
- 1e: 정규 클러스터링 (+ repair)
- 1f: marker policy induction (AI 기반 marker 판별 + evidence 교차검증)
- 1a 경량화: marker 상세 지시 축소, paragraph_styles 분리
- 캐시: template hash 기반, `/tmp/hwpx_cache/`

### 다른 단계에서 기억할 것
- **1a description이 semantic role의 원천 데이터**. 11단계에서 활용.
- 1f marker_policy가 canonical (1a marker는 hint로 격하)
- 캐시 무효화 조건: 1a~1f 로직 변경, CACHE_SCHEMA_VERSION 변경
- paragraph_styles는 1a 이후 코드에서 삽입 (AI output에서 제거됨)

---

## Stage 2: Role Clustering — done

### 완료
- role_cluster 번호 부여 (style fingerprint 기반 그룹핑)
- c0~c16+ (양식마다 다름)

### 다른 단계에서 기억할 것
- role_cluster는 **format-level 그룹핑** (같은 marker/style → 같은 cluster)
- **semantic 의미와 1:1이 아님** — 특히 c8 (`*`/`**`)에 11종 semantic role 혼재
- role_cluster 번호로 분기하는 로직 금지
- 11단계에서 semantic_tag를 별도 필드로 관측

---

## Stage 3: Grammar Induction — done

### 완료
- `extract_template_grammar()`: observed parent→child 전이 → allowed_children/parents
- global grammar + per_type grammar
- singleton/repeatable/optional 계산
- observed_counts per parent instance

### 다른 단계에서 기억할 것
- grammar는 **format_role (role_cluster) 기반**
- grammar가 c6→c8, c7→c8 모두 허용하면 parent ambiguity 발생 (8단계에서 관측된 c8 disagreement)
- grammar 변경 → 캐시 무효화 필요
- 12단계에서 grammar를 semantic_role 기반으로 재정의할 가능성 있음

---

## Stage 4: Chapter Type / 2a Planning — done

### 완료
- `_build_chapter_types()`: pattern tree 구성
- `build_chapter_classify_prompt()`: 2a prompt
- chapter_type별 root_roles, pattern 정의

### 다른 단계에서 기억할 것
- 2a가 chapter title + type을 결정 → source split의 anchor
- 2a output stability가 source allocation에 직접 영향 (10단계 finding)
- 같은 양식에서 3 chapter vs 5 chapter 편차 있음 (AI randomness)

---

## Stage 5: Role Semantics Schema 정리 — done

### 완료
- `classify_role_text_types()`: heading/body/supporting/summary 4분류
  - keyword set: _heading_kw, _body_kw, _supporting_kw, _summary_kw
  - has_children + keyword 조합으로 판별
- `build_per_type_role_semantics()`: (type, role)별 description 집계
  - representative_description, parent_roles, inferred_text_type

### 다른 단계에서 기억할 것
- **이 4분류가 11단계 semantic_tag의 출발점**
- keyword set을 확장하면 _caution_kw 등 추가 가능
- per_type_role_semantics의 inferred_text_type가 2b prompt에 이미 전달됨
- 11단계: 여기에 semantic_tag 필드를 추가하는 것이 자연스러운 확장점

---

## Stage 6: 2b Generation 안정화 — done

### 6.5 marker rewrite — done
- `enable_marker_rewrite` 기본값 True
- REWRITE_ALLOWED_POLICIES = {arabic_sequence, circled_sequence, fixed_char}
- star_depth: 항상 skip
- sibling counter: (chapter_idx, parent_id, role) 기반
- 4-way 로그 체계

### 6.6 header slot semantic — done
- `extract_header_roles()`: header role + description 추출
- 2a prompt에 header role description 전달
- 보안등급 오배치 해결

### 다른 단계에서 기억할 것
- **marker rewrite는 format_role + sibling_index로 결정** — semantic_tag 무관
- star_depth(*/**)는 rewrite 대상 아님 → 11단계에서도 marker 변경 불필요
- 12단계: marker/content 분리 시 rewrite 로직이 content와 독립임을 확인한 상태

---

## Stage 7: Validation Contract + Cache Gate — done

### 완료
- `build_validation_summary()`: 13+1개 check → 11_validation_summary.json
  - blocker: A1~A7, C1 / warning: A4, A6, E1 / watch: B1, C2, C3 / later: B3
- `validate_structure_for_cache()`: SC1~SC5 blocker, SC6~SC9 watch
- CACHE_SCHEMA_VERSION = 3
- sequence marker fallback fix

### 다른 단계에서 기억할 것
- **gate_enabled=false** — 현재 모든 check가 dry-run
- E1 (heading text_type 장문) → 11단계에서 semantic_tag 기반 재분류 대상
- B3 (rewrite 후 marker 검증) → placeholder, 12단계 이후 구현 가능
- hard gate 전환은 관측 데이터 충분히 수집 후 (원칙 9)

---

## Stage 8: Generation Tree / AST — done

### 8.0a parent_id AST — done
- 2b output에 id/parent_id 필드 추가
- `normalize_section_items()`: title node id=0 injection, body id +1
- `validate_ai_parent_ids()`: grammar 기반 검증
- `apply_parent_id_fallback()`: invalid → reconstruct 결과로 복구
- `build_chapter_trees()`: assemble용 node list

### 8-infra — done
- `process_section_fill_result()`: parse~validation 전체 서버 함수
- DB tool은 LLM 호출 + 결과 수집만 담당

### 8.0b title node root — done
- explicit_title_root, title_node_in_tree=true
- assemble alignment: [title_bi] + body_indices
- agreement 100% (offset 보정 후)

### 다른 단계에서 기억할 것
- **c8 parent disagreement 19건**: AI는 c6(□), reconstruct는 c7(ㅇ)을 parent로 선택
  - 둘 다 grammar상 valid
  - 원인: c8이 최소 2가지 structural intent를 가짐 (star_detail_example vs star_section_note)
  - **11단계에서 이 ambiguity를 semantic_tag로 관측**
  - **12단계에서 parent selection hint를 schema에 넣을지 결정**
- fallback 졸업 기준: 15+ chapters에서 fallback_rate=0%
- reconstruct_tree_from_flat은 agreement 비교용으로만 유지 (Phase 2 졸업 후 제거)

---

## Stage 9: Assemble Engine — done (1차 안정화)

### 완료
- 9.0+9.1: section_info pass-through + preserved/residual 관측
- 9.1b: secPr carrier 보존 (layout section 경계 유지)
- 9.2a: append target candidate 관측 (multi_body_section_warning)
- 9.3: tree split projection 비교 (fully_agreed=true)
- 9.5: chapter_split / marker_rewrite debug 분리
- `_orig_para_count` bounds check fix

### 보류 (조건부)

#### 9.2b: append target 정책 변경 — watch
- 현재: max_remove_section (section[0]에 모든 content append)
- 문제: multi-section 양식에서 모든 content가 section[0]에 몰림
- **재검토 조건**: body_sections 3개+ 양식, chapter→section mapping 안정, 2a chapter 정의 안정
- **관련 단계**: 12단계에서 section-aware generation schema 검토 시 함께

#### 9.8: chapter→section mapping append — watch
- source allocation + chapter slot 구조 안정화 후 재검토
- **관련 단계**: 12단계(schema), 14단계(slot-filling)

#### 9-infra: assembly dict 서버 추출 — watch
- assembly 구조 변경 시 선행
- **관련 단계**: 12단계에서 assembly 구조가 바뀌면 이때 함께

### 다른 단계에서 기억할 것
- **section_info pass-through 계약**: section_info 하위 필드 추가 시 서버 코드만 수정, DB tool 변경 불필요
- **secPr carrier 보존**: layout section 경계를 지키기 위해 secPr 보유 문단은 항상 보존
- **multi_body_section_warning=true**: 현재 양식2에서 발생. section-aware append는 미구현
  - 12단계: section-aware 생성이 필요할 경우 assemble의 append 정책 변경 필요
  - 14단계: slot-filling에서 section 단위 배치가 필요할 수 있음
- **tree split projection**: title-scan과 tree-based split이 일치함을 확인 (fully_agreed). 불일치 시 tree-based로 전환 검토

---

## Stage 10: Source Allocation — done (10.0+10.1)

### 완료
- `_find_title_in_text()`: dict 반환 (position, match_method, core_form, context_preview)
- `split_source_by_chapters()`: (sections, decision_log) 반환
- `07b_source_split_decision.json`: per-chapter decision log + allocation summary
- underfill/overfill candidate 집계

### 10단계의 성격
- 이전 3-chapter imbalance 원인을 확정한 단계가 **아님**
- 재발 시 07b decision log로 A/B/C/D 판정이 가능하게 만든 **인프라 단계**
- 이번 실행에서는 5개 chapter exact match로 split 성공
- split 함수 고정 실패보다는 2a chapter output stability가 핵심 변수일 가능성이 큼

### 보류

#### 10.2: A/B/C/D 자동 분류 — watch
- A: split 함수 매칭 실패 / B: 2a가 source에 없는 title 생성
- C: source에 경계 없음 / D: title 형태 불일치
- **재검토 조건**: underfill/fallback/none-match 사례 재관측 시
- **관련 단계**: 13단계에서 source coverage validation과 연계

#### 10.5: Source-to-Template Allocation Redesign — conditional watch
- 2a를 chapter splitter → source-to-template allocator로 전환 검토
- **재검토 조건**: 2a chapter output stability 문제가 반복 확인될 때
- **관련 단계**: 12단계 schema redesign에서 source_refs 설계와 연계

### 다른 단계에서 기억할 것
- **핵심 finding: split 함수 자체보다 2a chapter output stability가 문제**
  - 같은 양식에서 3 chapter vs 5 chapter 편차
  - source imbalance는 split 함수 버그가 아닌 2a 입력 편차
- **source_concentration_ratio**: max_chunk / total — 0.5 이상이면 불균형 후보
- **underfill 기준 (1차)**: chunk_length < 500 AND generated_items == 0
- 12단계: source_refs 필드를 schema에 넣으면 source coverage 추적 가능
- 13단계: source evidence validation의 입력 데이터
- 15단계: Open Notebook에서 source block 단위로 재설계 시 split 함수 대체 가능

---

## Stage 11: Structural Intent / Role Semantics — next

### 목적
format_role(role_cluster)과 semantic_role(structural intent)을 분리 관측하여, 12단계 schema redesign의 결정 재료를 만든다.

### 해결할 문제
1. **role_cluster ambiguity 정량화**: c8에 11종 semantic role 혼재 — 실제 생성에서 얼마나 문제인지
2. **semantic_tag granularity 결정**: 4종(현재) vs 6종(제안) vs 더 세분화
3. **parent context의 semantic 기여도**: parent_role이 semantic 판별에 유용한지
4. **exclusive_rules의 semantic coverage**: variant A/B/C가 이미 semantic 분리를 수행하는 범위
5. **marker vs semantic intent 관계 확인**: 분리 원칙 검증

### 구현 계획
- D1: `infer_semantic_tag()` 순수 함수 추가 (description keyword 기반)
- D2: `12_structural_intent.json` 디버그 파일 추가
- D3: `build_per_type_role_semantics` 결과에 semantic_tag 필드 추가
- D4: 2b 생성 결과에 semantic_tag post-hoc 부여
- D5: parent-semantic correlation matrix
- D6: exclusive_rules variant ↔ semantic_tag 대응

### 캐시 영향
없음 — post-hoc observation/debug에 한정될 때. 1a~1f 산출물 또는 2b prompt에 반영하는 순간 캐시/재실행 기준 재검토.

### 12단계에 넘길 결정 재료
- semantic_tag granularity (6종이 적절한지)
- polysemous cluster 목록
- parent-semantic 상관성
- exclusive_rules coverage
- 실제 생성에서의 semantic ambiguity 영향도

---

## Stage 12: Generation Schema Redesign — not started

### 목적
marker/content 분리, source_refs, run_policy 등 2b output schema를 재설계하여 assemble 품질을 높인다.

### 11단계에서 받을 입력
- semantic_tag 필드 정의 (관측 결과 기반 확정)
- polysemous cluster에 대한 처리 방침 (분할 vs 유지+tag)
- parent-semantic 상관성 데이터

### 예상 작업 (11단계 결과에 따라 변경 가능)
1. **marker/content 분리**: AI는 content만 출력, marker는 format_role + sibling_index 기반 자동 부착
2. **semantic_role / display_role 필드**: 12단계에서 schema에 확정
3. **source_refs**: 생성된 각 item이 source의 어느 부분에서 왔는지 참조
4. **run_policy / emphasis**: 7.5A에서 관측한 multi-run emphasis 구조 반영
   - 7.5A 결과: safe pattern 4개뿐 (1.0%), 대부분 3+ run 구조
   - emphasis_spans 같은 필드로 inline formatting 보존
5. **2a/2b contract redesign**: output schema + validation contract 업데이트

### 기억할 것 (다른 단계에서 옮긴 것)
- **9.2b append target 정책**: section-aware generation schema 필요성을 12단계에서 검토. 실제 append 정책 변경은 schema/slot/source 구조가 안정된 경우에만 진행
- **9-infra assembly dict 서버 추출**: assembly 구조 변경 시 함께
- **8단계 c8 parent disagreement**: parent_selection_hint를 schema에 넣을지 결정
- **6.5 marker rewrite**: content에서 marker를 완전 분리하면 rewrite 로직 단순화 가능
- **E1 heading 장문**: semantic_tag 기반으로 text_type 재분류 후 length validation 정확도 향상

---

## Stage 13: Source Evidence / Coverage Validation — not started

### 목적
생성된 각 item이 source의 어느 부분에서 왔는지 추적하고, source coverage를 검증한다.

### 12단계에서 받을 입력
- source_refs 필드 정의 (item → source 위치 매핑)

### 예상 작업
1. **source coverage check**: source 전체 중 생성에 사용된 비율 측정
2. **hallucination detection**: source에 없는 내용이 생성되었는지 검증
3. **source gap warning**: source에 있지만 생성에 반영되지 않은 내용 알림

### 기억할 것
- **10단계 decision log**: per-chapter source allocation 데이터를 여기서 활용
- **10.2 A/B/C/D 자동 분류**: source coverage 실패 시 원인 자동 분류와 연계
- **underfill detection**: 10단계의 underfill_chapters 데이터 활용

---

## Stage 14: Shallow Template / Slot-Filling — not started

### 목적
간단한 양식(반복 구조가 적고, 슬롯이 명확한 양식)에 대해 full generation pipeline을 거치지 않는 fast path를 제공한다.

### 예상 작업
1. **양식 복잡도 판별**: pattern tree의 depth, role 수, repeatable 비율로 분류
2. **slot-filling 모드**: 양식이 간단하면 2b 대신 직접 slot→source 매핑
3. **section-aware 배치**: 어떤 slot이 어떤 section에 속하는지 매핑

### 기억할 것
- **9.2b section append 정책**: slot-filling에서 section 단위 배치가 필요할 수 있음
  - multi-section 양식에서 slot이 어느 section에 속하는지 결정 필요
  - section_info (9.0+9.1 결과)를 slot-filling에서도 활용
- **9.8 chapter→section mapping**: slot-filling에서는 chapter가 아닌 section 단위 매핑이 자연스러울 수 있음
- **header slot semantic (6.6)**: header 영역은 이미 slot-filling에 가까운 방식으로 처리 중
- **12단계 schema**: slot-filling 모드에서도 같은 output schema를 사용할지, 별도 경량 schema를 쓸지 결정 필요
- **template table filling**: 양식 자체가 표 셀 채우기 중심이면 slot-filling의 특수 케이스로 분리 검토. source-side table extraction은 Stage 15, template-side table filling은 이 단계(14)에서 담당

---

## Stage 15: Open Notebook Source Planning — not started

### 목적
파일(PDF) 기반 source 입력을 source block(구조화된 단위) 기반으로 전환한다.

### 예상 작업
1. **source block 정의**: 제목, 본문, 표, 이미지 등의 구조화된 단위
2. **table block contract**: source 표 → template 표 매핑
3. **source block → chapter/section 할당**: split_source_by_chapters 대체
4. **source block 에디터**: 사용자가 source block을 편집/추가/삭제

### 기억할 것
- **10단계 source split**: 현재 text 기반 split은 임시. source block 단위로 대체 예정
- **table watch items (10단계)**:
  - 현재 table은 opaque block (exemplar clone) 또는 flattened text로 처리
  - source 핵심 정보가 표 안에 있으면 source coverage blocker
  - template 표 셀을 source로 채워야 하는 양식이 나오면 template-side filling blocker
  - source-side extraction과 template-side filling은 분리된 책임
  - **이 단계에서 table block contract 설계**
- **9.8 chapter→section mapping**: source block 단위 할당에서 section mapping 재활용 가능
- **13단계 source_refs**: source block에서 ref를 걸면 coverage 추적이 정확해짐
- **E (전역 전제)**: 파일 기반 source adapter는 임시. 이 단계에서 근본 대체

---

## Stage 16: Internal AI Transition — not started

### 목적
외부 AI API(GPT 등) → 내부 오픈소스 AI(vLLM 등)로 전환한다.

### 예상 작업
1. **prompt 단순화**: 내부 AI는 외부 대비 능력 낮을 수 있음 → prompt/schema 단순화
2. **few-shot / fine-tune**: 축적된 정상 출력을 학습 데이터로 활용
3. **latency 최적화**: 내부 서버 자원에 맞는 batch/parallel 전략

### 기억할 것
- **원칙 6 (AI 자유도 줄이기)**: "알아서 잘 맞춰줘" 방향 피함 — 내부 AI 전환 시 더 중요
- **grammar/validation이 AI 독립적**: grammar 기반 검증/fallback은 AI 품질과 무관하게 동작
- **marker rewrite가 code-based**: marker는 AI에 의존하지 않으므로 AI 전환에 영향 없음
- 현재 내부 LLM 서버(192.168.0.201): 전부 꺼져있음. 복구 시점 미정

---

## 교차 관심사 (Cross-Cutting Concerns)

여러 단계에 걸쳐 추적해야 하는 항목들.

### CC1: Section Handling

section(HWPX의 물리적 레이아웃 단위)과 chapter(논리적 구조 단위)의 관계.

| 단계 | 한 것 / 할 것 |
|------|-------------|
| 9.0+9.1 | section_info debug, preserved/residual 관측 |
| 9.1b | secPr carrier 보존 (layout 경계 유지) |
| 9.2a | append target candidate 관측 (multi_body_section_warning) |
| **9.2b** | **append target 정책 변경 — 보류** |
| **9.8** | **chapter→section mapping — 보류** |
| **12** | **section-aware generation schema 검토** |
| **14** | **slot-filling에서 section 단위 배치** |
| **15** | **source block → section 할당** |

**현재 상태**: 모든 content가 section[0]에 append. multi-section에서 layout이 깨질 수 있지만 현재 observable failure 없음.

### CC2: Table Handling

template과 source 양쪽의 표(table) 처리.

| 단계 | 한 것 / 할 것 |
|------|-------------|
| 현재 | opaque block (exemplar clone) 또는 flattened text |
| **12** | **table cell에 대한 generation schema 검토** |
| **14** | **template 표 셀 filling 필요 시 slot-filling/table-filling sub-step 검토** |
| **15** | **table block contract 설계 (source↔template 매핑)** |

**현재 상태**: 양식 2개에서 table 관련 observable failure 없음 → watch.

### CC3: Source Allocation Chain

source text가 chapter에 할당되는 전체 경로.

| 단계 | 한 것 / 할 것 |
|------|-------------|
| 4 (2a) | chapter title + type 결정 → source split anchor |
| 10.0+10.1 | decision log, allocation summary |
| **10.2** | **A/B/C/D 원인 자동 분류 — 보류** |
| **10.5** | **2a → source-to-template allocator 전환 — conditional** |
| **12** | **source_refs 필드 → item-level source 추적** |
| **13** | **source coverage validation** |
| **15** | **source block 단위로 대체** |

**현재 상태**: split 함수는 정상이지만, 2a output stability가 핵심 변수.

### CC4: Marker / Content 분리

생성된 text에서 marker와 content를 분리하는 과정.

| 단계 | 한 것 / 할 것 |
|------|-------------|
| 6.5 | marker rewrite (allowlist 기반, code-driven) |
| 7.1 | chapter title marker normalization |
| **11** | **marker vs semantic_intent 관계 확인 (관측)** |
| **12** | **AI는 content만 출력, marker 완전 자동 부착** |

**현재 상태**: AI가 marker를 포함해서 출력 → rewrite로 교정. 12단계에서 분리 완성 목표.

### CC5: Role Cluster Ambiguity

같은 role_cluster에 다른 semantic intent가 섞인 문제.

| 단계 | 한 것 / 할 것 |
|------|-------------|
| 8 | c8 parent disagreement 19건 관측 (c6 vs c7) |
| **11** | **semantic_tag 관측, polysemous cluster 목록, parent-semantic 상관성** |
| **12** | **role_cluster 분할 vs semantic_tag 추가 결정** |

**현재 상태**: c8 (11종), c9 (10종) 확인. 생성 품질 영향 미측정.

### CC6: Emphasis / Multi-Run Formatting

inline emphasis (bold, color 등) 보존.

| 단계 | 한 것 / 할 것 |
|------|-------------|
| 7.5A | run formatting 관측 (safe pattern 4개, 1.0%) |
| **12** | **emphasis_spans 같은 필드로 inline formatting 보존** |

**현재 상태**: 대부분(81%) 3+ run 구조라 단순 분리 불가. 12단계에서 schema 확장 필요.

---

## 금지 사항 (전 단계 공통)

- role_cluster 번호 기반 분기 (c4면 Ⅰ 같은)
- 특정 문서명/기관명/정책명 하드코딩
- AI에게 "알아서 맞춰라" 위임
- marker를 content와 뒤섞기
- 실패 사례 수집 없이 hard gate
- 2b prompt 수정 (output schema 외) — 12단계 전까지
- star_depth rewrite — 안 함

---

## 양식 현황

| 양식 | 이름 | sections | chapters | 특이점 |
|------|------|----------|----------|--------|
| 1 | 조달청 업무계획 | 1 (single) | 3 | c8 parent disagreement, 기본 테스트 |
| 2 | 민원인 위법행위 대응지침 | 5 (multi) | 5 | multi-section, secPr carrier, type_3 |

테스트 시 양식 번호에 따라 다른 검증 포인트:
- 양식1: single-section 정상 동작, grammar, marker rewrite
- 양식2: multi-section, secPr 보존, section_info, append target

---

## AI 호출 구조 (현재)

| # | task_name | 단계 | 캐시 |
|---|-----------|------|------|
| 1 | hwpx_structure_analysis | 1a | O |
| 2 | hwpx_1b_role_candidates | 1b | O |
| 3 | hwpx_1c_level_hybrid | 1c | O |
| 4 | hwpx_canonical_clustering | 1e | O |
| 5 | hwpx_canonical_clustering_repair | 1e repair | O (조건부) |
| 6 | hwpx_chapter_classify | 2a | X |
| 7 | hwpx_section_fill_{ch_idx} | 2b x N | X |

캐시 hit 시 1~5 skip → 2a+2b만 호출 (6 + N회).
