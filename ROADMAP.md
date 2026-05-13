# HWPX Generation Pipeline Roadmap

이 파일은 HWPX 파이프라인의 단계별 로드맵입니다.
각 단계에서 무엇을 했고, 무엇이 남았고, 다른 단계에서 기억해야 할 것이 무엇인지 기록합니다.

최종 수정: 2026-05-13

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
| 11 | Role & Style Observation | done (조건부) | 11.1 semantic_tag, 11.2 style profile, 11.3 findings |
| **12** | **Generation Schema Redesign** | **done** | marker/content 분리, template observation, target unit planning |
| 13 | Unit-Aware Generation | **in progress** | 13.0 done, 13.3b-1 done, 13.1 deferred |
| **13.4b** | **Chapter Template Plan Seed** | **in progress** | template-driven chapter loop, broad source fallback, 2b template context |
| **13.5** | **Region Action Plan + Unanalyzed Section Preserve** | **done** | region action plan + unanalyzed section preserve safety |
| **13.6** | **Per-Chapter Subtree + Multi-Section/Source Gate** | **done** | B: per-chapter local_pattern→prompt+validation 연결, A: multi-section diagnostic, C: source diagnostic |
| **13.7a** | **Assembly 수정 (region-first body_split + section-aware)** | **next** | 1a 무변경. body_split을 region-first로, section-aware paragraph 배치 |
| **13.7b** | **Multi-Section Analysis 확장** | not started | 1a 파이프라인 변경. 모든 section 분석 + document-level merge |
| 14 | Open Notebook Source Planning | not started | KB→파일 선택 경로, source contract 유지 |
| 14-table | Table Cell Filling | not started | 표 셀 채우기 (14와 별도 scope) |
| 15 | Source Evidence / Coverage | not started | source coverage validation — 13.7 이후 |
| 16 | Internal AI Transition | not started | 외부→내부 AI 전환 |
| later | Assembly 고도화 | not started | tree→indentation, inline emphasis, section-aware append |

---

## 의존성

```
0~7: 기초 (완료)
  |
  8: Tree/AST ──────────────┐
  |                         |
  9: Assemble ──┐           |
  |             |           |
 10: Source ────┤    11: Role & Style Observation
  |             |           |
  |             └───────────┤
  |                         |
  |                    12: Schema Redesign (done)
  |                         |
  |                    13: Unit-Aware Generation (13.0, 13.3b-1 done)
  |                         |
  |                    13.4b: Chapter Template Plan Seed ← IN PROGRESS
  |                         |
  |                    13.5: Region Action Plan + Section Preserve (done)
  |                         |
  |                    13.6: Per-Chapter Subtree + Gate (done, CC12 해결)
  |                         |
  |                    13.7a: Assembly 수정 (region-first body_split) ← NEXT
  |                         |
  |                    13.7b: Multi-Section Analysis 확장 (CC11)
  |                         |
  |              ┌──────────┤
  |              |          |
  |         14-table   14: Open Notebook (KB 연동)
  |              |          |
  |              └──────────┤
  |                         |
  |                    15: Source Evidence (13.7 이후)
  |                         |
  |                    16: Internal AI
  |
  └── later: Assembly 고도화 (독립)
```

- 9, 10, 11은 8 이후 병렬 가능
- 12는 8 + 10(최소) + 11 결정 완료 후
- 13은 12 이후 (target_unit_plan → generation 연결)
- **13.4b는 13 이후** (template-driven chapter loop — template intent flow 보존 최소 안전장치)
- **13.5는 13.4b 이후** (region action plan + unanalyzed section preserve safety)
- **13.6 완료** (CC12 해결: per-chapter subtree extraction + local_pattern_override validation, A/C diagnostic으로 13.7 scope 확정)
- **13.7a는 13.6 이후** (assembly 수정: region-first body_split + section-aware 배치. 1a 무변경)
- **13.7b는 13.7a 이후** (analysis 확장: 모든 section 1a 분석 + document-level merge [CC11]. source allocation redesign은 watch)
- **14-table과 14는 13.7 이후, 병렬 가능**
- **15는 13.7 이후** (allocation 안정 후 coverage validation)
- **Assembly 고도화는 독립** (tree→layout, section-aware append. 다른 단계와 의존 없음)
- 16은 최후

---

## Stage 0: 관측/디버그 기반 — done

### 완료
- `/tmp/hwpx_debug/` 분리 디버그 파일 체계 (01~11 + 99_summary)
- `/tmp/hwpx_debug_last.json` 하위 호환
- `write_stage_debug_files()` 통합 출력 함수

### 다른 단계에서 기억할 것
- 새 디버그 파일 추가 시 `write_stage_debug_files`에 추가하면 됨
- 파일 번호: 01~05c 분석, 06~07b 2a, 08~09b 2b+validation, 10 assemble, 11 contract, 12 structural_intent, 12b style_profile
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
- CACHE_SCHEMA_VERSION = 4 (11.2에서 idx_full_texts 추가로 bump)
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
- **관련 단계**: 12단계(schema), 13단계(slot-filling)

#### 9-infra: assembly dict 서버 추출 — watch
- assembly 구조 변경 시 선행
- **관련 단계**: 12단계에서 assembly 구조가 바뀌면 이때 함께

### 다른 단계에서 기억할 것
- **section_info pass-through 계약**: section_info 하위 필드 추가 시 서버 코드만 수정, DB tool 변경 불필요
- **secPr carrier 보존**: layout section 경계를 지키기 위해 secPr 보유 문단은 항상 보존
- **multi_body_section_warning=true**: 현재 양식2에서 발생. section-aware append는 미구현
  - 12단계: section-aware 생성이 필요할 경우 assemble의 append 정책 변경 필요
  - 13단계: slot-filling에서 section 단위 배치가 필요할 수 있음
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
- **관련 단계**: 15단계에서 source coverage validation과 연계

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
- 15단계: source evidence validation의 입력 데이터
- 14단계: Open Notebook에서 source block 단위로 재설계 시 split 함수 대체 가능

---

## Stage 11: Role & Style Observation — done (조건부)

### 목적
format_role(role_cluster)과 semantic_role(structural intent)을 분리 관측하고, role별 문체 특징을 수집하여, 12단계 schema redesign의 판단 재료를 만든다.

### 11.1 Semantic Tag / Role Ambiguity Observation — done
- `infer_semantic_tag()`: description keyword 기반 6종 taxonomy (heuristic, pipeline decision 미사용)
- `12_structural_intent.json`: per-paragraph semantic_tag, cluster distribution, polysemous/monomorphic
- `build_per_type_role_semantics`에 semantic_tag + semantic_inference optional field 추가
- has_children_by_grammar / has_actual_children 분리 기록

**핵심 findings:**
- 30 clusters 중 11 polysemous (37%), 실질적 ambiguity는 c8/c12/c27 정도 (3~4개)
- 나머지 polysemous cluster는 dominant tag 80%+ (keyword heuristic 경계 케이스)
- subsection_title 과분류 가능성 (has_children_by_grammar → heading 우선)
- semantic_tag는 heuristic observation이지 확정 semantic_role 아님

### 11.2 Style Profile Observation — done
- `idx_full_texts`: truncation 없는 전체 원문 (max 4788자), CACHE_SCHEMA_VERSION 4
- `12b_style_profile.json`: AI 기반 role별 문체 분석 (batch 2회 호출)
- format_observations / content_style_do / content_style_avoid 분리
- evidence_sample_ids + input_samples로 추적 가능
- semantic_tag_distribution 포함

**핵심 findings:**
- style profile이 role간 차이를 잡아냄 (c8 "각주형 보충" vs c9 "참고형 보충" vs c5 "요약형 슬로건")
- format/content 분리 작동 확인
- polysemous cluster에서 style은 semantic_tag와 독립적으로 일관적인 경우 있음 (c8: tag 3종이지만 "보충 기능"이라는 style은 일관)
- style profile은 관측 결과이며, generation 품질 개선은 12단계에서 검증 필요

### 11.3 12단계로 넘길 Findings
- semantic_tag 6종 taxonomy → 12단계에서 granularity 재검토 (세분화 or 유지)
- style_profile → 12단계에서 style_policy로 전환 여부 판단
- format_observations → marker_policy와 보완 관계, 12단계 schema에서 활용 가능
- content_style_do/avoid → 2b prompt에 넣을지 12단계에서 A/B 비교
- source_refs → 12에서 interface만 열기, 14(Open Notebook)에서 source block 구조 설계, 15에서 실제 coverage validation
- role_cluster만으로 generation 제어하면 안 됨 → role + semantic + style evidence 함께

