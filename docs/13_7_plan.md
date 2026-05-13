# 13.7 Plan — Multi-Section Assembly + Analysis Expansion

## 성격

13.7은 두 단계로 나눈다.

| 단계 | 범위 | 핵심 |
|------|------|------|
| **13.7a** | Assembly 수정 (1a 파이프라인 무변경) | title body_split 수정 + section-aware 조립 |
| **13.7b** | Analysis 확장 (1a 파이프라인 변경) | 모든 section 분석 + document-level merge |

13.7a만으로 "양식 구조가 안정적으로 나오는 첫 단계" 조건을 충족한다.
13.7b는 13.7a 완료 후 별도 판단.

---

## 현재 문제

### 문제 1: tree_available=false (민원인)

민원인 title(role_cluster_4)이 level=1이다. 현재 assemble의 body_split은 level=0 paragraph를 스캔하여 chapter title을 찾는다.

```
조달청: title idx=4, level=0, parent=None → body_split 성공 → tree_available=true
민원인: title idx=4, level=1, parent=3   → body_split 실패 → tree_available=false
```

tree_available=false의 결과:
- marker rewrite가 tree alignment 대신 fallback scan 사용
- sibling_group_key가 `fallback_role_cluster_*` 형태
- chapter_title_entries=0
- rewrite_alignment.fully_agreed=false (또는 null)

### 문제 2: section-aware assembly 부재

assemble이 모든 generated content를 section0에 몰아넣는다. multi-section 양식에서:
- section0의 content는 section0에 배치되어야 함
- section4의 content는 section4에 배치되어야 함
- 각 section의 secPr/layout (용지 방향, 여백 등)이 보존되어야 함

현재 13.5 preserve safety가 unanalyzed section paragraph를 보존하고 있으므로 **삭제는 안 됨**. 하지만 generated content 배치가 section-aware하지 않음.

### 문제 3: section0-only analysis (13.7b 범위)

`extract_section_xml()`이 section0만 반환. 1a~1f가 section0만 분석. section1~4의 구조, role, parent, level이 파악되지 않음.

13.5 preserve safety로 보존 중이지만, section1~4 내용을 생성/수정할 수 없음.

이 문제는 13.7a에서는 건드리지 않는다. 13.7b에서 해결.

---

## 설계 원칙

1. **analysis는 document-level로 통합** — section을 독립 단위로 분류하지 않음
2. **assembly는 section-aware** — generated content를 원래 section에 배치, layout 보존
3. **section role classification 금지** — "section0=본문, section1=붙임" 같은 하드코딩 없음
4. **section index, role_cluster 번호 기반 하드코딩 금지**
5. **section별 secPr/layout 보존** — 용지 방향, 크기, 여백, 단
6. **title은 level이 아니라 target_unit_plan/tree/title_role 기준으로 식별**
7. **기존 single-section 양식 regression 금지** — 조달청, CC7

---

## 13.7a: Assembly 수정

### 범위

1a 파이프라인 변경 없이 assemble 쪽만 수정.

### 구현 항목

#### A1. Region-first body_split

**현재**: level=0 paragraph를 스캔하여 title_role 매칭 → chapter boundary 결정.

**변경**: target_unit_plan의 chapter region을 body_split의 primary boundary로 사용.

```
현재: doc.paragraphs → level=0 scan → title_role match → split
변경: target_unit_plan regions → region boundary 직접 사용 → tree alignment
```

body_split boundary 결정 우선순위:
1. **target_unit_plan chapter region** — paragraph_indices / first_paragraph_idx로 boundary 결정. 이것이 원본 문서의 구조 분석 결과이므로 가장 신뢰할 수 있음.
2. **region first paragraph + title_role** — region 시작 paragraph가 title_role과 일치하는지 확인하여 boundary 검증.
3. **generated chapter_trees와 region 매핑** — 생성 결과 tree를 region boundary에 매핑. tree는 생성 결과의 내부 구조이지 배치 기준이 아니므로, region boundary가 먼저 결정된 후 tree를 mapping하는 순서.
4. **기존 level=0 title scan** — 최후 fallback. target_unit_plan 없는 경로에서만 사용.

이 변경으로:
- 민원인 title=level=1 → region boundary로 직접 결정 → body_split 성공 → tree_available=true
- 조달청 title=level=0 → region-first이므로 기존과 동일 결과 (regression 없음)

#### A2. Section-aware paragraph tracking

assemble에서 각 paragraph가 어느 section에 속하는지 추적.

python-hwpx의 `doc.paragraphs`는 모든 section을 순회한다. 현재 section_info에 section별 정보가 이미 부분적으로 있음 (9.0~9.2에서 추가).

확장:
- paragraph remove/append 시 section 정보 유지
- generated content를 append할 때 올바른 section에 배치
- section별 remove count, append count 추적

#### A3. Section-aware content placement

현재 모든 generated body_items가 하나의 body_split으로 section0에 들어감.

변경:
- generated content를 원래 section에 배치하는 것이 최종 원칙
- **13.7a에서는**: 1a가 section0만 분석하므로 generated target은 사실상 section0 전부. section0 generated content를 section0에 안정적으로 배치
- **section1~4**: 13.5 preserve safety로 원본 유지. section4 content generation은 13.7b 이후
- section별 secPr 보존 확인

**13.7a의 실질 변경**: 현재와 동일하게 section0에만 generated content가 들어가지만, 코드 구조가 section-aware해짐. 13.7b에서 multi-section generation으로 확장할 때 section 배치 로직을 다시 만들지 않아도 됨.

### 검증 기준 (13.7a)