### 조건부 완료 이유
- 12_structural_intent.json이 cache HIT에서 skip되는 known issue (C-1 보류)
- D4~D6 (2b post-hoc semantic_tag, parent-semantic correlation, exclusive_rules coverage)는 미구현 — 12단계 진행에 필수는 아님

### 다른 단계에서 기억할 것
- **semantic_tag는 heuristic** — 12단계 schema에 정답값처럼 넣으면 안 됨
- **style profile은 관측** — generation에 바로 적용하지 않고 12단계에서 transition plan 필요
- **marker/content 분리 원칙**: 11.2에서 실측 검증됨. 12단계 첫 작업으로 marker/content 분리 schema 설계
- **marker 제거 hard switch 금지**: marker 재부여/validation/debug 준비 전에 AI에게 marker를 빼라고 하면 최종 문서에서 marker 소실 위험

---

## Stage 12: Generation Schema Redesign — done

### 진입 전 체크 (hard prerequisite)
1. **3번째 양식 e2e 관측** — 새 양식에서 실패 패턴이 나오면 schema 설계에 반영해야 하므로 12 전에 수행
2. **10.5 decision gate** — 2a stability를 계속 신뢰할지, 제약할지 판단. 12 초반에 함께 가능

### 목적
marker/content 분리, source_refs, run_policy 등 2b output schema를 재설계하여 assemble 품질을 높인다.

### 11단계에서 받은 입력
- semantic_tag 6종 taxonomy (heuristic, granularity 재검토 필요)
- polysemous cluster: 실질적 ambiguity c8/c12/c27, 나머지는 outlier 수준
- style_profile: role별 문체 특징 (format/content 분리됨, evidence 추적 가능)
- content_style_do/avoid: 2b generation style 가이드 후보 (효과 검증 필요)
- idx_full_texts: full text source 확보됨
- marker/content 분리 원칙: 11.2에서 실측 검증됨

### 첫 작업: marker/content 분리 schema 설계 + transition plan
- **목표**: AI가 content만 출력하고, marker는 format_role + sibling_index 기반으로 자동 부착하는 구조
- **단, 초기에는 transition plan / dual debug / validation을 거쳐 marker 소실이 없음을 확인한 뒤 content-only로 전환**
- marker 재부여/validation/debug가 준비되기 전에 AI에게 marker를 빼라고 하면 최종 문서에서 marker 소실 → hard switch 금지
- marker rewrite(6.5)가 이미 code-driven이므로 전환 기반은 있음

### 예상 작업
1. **marker/content 분리 + transition plan**: 단계적 전환 (schema 설계 → dual debug → validation 확인 → content-only 전환)
2. **semantic_role / display_role 필드**: 11단계 semantic_tag 기반, granularity 재검토 후 확정
3. **source_refs**: 12에서 interface만 열기. 14(Open Notebook)에서 source block 구조 설계. 15에서 실제 coverage validation
4. **run_policy / emphasis**: 7.5A 관측 기반, emphasis_spans 등
5. **style_policy**: 11.2 style_profile을 2b에 넣을지 A/B 비교 — **현재 style profile AI 호출 비활성화됨 (latency 절감). 이 작업 진입 시 DB tool에서 재활성화 필요.**
6. **2a/2b contract redesign**: output schema + validation contract 업데이트

### 기억할 것 (다른 단계에서 옮긴 것)
- **9.2b append target 정책**: section-aware generation schema 필요성 검토. 실제 append 정책 변경은 schema/slot/source 구조가 안정된 경우에만 진행
- **9-infra assembly dict 서버 추출**: assembly 구조 변경 시 함께
- **8단계 c8 parent disagreement**: parent_selection_hint를 schema에 넣을지 결정
- **6.5 marker rewrite**: content에서 marker를 완전 분리하면 rewrite 로직 단순화 가능
- **E1 heading 장문**: semantic_tag 기반으로 text_type 재분류 후 length validation 정확도 향상

---

## Stage 13: Unit-Aware Generation — in progress

### 목적
12.2에서 만든 `target_unit_plan`을 실제 generation route에 연결하여, region별로 적합한 generation strategy를 실행한다.

### Source Contract (합의 사항)
- source = 파일 전문 1~N개의 텍스트 (PDF text 또는 마크다운)
- 14단계에서 KB 연동 시에도 이 형태 유지 (RAG는 파일 식별용, source 자체는 전문)
- 마크다운 구조(heading)가 있으면 split point로 활용 가능

### 완료

#### 13.0: source_blocks adapter (c908865)
- `text_blob_to_source_blocks()`: heading split, 4 fields, debug-only
- `16_source_blocks.json` debug output
- generation output 무변경

#### 13.3: shallow route 기본 동작
- **routing**: `should_use_shallow_route(target_unit_plan)` — chapter 없음 + shallow primary → shallow route
- **2b single-call**: CC7 cache의 `chapter_types.type_1.pattern` 재사용, `shallow_mode=True`
- **assemble safety**: `preserve_indices` (slot/attachment 보존), table text replacement skip
- **table policy**: shallow mode에서 table-like role은 structural placeholder (cell filling deferred)
- **prompt**: shallow mode 지시 5항 (간결/구조반복금지/표보존/단일body/소제목의도유지)
- 검증: CC7 grammar_passed, assembly_fail=0, 조달청 regression 없음

#### 13.3b-1: shallow section plan seed (751489f)
- **extract_shallow_section_plan_seed()**: template evidence 기반 heading 후보 추출
  - required filter: region membership, level threshold, table exclusion
  - evidence scoring: subregion_candidate_heading(+2), role_text_type_heading(+2), grammar_has_children(+1), repeatable_in_region(+1), body_negative(-2)
  - threshold: score >= 2 → seed, otherwise → fallback
- **build_section_fill_prompt(section_plan_seed=)**: seed 있으면 "Template Section Flow" 블록 삽입
- **observe_section_plan_compliance()**: debug-only 사후 관측 (heading count/order/thin/repetition)
- 검증 결과:
  - CC7: seed 4 headings → template 4-section 흐름 보존 (이전: source topic 6개 재구성)
  - compliance: planned=4, generated=4, count_match=True, repetition=False
  - heading adaptation 정상: "주간 AI 활용 현황 (3.30.~4.5.)" → "주간 현황 및 지표"
  - 조달청: shallow route 미진입, chapter route 불변, assembly fail=0
  - 민원인: chapter route 불변, assembly fail=0. **attachment 삭제 문제 관측 → 13.5 blocker로 승격.**
- 설계서: `docs/13_3_shallow_section_planning.md`
- "seed"로 명명 — template_only, source 미고려. 완성형 planner 아님.

#### 13.1: allocation debug — deferred
- 소비자 없어서 defer. shallow는 broad source, chapter는 기존 split_source_by_chapters 유지.

### 하지 않을 것
- table cell fill 구현 (14-table 단계로 분리)
- KB 연동 (14단계)
- internal AI transition (16단계)
- marker rewrite retirement (Phase 3, 별도 timing)
- source allocation 고도화 (13.1 deferred)
- heading reuse policy의 full classifier/schema (later)

### Watch
- **별도 shallow generator 코드 정리**: SHALLOW_FILL_PROMPT 등 미사용. 2b single-call로 대체됨. 삭제 후보.
- **compute_preserve_indices 파일 위치**: source_block_adapter.py → target_unit_plan_utils.py 분리 후보
- **AI가 일부 role 미생성**: role_cluster_5/6/7 미생성. optional이므로 정상일 수 있으나 관측 필요.
- **seed → planner 확장**: 현재 seed는 template_only. source-aware planner (Option B)로 확장 시 seed를 입력으로 받아 adapted plan 생성.

### 기억할 것
- **shallow route는 2a를 header_data 획득용으로만 실행** — chapter output은 무시
- **split_source_by_chapters 공존**: chapter path에서 계속 사용, shallow는 broad source
- **preserve_indices**: shallow route에서만 전달 (slot+attachment). **chapter route는 None → 13.5에서 확장 필요.**
- **table text skip 조건**: `is_tbl_box and preserve_indices` — shallow route에서만 동작
- **idx_full_texts는 cache top-level**: `structure` 안이 아님. DB tool에서 `_idx_full_texts` 변수 사용.

---

## Stage 13.4b: Chapter Template Plan Seed — IN PROGRESS

### 목적

chapter route에서 2a가 source 구조로 template chapter flow를 덮어쓰는 문제를 방지하는 최소 안전장치.
template의 chapter intent flow를 anchor로 삼고, title/content는 source에 맞게 adaptation하는 구조.

### 문제: source가 template 구조를 덮어씀

#### 근본 원인

현재 chapter route의 architecture:
```
2a → source 기반 chapter 추출 → chapter loop 구동 → per-chapter 2b
```

2a의 task 정의가 **"source에서 대제목을 추출하라"**이므로, template의 장 구조와 관계 없이
source content에서 topic을 뽑아 chapter를 만든다. 결과적으로 template의 chapter flow가 완전히 무시된다.

#### 민원인 양식에서 관측된 현상

**template 원본 구조:**
```
Ⅰ. 목 적
Ⅱ. 추진배경 및 경과
Ⅲ. 민원응대 기본방향
Ⅳ. 안전한 근무환경 구축
Ⅴ. 폭언·폭행 등 특이민원 대응방안
Ⅵ. 위법행위에 대한 기관차원의 대응체계 확립
Ⅶ. 민원공무원 근무여건 개선 및 인식개선
Ⅷ. 행정사항
```

**2a가 만든 source 기반 구조 (소스 PDF = 수석보좌관 회의자료):**
```
Ch0: "문제정책 관리제도 평가 및 제도보완 방안"  (body 0개 — 빈 chapter)
Ch1: "외국국적동포 방문취업제 신설 추진 점검"
Ch2: "문서관리카드 처리방안과 개선사항 및 보고서 작성 방법"
```

**출력 파일에서 보이는 증상:**
```
[1] 336차 수석·보좌관 회의                                     ← header slot (정상)
[2] 2005. 11. 21                                              ← header slot (정상)
[3] 차례 문제정책 관리제도.../외국국적동포...                      ← TOC에 source 제목
[4] - 제1장 -민원인의 위법행위 대응문제정책 관리제도 평가 및...    ← 원본 텍스트 + 생성 title concat
[5] - 제1장 -민원인의 위법행위 대응외국국적동포 방문취업제...      ← 같은 문제
[7] 11 대통령 지시 및 초기 검토                                  ← 대제목 없이 소제목부터
```

**문제 분석:**
1. template 8장 → source 기반 3장으로 대체됨 (장 수/순서/의미 전부 소실)
2. role_cluster_3이 slot이면서 title_role이라 dual-use 충돌 (보존 + 생성 동시 발생)
3. 2a header_data에 role_cluster_3이 없어서 slot 텍스트가 변경되지 않음
4. 생성된 chapter title이 보존된 slot 텍스트에 concatenate됨
5. template의 대제목(Ⅰ. 목적 등)이 아예 출력에 없음 — 소제목부터 시작

#### 조달청에서도 해당

조달청 template의 3장도 의미 흐름이 있다:
```
Ⅰ. 추진성과 및 평가      ← 작은 볼륨 (성과 요약)
Ⅱ. 2024년 업무추진 여건 및 방향  ← 중간 볼륨 (방향 설정)
Ⅲ. 2024년 핵심 추진과제     ← 큰 볼륨 (문서 대부분)
```
현재 2a가 source 기반으로 비슷한 3장을 만들어서 우연히 동작하지만,
source가 바뀌면 "3 vs 5 chapter 편차" (10단계 관측)처럼 불안정.

### 해결 방향 선택 이유

| 후보 | 설명 | 채택 여부 | 이유 |
|------|------|----------|------|
| A: 2a prompt 수정으로 template 구조 강제 | 2a에 template chapter plan을 넣어 매핑 | ✗ | 2a의 task 정의 자체가 바뀜. "source chapter 추출"에서 "template chapter 매핑"으로 변경. 과도한 변경. |
| B: template-driven chapter loop + 2a header_data 전용 | chapter loop를 template 기준으로 전환. 2a는 header_data만 사용. | **✓** | shallow route에서 검증된 패턴. 2a 변경 최소화. template 구조 보존 보장. |
| C: source-to-template allocation 전체 개편 | evidence 기반 source→template 매핑 planner | ✗ | 규모가 크고 13.6~13.7 범위. 지금은 최소 안전장치만 필요. |
| D: 2a를 폐기하고 template plan만 사용 | 2a를 아예 안 부름 | ✗ | header_data(제목/날짜/기관 등)를 2a가 추출하므로 여전히 필요. |

**B를 택한 핵심 이유**: shallow route에서 이미 동일 패턴이 검증됨.
shallow route에서도 2a는 header_data 획득용으로만 실행하고, template section plan이 생성을 구동한다.
chapter route에 같은 패턴을 적용하면 일관성 있고 변경이 작다.

### source 전달 방식

template-driven loop에서 per-chapter source split을 쓰면 문제가 생긴다:
- `split_source_by_chapters`는 2a title 기반으로 source를 자름
- template title(Ⅰ.목적, Ⅱ.추진배경 등)로 source를 자르면, source에 해당 heading이 없어서 split 실패
- 결과: 8장 전부 source 미할당 → 빈 생성

따라서 template-driven loop에서는 **broad source fallback**을 사용한다:
- 각 2b에 전체 source를 전달
- 2b가 template chapter context(제목/의도/위치)를 보고 관련 source 내용을 선택
- 비효율(토큰 8배)이지만, 정확도 우선
- 이건 최종 구조가 아니라 임시 안전장치. 13.6~13.7에서 evidence 기반 allocation으로 대체 예정

split_source_by_chapters 결과는 삭제하지 않고 diagnostic으로 보존 (debug에 남김).

### 이번에 하는 것

| 항목 | 설명 |
|------|------|
| `extract_chapter_template_plan_seed()` | target_unit_plan + structure + idx_full_texts에서 chapter 순서/제목/의도/위치 추출 |
| template-driven chapter loop | seed가 있으면 template chapter 기준으로 2b loop 구동 |
| 2a 역할 축소 | seed 유효 시 2a chapters는 loop driver로 사용하지 않고, header_data 전용 |
| 2b template chapter context | 각 2b에 template_title, description, position, paragraph_count 전달 |
| broad source fallback | template-driven loop에서는 split_source 결과 대신 전체 source 전달 |
| fallback 보존 | seed 없거나 confidence 낮으면 기존 2a-driven loop 유지 |
| per-chapter 상태 기록 | filled / insufficient_source debug 수준 |
| dual-use role warning | slot이면서 title_role인 role은 debug/warning 기록 (구조 변경 안 함) |

### seed schema (최소)

```python
{
    "chapters": [
        {
            "template_title": "Ⅱ. 2024년 업무추진 여건 및 방향",
            "description": "업무추진 여건과 향후 방향 설명",
            "position": 2,
            "total_chapters": 3,
            "paragraph_count": 17,
        },
        ...
    ],
    "confidence": "high",
    "evidence": {...},
    "loop_driver": "template_plan",
}
```

title adaptation, intent flow 보존, hallucination 금지는 prompt-level global instruction.

### 하지 않는 것 (명시적 deferred — 12개)

> **13.4b의 broad source fallback과 template-driven chapter loop는 최종 source allocation redesign이 아니라,
> template intent flow를 깨지 않기 위한 최소 안전장치다.**

#### D1. source-to-template allocation redesign (→ 13.6~13.7)

**문제**: 13.4b의 broad source fallback은 각 2b에 전체 source를 보내서 AI가 관련 내용을 골라쓰게 한다.
이건 비효율(토큰 N배)이고, 장 간 content 중복/누락을 막지 못한다.
**최종 구조**: source를 template의 각 chapter 위치에 evidence 기반으로 배정하는 planner가 필요하다.
source_blocks(13.0)를 generation input으로 연결하거나, template chapter별 evidence matching으로 split을 대체해야 한다.
**왜 지금 안 하나**: 규모가 크고, template-driven loop가 안정된 후에 allocation을 고도화하는 순서가 맞다.
**재검토**: 13.6 decision gate에서 해결 방식 선택 → 13.7에서 구현.