| 양식 | 확인 항목 |
|------|----------|
| 민원인 | tree_available=true (또는 tree-first split 성공) |
| 민원인 | body_split_count > 0 |
| 민원인 | marker rewrite에서 fallback_role_cluster_* 감소/제거 |
| 민원인 | section1~4 preserve 유지 (삭제 없음) |
| 민원인 | section별 secPr/layout 보존 |
| 민원인 | Ⅰ~Ⅷ chapter 유지 |
| 민원인 | D11 악화 없음 |
| 조달청 | tree_available=true 유지, assembly fail=0, local_pattern_override 유지 |
| CC7 | shallow route 불변 |

### 완료 조건 (13.7a)

1. 민원인 tree_available=true (또는 tree alignment 성공)
2. 3개 양식 assembly fail=0
3. 3개 양식 regression 없음
4. section-aware assembly 기반 코드 구조 확립 (13.7b 확장 가능)
5. **실제 출력 눈검증**:
   - 민원인: Ⅰ~Ⅷ 제목 marker 중복/깨짐 없음, section1~4 원본 preserve 유지
   - 조달청: Ⅰ/Ⅱ/Ⅲ 구조 유지
   - CC7: shallow 양식 유지

---

## 13.7b: Analysis 확장 (13.7a 이후 별도 판단)

### 범위

1a 파이프라인 변경. 모든 section 분석.

### 구현 항목 (예정)

#### B1. extract_section_xml → 모든 section 반환

현재 `section_names[0]`만 → 전체 section XML list 반환.

호출자(analyze_hwpx, DB tool)도 multi-section 수용하도록 변경.

#### B2. Section별 1a 분석

토큰 관리 전략 필요:

| section | 크기 (민원인) | 전략 |
|---------|-------------|------|
| section0 | 1.9MB | 기존 truncate_xml (100K chars) |
| section1 | 260KB | 축소 truncate 또는 별도 budget |
| section2 | 250KB | 동일 |
| section3 | 3.5KB | 전체 분석 가능 |
| section4 | 425KB | 축소 truncate |

선택지:
- A: section별 독립 1a 호출 (토큰 × section 수, 비용 높음)
- B: 전체 section merge 후 단일 1a 호출 (토큰 budget 내 truncation 필요)
- C: section0 full 분석 + 나머지 section lightweight 분석 (하이브리드)

**C가 유력**: section0는 기존 full analysis, section1~4는 paragraph count + role hint + heading 추출 정도의 lightweight 분석. 13.7b 설계 시 결정.

#### B3. Document-level structure merge

section별 분석 결과를 하나의 document-level structure로 통합.

- 각 paragraph에 section_id, section_local_idx, global_document_idx 부여
- role/level/parent_idx가 section 간 일관성 유지
- cache schema 확장 (section 정보 포함)

#### B4. Section-aware target_unit_plan

- region에 section_span 또는 section_ids 추가
- cross-section region 감지 (있다면)
- generation target이 원래 section 기억

#### B5. Section-aware generation

- 2b 호출 시 target section 정보 전달
- section별 source allocation (필요 시)

### 검증 기준 (13.7b)

- 민원인 section1~4 내용이 분석되고 target_unit_plan에 포함
- section4 (제2장) 내용이 generation 대상이 될 수 있음
- document-level paragraph indexing 일관성
- 조달청 single-section regression 없음
- cache schema 호환 (기존 cache invalidation 또는 migration)

### 완료 조건 (13.7b)

- multi-section 분석 결과가 document-level structure에 통합
- section-aware target_unit_plan 동작
- 13.7a의 section-aware assembly와 연동
- 14-table 진행 가능 여부 최종 판단

---

## 하지 않을 것 (전체)

| 항목 | 이유 |
|------|------|
| source-to-template allocation redesign | watch — content mismatch evidence 부족 |
| chapter title adaptation (source에 맞게 제목 변경) | 15 이후 / 별도 단계 |
| 강조표시 / run-level style / emphasis 적용 | later |
| table cell filling | 14-table |
| table 내용 source 검증 | 15 |
| D11 dual-use title/slot concat fix | watch — regression만 확인 |
| section role classification | 금지 (설계 원칙) |
| section index / role_cluster 번호 하드코딩 | 금지 (설계 원칙) |
| 1a pipeline 변경 (13.7a) | 13.7b 범위 |

---

## 구현 순서 (13.7a)

| # | 작업 | 유형 | 예상 규모 |
|---|------|------|----------|
| 1 | assemble body_split을 region-first 기반으로 수정 | A1 | ~60 lines |
| 2 | paragraph → section mapping 추적 | A2 | ~30 lines |
| 3 | generated content section-aware 배치 | A3 | ~20 lines |
| 4 | 민원인 검증: tree_available, section preserve, layout | 검증 | - |
| 5 | 조달청 검증: regression 없음 | 검증 | - |
| 6 | CC7 검증: shallow 불변 | 검증 | - |
| 7 | ROADMAP 업데이트 | - | - |

---

## 원칙 준수 확인

| 원칙 | 13.7a | 13.7b |
|------|-------|-------|
| 하드코딩 금지 | O — level/index 기준 아닌 region-first 기반 | O |
| 책임 분리 | O — assembly만 변경, analysis 무변경 | O — analysis/assembly 분리 유지 |
| section role classification 금지 | O — section 역할 판정 안 함 | O |
| debug/검증 가능성 | O — tree_available, section_info 기록 | O |
| 기존 route regression 금지 | O — single-section/shallow 불변 | O |

---

작성: 2026-05-13