#### D2. insufficient_source 정식 정책 (→ 13.6~13.7)

**문제**: template이 8장인데 source가 3장 분량이면, 5장은 source가 부족하다.
지금은 per-chapter status를 debug에 `insufficient_source`로 기록할 뿐이다.
**필요한 것**: 어떤 chapter가 "필수인데 source 부족"인지 vs "preserve/skip 가능"인지 판단하는 정식 정책.
user-facing report ("이 장은 source 부족으로 생성하지 못했습니다")와 production HWP 본문 반영 여부도 결정해야 한다.
**왜 지금 안 하나**: 필수/선택 판단에는 template semantic 분석이 필요하고, 이건 allocation redesign과 함께 해야 한다.
**재검토**: 13.6~13.7에서 allocation과 함께 설계.

#### D3. fixed/repeatable/hybrid template handling (→ auto 실패 5+ 사례 후)

**문제**: template의 top-level chapter가 고유 의미를 갖는 "fixed" 양식과,
동일 pattern이 topic 수만큼 반복되는 "repeatable" 양식이 있다.
실제로는 top-level은 fixed이고 내부 subunit은 repeatable인 "hybrid"도 가능하다.
예: 조달청 Ⅲ장 안에서 과제별 블록이 반복.
**현재 처리**: 별도 policy 없이, `extract_chapter_template_plan_seed()`의 리턴값 유무로 분기.
seed가 나오면 template-driven, 안 나오면 2a-driven.
**왜 지금 안 하나**: template 2개로 분류 체계를 확정하면 과적합. user-facing policy는 auto 감지 실패 사례가 쌓인 후 추가.
**설계 메모**: top-level fixed + 내부 repeatable 공존 가능성은 기억해 둘 것. repeatable exemplar handling은 후속 설계.

#### D4. user override / generation mode (→ auto 실패 사례 충분 시)

**문제**: auto 감지가 잘못 판단할 수 있다. fixed 양식을 repeatable로 보거나 그 반대.
user가 `auto`, `fixed_template`, `repeatable_template`을 선택할 수 있으면 이 위험을 회피할 수 있다.
**왜 지금 안 하나**: auto 실패 사례가 아직 없다. override를 미리 만들면 dead code이고 설계 부채.
auto가 잘 되면 override가 불필요하고, 안 되면 heuristic을 고치는 게 먼저다.
**재검토**: 5개 이상 양식에서 auto 감지 실패가 관측되면 valve/UI 추가 검토.

#### D5. template extension candidate (→ 13.6~13.7)

**문제**: source에 중요한 내용이 있는데 template에 대응하는 chapter가 없을 수 있다.
예: template에는 "목적/배경"만 있는데 source에 "결과/성과"가 중요하면,
grammar가 허용하는 같은 레벨에서 새 chapter를 추가할 수 있어야 한다.
**왜 지금 안 하나**: source↔template 매핑 판단 + grammar 기반 확장 가능성 체크 + 새 chapter slot 생성이 필요.
이건 source-to-template allocation의 핵심 기능이고, 최소 안전장치 범위를 넘는다.
**재검토**: 13.6~13.7에서 allocation redesign과 함께.

#### D6. relative volume hint 고도화 (→ later)

**문제**: template의 각 chapter가 문서에서 차지하는 비중이 다르다.
조달청 Ⅲ장은 전체의 80%+, Ⅰ장은 5% 미만.
2b에 "이 장은 큰 볼륨"이라고 알려주면 분량 배분이 나아질 수 있다.
**현재 처리**: seed에 paragraph_count만 기록. 2b prompt에 volume hint를 넣지 않음.
**왜 지금 안 하나**: AI가 volume hint를 정확히 따를 보장이 없고, 효과 측정이 안 된 상태에서 넣으면 관측 불가.
**재검토**: 13.4b 결과에서 장별 분량 불균형이 observable failure이면 그때 추가.

#### D7. chapter intent 정교화 (→ 필요 시)

**문제**: 2b에 "이 장은 성과 평가 목적이다"라고 알려주면 생성 품질이 나아질 수 있다.
하지만 template title에서 intent를 자동 추론하려면 AI가 필요하다.
**현재 처리**: template title text + 1a description을 그대로 사용. 별도 intent 추론 AI 호출 없음.
**왜 지금 안 하나**: description이 이미 역할 설명을 포함하고 있어 별도 추론이 불필요할 수 있다.
별도 AI 호출은 latency 증가. 효과 미검증.
**재검토**: 13.4b 결과에서 2b가 chapter의 의도를 잘못 파악하는 사례가 나오면 검토.

#### D8. table cell filling (→ 14-table)

별도 scope. template 표 셀을 source content로 채우는 기능.
13.4b에서 table은 기존대로 exemplar clone (구조만 보존, content 미변경).

#### D9. source evidence / coverage validation (→ 15)

generation output이 실제 source evidence를 얼마나 반영했는지 검증.
hallucination detection, source gap warning 포함.
13.4b에서는 broad source를 보낼 뿐, 어떤 source 부분이 사용됐는지 추적하지 않는다.
allocation이 안정된 후(13.7+) coverage validation이 의미 있다.

#### D10. section-aware append 전체 재설계 (→ later)

현재 모든 generated content가 section[0]에 append. multi-section 양식에서 layout 깨짐 가능.
13.5에서 attachment preserve를 최소 변경으로 해결한 후, 근본적 multi-section append는 별도 작업.

#### D11. dual-use role 구조 변경 (→ 13.5+ 또는 later)

**문제**: 민원인의 role_cluster_3은 slot(idx=3, "- 제1장 - 민원인의 위법행위 대응")이면서
동시에 chapter type_1의 title_role(chapter title exemplar)이다.
slot으로서 header_indices에 보존되고, title_role로서 exemplar clone 대상이기도 하다.
이 충돌 때문에 원본 텍스트에 생성 텍스트가 concatenate되는 현상이 발생.
**현재 처리**: debug/warning으로 기록. 구조 변경(role 분리, exemplar 선택 로직 변경) 안 함.
**왜 지금 안 하나**: template-driven loop로 전환하면 2a chapters가 loop driver가 아니게 되므로,
chapter title이 template chapter plan에서 오고 별도 exemplar 매핑이 필요해진다.
이 구조 변경은 13.4b loop 전환이 안정된 후에 하는 게 안전하다.

#### D12. multi-section body 분석 누락 (→ 별도 watch, CC11)

**문제**: 민원인 HWPX 파일은 5개 section을 가진다.
section4에 "제2장 - 반복민원 대응" 본문 Part 2 (559 p elements, 385 with text)가 있다.
그런데 truncated XML(291 paragraphs)에는 section4 내용이 포함되지 않는다.
원인: analyze_hwpx 또는 truncate_xml이 section0만 처리하거나, 문단 수 제한에 걸린 것으로 추정.
**영향**: 문서 후반부 전체(반복민원 대응, 서식 1~5 등)가 분석/생성 대상에서 빠짐.
**왜 지금 안 하나**: 13.4b는 template chapter flow 보존이 목적이고, 분석 범위 확장은 1a 단계 이슈.
현재 section0만으로도 8장 구조(Ⅰ~Ⅷ)가 전부 포함되어 있어 13.4b 검증에는 지장 없다.
**재검토**: 13.4b 이후 별도 조사. `analyze_hwpx`의 section 처리 로직 확인 필요.

### 검증 기준

| 양식 | 확인 항목 |
|------|----------|
| 민원인 | 8장 구조(Ⅰ~Ⅷ) 유지, title이 source에 맞게 adaptation, 대제목 존재, assembly fail=0 |
| 조달청 | template-driven loop 적용, 3장 구조 유지, grammar pass, assembly regression 없음 |
| CC7 | shallow route 불변, section plan seed 동작 불변 |

### 다른 단계에서 기억할 것

- **broad source fallback은 임시**: 13.6~13.7에서 evidence 기반 allocation으로 대체 예정
- **2a는 header_data 전용**: seed 유효 시 2a chapters는 무시. shallow route 패턴과 동일
- **split_source_by_chapters 결과는 diagnostic**: template-driven loop에서는 사용하지 않지만 debug에 보존
- **chapter_template_plan_seed와 shallow_section_plan_seed는 별개**: 각각 chapter route / shallow route 전용. 서로 건드리지 않음
- **repeatable은 내부 subunit 수준**: top-level chapter 개수 변경이 아닌, chapter 내부 항목 반복. 후속 설계 대상

---

## Stage 13.5: Attachment/Table Preserve — BLOCKER

### 문제 (민원인 양식에서 관측)

민원인 template은 5개 HWP layout section을 가진다.

| section | 역할 | remove 문단 | append 문단 |
|---------|------|-----------|-----------|
| section[0] | 본문 (slot + chapter x8) | 322 | **65 (전부 여기)** |
| section[1] | attachment (붙임 1~3) | 85 | **0** |
| section[2] | attachment | 3 | **0** |
| section[3] | (secPr carrier only) | 0 | 0 |
| section[4] | attachment | 192 | **0** |

target_unit_plan에서 attachment region (101p)이 section[1,2,4]에 분포. 현재 chapter route에서는 `preserve_indices=None`이라 **attachment 101p가 전부 삭제**되고 재삽입되지 않는다.

### 왜 blocker인가

- 본문 chapter generation이 성공해도 붙임 자료가 사라지면 최종 HWP가 불완전하다.
- 결과 품질 판단을 왜곡한다 (본문만 보고 "잘 됐다"고 판단할 수 없음).
- watch가 아니라 해결해야 할 구조적 결함이다.

### 후보 방향 (확정 아님 — 별도 설계 필요)

1. shallow route에서 쓰던 `preserve_indices` 메커니즘을 chapter route에도 확장 검토
2. target_unit_plan 기반으로 slot/attachment/table preserve 대상 계산 (`compute_preserve_indices`의 chapter route 확장)
3. body/chapter generation 대상과 preserve 대상의 section별 삭제/append 정책 분리
4. section[1,2,4]처럼 attachment-only section은 secPr carrier만 남기지 말고 attachment content 보존 여부를 명시적으로 결정
5. table filling/generation과 attachment preserve는 분리해서 다룰 것

### 하지 않을 것

- table cell filling (14-table 별도)
- section-aware append 정책 변경 (9.2b — 별도 판단)
- attachment content를 AI로 재생성 (preserve가 기본)

### 의존성

- 13 완료 후 진입 가능 (target_unit_plan의 region 분류에 의존)
- 14(KB 연동)과 독립 — 병렬 가능
- 14-table과 독립 — attachment preserve와 table filling은 별도 책임

---

## Stage 13.6: Source Allocation Decision Gate — not started

### 목적

2a→source split 불안정의 **해결 방식을 선택**한다. "문제인지 판단"이 아니라 "어떻게 고칠지" gate.

### 이미 관측된 문제

- 같은 양식에서 2a가 3 chapter vs 5 chapter 편차 (10단계 finding)
- source split 불균형: [154, 219, 40360] (한 chapter에 source 99% 집중)
- 2a title이 source에 없으면 split 실패 → chapter에 source 미할당
- 07b_source_split_decision.json으로 사후 진단 가능, 보정 메커니즘 없음

### gate에서 결정할 것

1. **2a 안정화**: 2a prompt/schema 개선으로 chapter output stability 확보 가능한가?
2. **source_blocks 기반 allocation**: 13.0의 source_blocks를 chapter→source 매핑에 사용하면 title match 의존도를 낮출 수 있는가?
3. **fallback chain**: split 실패 시 broad source로 fallback하는 게 나은가, 재시도가 나은가?
4. **multi-section append**: 13.5 이후 attachment가 보존된 상태에서, body content가 section[0]에만 가는 것이 실제 문제인지 관측
5. **document-level context**: 2b가 다른 chapter를 모르는 것이 실제 content 중복/누락을 유발하는지 관측

### 산출물

- 해결 방식 선택 문서 (13.7 구현 범위 결정)
- multi-section / document-context watch 유지 or 승격 판단

### 의존성

- 13.5 이후 (attachment 보존 상태에서 source 문제의 실제 영향 측정)

---

## Stage 13.7: Source-to-Template Allocation Redesign — not started

### 목적

13.6에서 선택한 방식으로 source→chapter allocation을 개선한다.

### 후보 방향 (13.6에서 확정)

1. source_blocks를 generation input으로 연결 (13.1 deferred → 여기서 소비)
2. chapter title 기반 split 보완 또는 대체
3. source concentration / empty chapter / coverage 지표 도입
4. allocation 결과를 debug에 기록 (17_source_allocation.json)

### 의존성

- 13.6 decision gate 결정 후 진입
- 15 (source coverage)의 선행 — allocation이 안정돼야 coverage 검증이 의미 있음

---

## Stage 14: Open Notebook Source Planning — not started

### 목적
파일(PDF) 직접 업로드 기반 source 입력을 Knowledge Base(오픈노트북) 연동으로 전환한다.

### Source Contract (13단계 설계 시 합의)
- **source 형태**: 파일 전문 1~N개의 텍스트 (현재 PDF text blob과 동일 형태)
- **파일 식별**: RAG로 관련 chunk 탐색 → chunk 출처 파일 식별 → 해당 파일 전문 획득
- **allocation 책임**: 생성기 (13단계에서 구현된 region-based allocation 그대로 사용)
- **결론**: 13단계 generation pipeline과 호환. 14에서는 "KB에서 파일 선택" 경로만 추가.

### 시스템 현황 (조사 결과)
- **Knowledge Base**: 컬렉션 1개 = 파일 여러 개. RAG(벡터 검색)으로 관련 chunk 추출 가능.
- **Notes**: 노트 1개 = 마크다운 1장. 첨부 시 전문 한 덩어리 전달.
- **File**: 파일 1개. 첨부 시 전문 전달.
- RAG chunk는 단편적(300자 조각)이라 문서 생성 source로 부적합 → 파일 단위 전문 사용이 적합.
- 채팅에서 note/file 첨부: `retrieval/utils.py` — type별 분기 (note→md전문, file→content전문, collection→RAG)

### 예상 작업
1. **KB→파일 선택 경로**: RAG chunk 출처 trace → 관련 파일 식별 → 전문 획득 API
2. **multi-file source 처리**: 파일 2개+ 선택 시 source 결합 또는 파일별 독립 allocation
3. **table block contract**: source 표 → template 표 매핑
4. **source block 에디터 (선택)**: 사용자가 source block을 편집/추가/삭제

### 기억할 것
- **10단계 source split**: 현재 text 기반 split은 임시. source block 단위로 대체 예정
- **table watch items (10단계)**:
  - 현재 table은 opaque block (exemplar clone) 또는 flattened text로 처리
  - source 핵심 정보가 표 안에 있으면 source coverage blocker
  - template 표 셀을 source로 채워야 하는 양식이 나오면 template-side filling blocker
  - source-side extraction과 template-side filling은 분리된 책임
  - **이 단계에서 table block contract 설계**
- **9.8 chapter→section mapping**: source block 단위 할당에서 section mapping 재활용 가능
- **15단계 source_refs**: source block에서 ref를 걸면 coverage 추적이 정확해짐
- **E (전역 전제)**: 파일 기반 source adapter는 임시. 이 단계에서 근본 대체
- **RAG는 파일 식별용**: chunk를 source로 쓰지 않음. 관련 파일 선택 후 전문 사용.

---

## Stage 15: Source Evidence / Coverage Validation — not started (14 이후)

### 목적
생성된 각 item이 source의 어느 부분에서 왔는지 추적하고, source coverage를 검증한다.

### 왜 14 이후인가
- 14(Open Notebook)가 source 입력 포맷을 근본적으로 바꿈 (PDF → source block)
- 현재 PDF source에 맞춰 coverage validation을 깊게 구현하면 14 이후 갈아엎어야 함
- 12단계에서 source_refs interface만 열어두고, 실제 coverage logic은 source 구조 확정 후

### 12단계에서 받을 입력
- source_refs 필드 정의 (interface만 — item → source 위치 매핑)

### 예상 작업
1. **source coverage check**: source 전체 중 생성에 사용된 비율 측정
2. **hallucination detection**: source에 없는 내용이 생성되었는지 검증
3. **source gap warning**: source에 있지만 생성에 반영되지 않은 내용 알림

### 기억할 것
- **10단계 decision log**: per-chapter source allocation 데이터를 여기서 활용
- **10.2 A/B/C/D 자동 분류**: source coverage 실패 시 원인 자동 분류와 연계
- **underfill detection**: 10단계의 underfill_chapters 데이터 활용
- **14단계 source block**: source block 구조가 확정되면 coverage 추적이 더 정확해짐

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
| **13** | **slot-filling에서 section 단위 배치** |
| **14** | **source block → section 할당** |

**현재 상태**: 모든 content가 section[0]에 append. multi-section에서 layout이 깨질 수 있지만 현재 observable failure 없음.

### CC2: Table Handling

template과 source 양쪽의 표(table) 처리.

| 단계 | 한 것 / 할 것 |
|------|-------------|
| 현재 | opaque block (exemplar clone) 또는 flattened text |
| **12** | **table cell에 대한 generation schema 검토** |
| **13** | **template 표 셀 filling 필요 시 slot-filling/table-filling sub-step 검토** |
| **14** | **table block contract 설계 (source↔template 매핑)** |

**현재 상태**: 양식 2개에서 table 관련 observable failure 없음 → watch.

### CC3: Source Allocation Chain

source text가 chapter에 할당되는 전체 경로.

| 단계 | 한 것 / 할 것 |
|------|-------------|
| 4 (2a) | chapter title + type 결정 → source split anchor |
| 10.0+10.1 | decision log, allocation summary |
| 13.0 | source_blocks adapter (debug-only, generation 미연결) |
| **13.4b** | **template-driven chapter loop + broad source fallback (최소 안전장치)** |
| **13.5** | **attachment preserve (source와 독립, 하지만 보존 대상 결정에 target_unit_plan 사용)** |
| **13.6** | **source split 불안정 해결 방식 결정 gate** |
| **13.7** | **source_blocks → generation 연결, split 보완/대체** |
| **14** | **KB에서 RAG 기반 파일 선택 → 파일 전문 획득 경로 추가** |
| **15** | **source coverage validation (13.7 이후)** |

**현재 상태**: 13.4b에서 template-driven chapter loop 도입 중. broad source fallback은 최종 구조가 아닌 임시 안전장치.
**관측된 문제**: 2a가 source 기반 chapter를 만들어 template 장 구조를 덮어씀 (민원인: 8장→3장). 같은 양식에서 3 vs 5 chapter 편차. source concentration [154, 219, 40360].
**13.4b 합의**: template intent flow를 anchor로 삼되 title은 source에 맞게 adaptation. 정식 allocation redesign은 13.6~13.7.
**Source contract (합의)**: source는 파일 전문 1~N개의 텍스트. 14에서 KB 연동 시에도 유지.

### CC4: Marker / Content 분리

생성된 text에서 marker와 content를 분리하는 과정.

| 단계 | 한 것 / 할 것 |
|------|-------------|
| 6.5 | marker rewrite (allowlist 기반, code-driven) |
| 7.1 | chapter title marker normalization |
| **11** | **marker vs semantic_intent 관계 확인 (관측), format/content 분리 실측 검증** |
| **12** | **marker/content 분리 schema 설계 + transition plan → 검증 후 content-only 전환** |

**현재 상태**: AI가 marker를 포함해서 출력 → rewrite로 교정. 12단계에서 transition plan을 거쳐 단계적 전환. hard switch 금지.

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

### CC7: Template Diversity / Regression

양식 과적합 방지와 일반화 검증.

| 시점 | 할 것 |
|------|------|
| **12 진입 전** | **3번째 양식으로 현재 파이프라인 e2e 관측 (hard prerequisite)** |
| 12 완료 후 | 3번째 양식 재검증 (schema 변경 후 regression) |
| 13/14 이후 | 4번째 양식 추가 검토 |

- 새 양식에서 깨진 문제는 특정 양식 하드코딩으로 고치지 않고 evidence 기반으로 일반화
- 현재: 양식1 (조달청, single-section), 양식2 (민원인, multi-section) — 2개 양식 검증

### CC8: Cache / Schema / Test Strategy

각 단계에서 반드시 확인할 체크리스트.

- cache schema 변경 여부 → CACHE_SCHEMA_VERSION bump 필요 여부
- cache HIT로 확인 가능한지 vs cache MISS/no-cache 테스트 필요한지
- 같은 책임 범위 변경을 묶어서 한 번에 e2e 테스트할 수 있는지
- debug output이 cache HIT/MISS에서 달라지는지
- DB tool 변경 시 웹 UI 편집기 리스크 확인

### CC9: Layout Fidelity / Rendering Polish

최종 문서의 물리적 형태 보존. 현재 blocker 아님 (later/watch).

| 항목 | 현재 상태 | 관련 단계 |
|------|----------|----------|
| indentation (들여쓰기) | format_rules로 tab/space 자동 부착 중 | 12 (schema에서 indent_policy 가능성) |
| manual line breaks / line wrapping | 미처리 | 12 (line_break_policy 가능성) |
| paragraph spacing | 미처리 | 12 (paragraph_layout_ref 가능성) |
| template 문단 모양 보존 | exemplar clone으로 일부 보존 | assemble 개선 시 |
| bold/color/inline emphasis | 7.5A 관측 완료, 미반영 | 12 (run_policy/emphasis_spans) |

- 12단계 schema에서 line_break_policy / paragraph_layout_ref / run_policy 자리를 열어둠
- 실제 보정은 assemble/rendering 단계에서 처리
- 현재 observable layout failure가 심각하지 않으면 watch 유지

### CC10: Template-First vs Source-First Generation

chapter loop를 template이 구동하는가 / source(2a)가 구동하는가.

| 단계 | 한 것 / 할 것 |
|------|-------------|
| 4 (2a) | source 기반 chapter 추출 — chapter loop 구동 |
| **13.3b-1** | **shallow route에서 template section plan seed → 2b에 template flow 보존** |
| **13.4b** | **chapter route에서 template-driven chapter loop 도입. 2a는 header_data 전용으로 축소** |
| **13.6~13.7** | **정식 source-to-template allocation redesign** |

**현재 상태 (13.4b)**: template plan seed가 있으면 template chapter 기준 loop, 없으면 2a 기준 loop (fallback).
**합의된 원칙**:
- template의 chapter intent flow를 anchor로 삼는다
- title text는 source에 맞게 adaptation 허용
- source 기반 목차가 template 전체 구조를 대체하는 것은 금지
- source 부족 chapter는 hallucination 없이 insufficient_source로 남김
- top-level chapter는 template-driven, 내부 subunit은 반복 가능 (후속 설계)
- broad source fallback은 최종 구조가 아닌 임시 안전장치

### CC11: Multi-Section Analysis Coverage

HWPX의 여러 section이 분석에 모두 포함되는지.

| 양식 | section 수 | 분석 포함 | 비고 |
|------|-----------|----------|------|
| 조달청 | 1 | 전부 | single-section |
| CC7 | ? | ? | 미확인 |
| 민원인 | 5 | **section0만** | section1/2/4 (attachment 85+3+192p) 전부 미분석 |

**근본 원인 (13.5에서 확인)**: `extract_section_xml()` (hwpx_analyzer.py:96)이 `section_names[0]`만 반환. section0.xml만 1a~1f 분석 대상. sections 1~4는 입력 자체가 안 됨.

**수치**:
- 민원인 HWPX: 5개 section 파일 (section0: ~2534p, section1: ~373p, section2: ~319p, section3: ~1p, section4: ~559p)
- 1a 분석: 291 paragraphs (section0의 truncated subset만)
- assemble: 전체 5개 section 순회 → 분석 안 된 section1/2/4의 body-level 280 paragraphs 전부 삭제
- Gap: 311 paragraphs가 1a 분석에 포함되지 않음

**13.5 안전장치**: `analyzed_sections={0}` 전달 → assemble에서 unanalyzed sections(1,2,3,4) paragraph removal skip. "unanalyzed section preserve safety"로 명명 — 근본 해결이 아닌 안전 정책.

**왜 안전장치만으로 부족한가**: section1/2/4의 구조, 역할, 채움 대상 여부를 이해하지 못하고 단순 보존만 함. 향후 section 내부에 생성/수정 대상이 있을 수 있음.

**근본 해결 (13.6 blocker)**:
1. `extract_section_xml()` → 모든 section XML 반환
2. section별 1a 구조 분석 (role, level, marker, parent)
3. section-aware structure merge (document-level)
4. section-aware target_unit_plan (region에 소속 section 정보 포함)
5. section-aware generation (어느 section의 어느 chapter에 source를 채울지)
6. section-aware assembly (section별 layout/용지/여백 유지)

**재검토 시점**: 13.6 핵심 항목.
**주의**: attachment preserve 문제로만 취급하면 안 됨. multi-section 문서 전체 처리 능력의 문제.

### CC12: Chapter-Local Pattern Preservation (대제목별 하위 양식 보존)

> "Top-level chapter flow preservation is not enough. Each top-level chapter owns its local sub-patterns, and those local patterns are part of the chapter's semantic intent. Generation must fill source content within the original chapter-local pattern rather than selecting arbitrary patterns from elsewhere in the template."

**문제**:
- 13.4b에서 top-level chapter 흐름(Ⅰ→Ⅱ→Ⅲ)은 보존됨
- 하지만 대제목만 유지하는 것으로 부족. 각 대제목 아래 하위 role pattern/paragraph structure가 다를 수 있음
- 예: 조달청 `Ⅰ. 추진성과 및 평가`(평가용 하위 구조) vs `Ⅲ. 핵심 추진과제`(과제 설명용 하위 구조)
- source 내용이 많다고 해서 Ⅲ장용 pattern을 Ⅰ장에 가져다 쓰거나, Ⅰ장용 짧은 pattern을 Ⅲ장에 쓰면 chapter-local intent가 깨짐
- 현재 2b는 `dominant_chapter_type` 1개의 pattern으로 모든 chapter를 생성 → chapter 간 local pattern 차이를 반영하지 못함

**원칙**:
- top-level chapter는 제목뿐 아니라 그 chapter에 속한 하위 pattern까지 포함하는 generation unit
- 각 chapter의 하위 양식은 그 chapter intent를 표현하는 template evidence
- generation은 source에 맞춰 pattern을 임의 선택하지 않고, 원래 template chapter에 속한 pattern 안에서 내용을 채움
- source가 해당 chapter pattern을 충분히 채우지 못하면 다른 chapter pattern으로 바꾸지 말고 insufficient_source/preserve/skip으로 처리
- 반복/확장은 해당 chapter의 grammar, parent context, marker policy가 허용하는 범위 안에서만

**현재 위치**:
- 13.4b: top-level chapter loop = template-driven (첫 단계 해결)
- chapter-local 하위 pattern preservation은 미완성
- 현재 `chapter_types`에 type이 1개인 양식(조달청)에서는 표면화되지 않음
- type이 여러 개이거나 같은 type 내에서 chapter별 하위 구조가 다른 양식이 나오면 드러남

**조달청에서 확인된 gap (13.5 세션에서 관측)**:

조달청 3개 chapter의 local pattern이 명백히 다름:
```
Ⅰ.평가:  17p, roles 6종 (c5/c6/c7/c9),    max_level=3, 얕은 평가 구조
Ⅱ.여건:  17p, roles 7종 (c10/c11 등장),     max_level=4, 여건/방향 분리 구조
Ⅲ.과제: 186p, roles 11종 (c12~c18 고유),    max_level=6, 깊은 과제 반복 구조
```

`chapter_types`에 type_1~type_4까지 4개 type이 정의되어 있음에도, `_find_dominant_chapter_type`이 type_1 하나만 선택하여 3개 chapter 모두에 적용. **chapter-local pattern이 이미 손실되고 있음.**

이 문제는 "나중에 type이 여러 개인 양식이 나오면 보자"가 아니라, **이미 조달청에서 발생 중인 chapter-local pattern detection gap**임.

**13.6 핵심 이슈 후보** — 단순 later/watch가 아님.

**13.6 핵심 해결 후보: Per-template-chapter subtree extraction**

dominant_chapter_type clustering을 개선하는 방향이 아니라, 각 top-level chapter의 실제 paragraph로부터 local subtree를 직접 추출하는 방향.

```
현재:  chapter_types clustering → dominant_type 1개 선택 → 모든 chapter에 적용  (손실 발생)
제안:  target_unit_plan region → 해당 region의 paragraphs → local pattern dict 구축  (직접 추출)
```

**구현 아이디어**:
1. top-level chapter heading(target_unit_plan의 chapter region)을 기준으로 chapter boundary 결정
2. 각 chapter region의 paragraph_indices → structure에서 role/level/parent 조회 → local role pattern dict 구축
3. 해당 chapter의 paragraph exemplar text로 local catalog 구축
4. 2b 호출 시 global dominant pattern 대신 해당 chapter의 local pattern + catalog 전달
5. `extract_chapter_template_plan_seed`를 확장: seed에 `local_pattern`, `local_catalog` 추가

**기존 chapter_types와의 관계**:
- chapter_types는 삭제하지 않음 — grouping/labeling/debug 역할로 유지
- subtree 추출 실패 시 dominant type으로 fallback
- generation용 패턴은 per-chapter subtree가 우선

**주의점 — subtree boundary 기준은 고정 level이 아니다**:

`level == 0`으로 자르거나 `Ⅰ/Ⅱ/Ⅲ` 문자열로 자르면 안 됨. 문서에 따라 generation unit 기준 heading이 다를 수 있음:
- 어떤 양식: Ⅰ/Ⅱ/Ⅲ (level 0) heading이 기준
- 어떤 양식: 큰 section 아래 1/2/3 (level 1) heading이 실제 chapter 단위
- 어떤 양식: level 2 heading이나 특정 parent 아래 반복 block이 generation unit

**기준 heading은 evidence 기반으로 판단**:
- target_unit_plan region (12.2 AI가 판정한 chapter region)
- parent_idx / sibling order
- marker_policy (heading marker type)
- role semantics (heading vs body)
- chapter region evidence (반복 패턴, depth 분포)
- grammar / pattern tree

**subtree 추출 방식**:
- 기준 heading부터 시작 → 같은 parent/context 안에서 다음 동일 기준 heading 전까지를 subtree로 묶음
- 하위 문단, 하위 heading, 표, bullet, attachment/table placeholder 포함
- section boundary와 layout boundary는 유지

**fallback**:
- 기준 heading 확정 불가 시 억지로 level 0으로 자르지 않음 → ambiguity/debug로 남김
- 필요 시 dominant chapter_type fallback 사용 + fallback 발생 이유 기록

**기타 주의점**:
- subtree에서 role hierarchy 재구성 시 sibling ordering과 repeatable 판단은 grammar 데이터 교차 필요
- multi-section analysis (CC11)와 연결: section별 구조를 모두 분석해야 section 간 chapter subtree도 정확히 추출 가능

**원인 분석 (확인됨)**:
- `chapter_types`에 type_1~4까지 4개 type이 존재하지만, `_find_dominant_chapter_type`이 하나만 선택
- type_1: c5/c6/c9, type_2: c10→c6, type_3: c10→c5, type_4: c12→c5/c13 — 서로 다른 pattern
- 조달청 Ⅰ(6 roles, depth 3), Ⅱ(7 roles, depth 4), Ⅲ(11 roles, depth 6)가 각각 다른 type에 해당
- per-chapter subtree 추출이면 이 문제가 구조적으로 해결됨

**현재 상태**: **13.6에서 해결됨.** `extract_per_chapter_pattern()`으로 per-chapter subtree 직접 추출 + `pattern_to_grammar()`로 local grammar 변환 + `process_section_fill_result(override_grammar=)` 연결.

조달청 검증: Ch0=[c5,c6,c9], Ch1=[c10], Ch2=[c12] — 3개 chapter 각각 다른 local_pattern으로 validation 통과. grammar_violation=0, fallback=0.

---

### 13.6 완료 기록 (2026-05-13)

#### 13.6 = gate 단계. 확인된 gap 1개 구현 + 의심 항목 2개 측정.

**B: Per-Chapter Subtree Extraction — 구현 완료 (CC12 해결)**

| 구현 | 내용 |
|------|------|
| `extract_per_chapter_pattern()` | chapter region paragraphs → parent_idx 기반 tree 구축 → local_pattern/catalog 추출 |
| `pattern_to_grammar()` | local_pattern → grammar 변환 (validate/fallback/reconstruct용) |
| `extract_chapter_template_plan_seed()` 확장 | seed에 local_pattern, local_catalog, local_title_role, pattern_source 추가 |
| `process_section_fill_result()` 확장 | override_grammar, override_root_roles 파라미터로 per-chapter grammar 사용 |
| DB tool | per-chapter pattern/catalog 전달 + override_grammar 전달 |

검증 결과:

| 양식 | pattern_source | grammar_violation (before→after) | fallback (before→after) | assembly |
|------|---------------|----------------------------------|------------------------|----------|
| 조달청 | 3/3 per_chapter_subtree | Ch1: 4→**0**, Ch2: 15→**0** | Ch1: 5→**0**, Ch2: 16→**0** | 20/0 |
| 민원인 | 8/8 per_chapter_subtree | 대부분 0 (Ch1: 1건 AI 실수) | Ch1: 1건 | 27/0 |
| CC7 | N/A (shallow) | N/A | N/A | 22/0 |

**A: Multi-Section Diagnostic — 관측 완료**

`diagnose_multi_section()`: section role classification 없이 관측값 중심.
- observations 3축: layout_heterogeneity, content_significance, preserve_adequacy
- gate_decision: 관측에서 파생된 결론 (분리)

민원인 결과:
- 5 sections, layout heterogeneous (orientation/margin 차이)
- unanalyzed: 284p (47%)
- gate: **blocker** — multi-section full analysis + section-aware assembly 필요

조달청/CC7: single-section → skip

**C: Source Diagnostic — 관측 완료**

chapter loop debug에 source_diagnostic 추가.
- 조달청: 41K chars, 31K total tokens (3 chapters), anomalies=0, 전부 filled
- 민원인: broad source × 8 chapters, 4/8 insufficient_source — source coverage 부족 가능성

#### 13.7 Scope 확정 (gate decision)

| 항목 | 판정 | 근거 |
|------|------|------|
| Multi-section full analysis + section-aware assembly | **blocker** | 민원인 47% unanalyzed, layout heterogeneous, section4에 본문 content 존재 |
| Title-level-independent body_split/tree alignment | **blocker** | 민원인 title=level=1 → body_split 실패 → tree_available=false (pre-existing) |
| Source-to-template allocation redesign | **watch** | C diagnostic에서 clear blocker evidence 없음. insufficient_source는 source coverage 부족 가능성 |
| D11 dual-use title/slot concat | **watch** | pre-existing, 13.6에서 악화 없음 |
| AI parent_id 단일 fallback | **watch** | 민원인 Ch1에서 1건, harmless |
| Marker rewrite fallback debug 이상 | **watch** | tree_available=false의 증상, debug-only (출력 무영향) |

#### 왜 13.7을 a/b로 나누는가

**한 번에 하면 위험한 이유**: multi-section full analysis(1a 파이프라인 변경)와 section-aware assembly(assemble 변경)는 독립적인 문제다. 1a 파이프라인을 바꾸면 cache, structure, target_unit_plan, grammar, marker_policy 전부에 영향이 가는데, assembly 문제(tree_available=false, body_split 실패)는 analysis 변경 없이 해결 가능하다. 두 작업을 합치면 문제 원인 분리가 어렵고, regression 범위가 커진다.

**13.7a만으로 "양식 골격이 안정적으로 나오는 첫 단계" 충족**: section1~4를 preserve하면서(13.5 safety) body_split/tree alignment만 고치면, 사람이 볼 때 장/section 골격이 망가지지 않는다. section1~4 content 생성은 13.7b에서 해도 됨.

#### 13.7a: Assembly 수정 (1a 파이프라인 무변경)

**해결하는 문제**:

1. **tree_available=false** — 민원인 title(role_cluster_4)이 level=1이라 현재 level=0 기반 body_split이 title을 못 찾음. body_split_count=0 → tree alignment 실패 → marker rewrite fallback.

2. **section-aware content 배치** — 현재 모든 generated content가 section0에 들어감. section별 secPr/layout이 보존되어야 하는데 배치 로직이 section-aware하지 않음.

**해결 방안**:

| 항목 | 현재 | 변경 |
|------|------|------|
| body_split boundary | level=0 scan → title_role match | **region-first**: target_unit_plan chapter region paragraph_indices로 boundary 직접 결정 |
| body_split 우선순위 | level=0 scan만 | 1) target_unit_plan region → 2) region first_paragraph + title_role 확인 → 3) chapter_trees 매핑 → 4) level=0 scan fallback |
| section tracking | 부분적 (section_info에 일부) | paragraph별 section_id 추적, remove/append 시 section 유지 |
| content 배치 | section0에 모두 append | section0 generated → section0에 배치. section1~4는 preserve 유지 (13.7a에서는 section0만 분석이므로 실질 변경 없지만, 코드 구조가 section-aware해짐) |

**하지 않는 것**:
- 1a pipeline 변경 (13.7b)
- section1~4 content generation (13.7b)
- source allocation redesign (watch)
- D11 fix (watch, regression만 확인)

**검증 기준**:
- 민원인: tree_available=true, body_split_count>0, section1~4 preserve 유지, Ⅰ~Ⅷ 유지, 실제 출력에서 제목 marker 깨짐 없음
- 조달청: tree_available=true 유지, local_pattern_override 유지, assembly fail=0, 실제 출력 Ⅰ/Ⅱ/Ⅲ 유지
- CC7: shallow route 불변, 실제 출력 양식 유지

#### 13.7b: Multi-Section Analysis 확장 (13.7a 이후 별도 판단)

**해결하는 문제**:

`extract_section_xml()`이 section0만 반환 → 1a~1f가 section0만 분석 → section1~4 구조 미파악 → 생성/수정 불가 → 13.5 preserve safety로 임시 보존 중.

민원인 수치:
- section0: 326 body paragraphs (분석됨)
- section1~4: 284 body paragraphs, 47% (미분석, preserve만)
- section4 "제2장 - 반복민원 대응": 193p — 본문급 content가 분석 대상 밖

**해결 방안**:

| 항목 | 방안 |
|------|------|
| extract_section_xml | 모든 section XML list 반환 |
| section별 1a 분석 | 토큰 전략 필요. section별 content significance 진단 후 analysis depth 동적 결정. 본문성 content가 있는 section(예: section4 "제2장" 193p)은 lightweight로 축소하지 않음. 목표는 토큰 최소화가 아니라 문서 구조 정확 이해 |
| document-level merge | section별 분석 결과 통합. paragraph에 section_id/section_local_idx/global_document_idx 부여 |
| section-aware target_unit_plan | region에 section_span/section_ids 추가. generation target이 원래 section 기억 |
| section-aware generation | 2b 호출 시 target section 정보 전달 |
| cache schema | section 정보 포함으로 확장. 기존 cache invalidation 필요 |

**토큰 비용 추정** (민원인 기준):
- 현재: section0 1.9MB → truncate 100K chars → 1a 1회 호출
- section별 독립 호출: 5회 × 토큰 → 비용 높음
- 하이브리드(유력): section0 full + section1~4 lightweight → 1~2회 추가

**하지 않는 것**: source allocation redesign, table cell filling, emphasis

**검증 기준**:
- 민원인 section1~4 분석 결과가 structure에 포함
- section4 (제2장) content가 generation 대상 가능
- document-level paragraph indexing 일관성
- 조달청 single-section regression 없음
- 14-table 진행 가능 여부 최종 판단

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

| 양식 | 이름 | sections | template chapters | 특이점 |
|------|------|----------|-------------------|--------|
| 1 | 조달청 업무계획 | 1 (single) | 3 (Ⅰ.성과/Ⅱ.여건/Ⅲ.과제) | top-level fixed flow, 내부 과제별 반복 |
| 2 | 민원인 위법행위 대응지침 | 5 (multi) | 8 (Ⅰ~Ⅷ) | multi-section, secPr, attachment 101p, section4 Part2 누락 |
| 3 | CC7 주간보고 | 1 (single) | shallow (non-chapter) | shallow route, section plan seed |

테스트 시 양식 번호에 따라 다른 검증 포인트:
- 양식1: template-driven 3장 유지, per-chapter local_pattern, grammar, marker rewrite, broad source
- 양식2: template-driven 8장 유지, per-chapter local_pattern, multi-section (5 sections, layout diff), attachment preserve (13.5), tree_available=false (title level=1), section4 누락 (CC11)
- 양식3: shallow route 불변, section plan seed 동작, 13.6 변경에 영향 없음

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
| 8 | hwpx_style_profile | 11.2 style x 2~3 batch | X |

캐시 hit 시 1~5 skip → 2a + 2b + style profile 호출.
