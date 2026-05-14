# 13.7 Plan — Chapter-Grouped Assembly + Multi-Section Analysis

작성: 2026-05-13 (rev: 진단 정정 후 재작성)

---

## 1. 단계 구분

| 단계 | 범위 | 핵심 |
|------|------|------|
| **13.7a** | Assembly 수정 (1a 파이프라인 무변경) | `content["chapters"]` 도입 + chapter-grouped + region-aware placement |
| **13.7b** | Analysis 확장 (1a 파이프라인 변경) | 모든 section 분석 + document-level merge + (section, region, chapter) 단위 확장 |

13.7a만으로 "양식 구조가 안정적으로 나오는 첫 단계" 조건을 충족한다. 13.7b는 13.7a 완료 후 진입 시점 별도 판단(범위는 확정).

---

## 2. 진단 정정 (rev)

### 이전 가설 — 폐기

> "민원인 chapter title이 level=1이고 assemble body_split이 level=0 paragraph scan에 의존하므로 실패한다."

**이 가설은 현재 코드와 맞지 않다.** `assemble_hwpx_hybrid()` 의 body_split은 `doc.paragraphs`나 structure paragraphs를 보지 않는다. `content["body"]`(AI 생성 결과)의 title_role 매칭으로 chapter boundary를 자른다. level은 어디서도 참조하지 않는다.

### 실제 fail point (2026-05-13 debug 확인)

| 항목 | 값 |
|------|-----|
| `rewrite_alignment.body_split_count` | 0 |
| `rewrite_alignment.tree_chapter_count` | 8 |
| `rewrite_alignment.chapter_count_match` | false |
| 08 chapters 수 | 8 |
| marker_rewrite_log role_cluster_4 entry | 8 (title이 body_items에 들어옴) |
| marker_rewrite_log `is_chapter_title=true` | 0 (assemble가 title을 인식 못 함) |
| cache `chapter_types.type_1.title_role` | role_cluster_3 |
| chapter_template_plan `local_title_role` (8 chapters 전부) | role_cluster_4 |
| 실제 Ⅰ~Ⅷ paragraph role | role_cluster_4 |

원인: assemble의 `_chapter_title_roles` set은 `structure.chapter_types[*].title_role`만 본다. cache에 저장된 1d 분석이 type_1.title_role = role_cluster_3으로 잘못 잡았고(role_cluster_3은 목차 paragraph), 실제 chapter title role은 role_cluster_4. set이 `{role_cluster_3, role_cluster_19}`로 구성되어 body_items 순회 중 role_cluster_4 title을 단 한 건도 매칭하지 못함 → `body_split_count=0`.

### 구조적 원인

기능적 fail point는 1d 부정확이지만, **구조적 fail point는 그보다 상위**: chapter는 generation unit인데, generation 결과(`process_section_fill_result`가 chapter 단위로 들고 있는 nodes/items)를 `content["body"]`라는 flat list로 평탄화한 뒤, assemble에서 다시 1d title_role을 이용해 chapter boundary를 복원하려는 구조. 평탄화 자체가 정보 손실이고, 1d 한 군데가 부정확하면 복원 실패.

**13.7a는 이 구조적 원인을 해결한다.** 1d 부정확 자체는 별도 stage 후보.

---

## 3. 설계 원칙

1. chapter는 generation unit. 평탄화 → 복원 구조 폐기. chapter 단위로 들고 다님.
2. analysis는 document-level. section을 독립 단위로 분류하지 않음.
3. assembly는 section-aware. generated content를 원래 section에 배치, layout 보존.
4. section role classification 금지. "section0=본문, section1=붙임" 같은 하드코딩 없음.
5. section index/role_cluster 번호 기반 하드코딩 금지.
6. section별 secPr/layout 보존(용지 방향, 크기, 여백, 단).
7. title은 level이 아니라 target_unit_plan / region / chapter object로 식별.
8. 기존 single-section / shallow route regression 금지.
9. **안전장치는 근본 해결이 아님**(원칙 1). 13.7a의 empty chapter region preserve, section1~4 preserve는 임시.

---

## 4. 13.7a: Chapter-Grouped Assembly

### 4.1 범위

1a 파이프라인 변경 없음. assemble + DB tool chapter loop만 수정.

### 4.2 Chapter object schema (확정)

```python
{
  "source_chapter_idx": 0,         # 0..N-1, chapter loop 순서
  "target_region_id": "...",       # target_unit_plan region 식별자
  "section_id": 0,                  # 13.7a 기본값 0, 13.7b에서 실 채워짐
  "first_paragraph_idx": 123,       # placement anchor, = paragraph_indices[0]
  "paragraph_indices": [123, 124, 125],
  "title_item": {"role": "...", "text": "..."},
  "title_node": {"id": 0, "parent_id": None, "role": "...", "text": "..."},
  "body_items": [{"role": "...", "text": "..."}, ...],   # derived view of body_nodes
  "body_nodes": [{"id": 1, "parent_id": 0, "role": "...", "text": "...", ...}, ...],
  "status": "ok" | "empty" | "fail",
  "_debug": {...}                   # empty_reason, fallback_reason 등
}
```

**Schema 결정 사유**:
- `chapter_id` 필드는 의미 미정이라 제거. `source_chapter_idx` + `target_region_id`로 식별.
- `title_item`과 `title_node` **둘 다 유지**. role/text가 중복이지만 node가 superset(id/parent_id). 13.7a 변경 범위 제한 위해 둘 다 유지. ROADMAP에 "13.7c 또는 14에서 derived view로 통합 검토" 명시.
- `title_item`과 `title_node`는 **derived view 관계**. chapter object 생성 코드에 invariant assert 필수:
  - `title_item.role == title_node.role`
  - `title_item.text == title_node.text`
  - `len(body_items) == len(body_nodes)`
  - `body_items[i].role == body_nodes[i].role`, text 동일
- `section_id` 필드는 13.7a에서 정의하고 값은 0 고정. 13.7b schema migration 비용 0이 목적(원칙 1).
- `status="empty"`는 generation 결과 비어있음(items=0). 13.7a에서는 region 전체 preserve 처리.
- `status="fail"`은 region/chapter alignment 실패 또는 invariant 위반. assemble validation fail.

### 4.3 content schema 공존 규칙

| Path | content["chapters"] | content["body"] |
|------|--------------------|-----------------|
| chapter route (DB tool) | 채움 | 채우지 않음 |
| shallow route (DB tool) | 없음 | 채움 |
| files.py dynamic endpoint | 없음 | 채움 |

assemble:
- `content["chapters"]` 있으면 chapter-grouped path. `content["body"]`는 무시.
- 둘 다 있으면 assert fail (이중 transport 금지, 원칙 6).
- 둘 다 없으면 fallback (현재 chapter_trees=None일 때의 path와 동일, files.py legacy 호환).

### 4.4 구현 항목

#### A1. Chapter-grouped 도입 + flat split path 제거

- `process_section_fill_result` 결과를 평탄화 없이 chapter object list로 수집.
- DB tool에서 `content["chapters"] = [chapter_obj, ...]` 전달. `content["body"]` 채우지 않음.
- chapter object 생성 시 invariant assert 헬퍼 호출.
- `assemble_hwpx_hybrid` 분기 추가: `content["chapters"]` 있으면 chapter object 단위로 처리, body_split / `_chapter_title_roles` 매칭 단계 미실행.
- `chapter_trees` 파라미터 제거 (chapter object 안으로 흡수).
- `build_chapter_trees`(`hwpx_analyzer.py:11967`) dead code 삭제 (호출 0건 확인).
- chapter route flat split path(`assemble_hwpx_hybrid` 의 `if chapter_trees:` 분기 1211~1320) 제거.
- shallow route, files.py dynamic endpoint의 chapter_trees=None 경로는 그대로(변경 없음).

#### A2. Region metadata attach

- target_unit_plan region에서 `first_paragraph_idx`, `paragraph_indices`, `target_region_id` 추출.
- chapter loop에서 chapter object에 attach.
- `section_id`는 13.7a에서 0 고정 (section0). 13.7b에서 실 section_id 매칭.

#### A3. Section-aware placement 기반

13.7a 실질 변경 적음(section0만 분석되므로 모든 chapter section_id=0).
**목적**: chapter object가 `section_id`를 들고 있는 schema를 13.7a에 도입해 두면, 13.7b에서 multi-section generation/placement로 확장할 때 schema migration 비용 0.

assemble은 chapter object의 `section_id` 기반으로 paragraph append target section을 결정 (현재는 모두 section0).

#### A4. Empty chapter 정책 (13.7a 한정 안전장치)

- chapter object `status="empty"` (items=0)인 경우: **region 전체 preserve**.
- title도 generated 결과 무시, region paragraph 전부 원본 유지.
- 근거: generated title + original body 혼합은 의미 mismatch 위험.
- "13.7a 한정 안전장치"로 ROADMAP/계획서에 명시. 13.7b에서 empty 원인 (2b LLM fail / source 부족 / grammar reject) 측정 결과에 따라 정책 정밀화.

#### A5. Section1~4 preserve (13.7a 한정 안전장치)

13.5에서 도입된 unanalyzed section preserve를 13.7a에서도 유지. "13.7a 한정 안전장치"로 명시. 13.7b에서 section별 분석 후 generation/update/preserve 재판단.

### 4.5 Debug schema 변경

`rewrite_alignment` 필드는 chapter object path에서 의미 변경:

| 기존 필드 | 새 의미 / 새 이름 |
|----------|------------------|
| `tree_chapter_count` | `chapter_count` (chapter object 수) |
| `body_split_count` | 제거 (split 안 함) |
| `chapter_count_match` | `region_chapter_count_match` (chapter 수 == region 수) |
| `per_chapter[].body_count` | `per_chapter[].body_items_count` |
| `per_chapter[].tree_count` | `per_chapter[].body_nodes_count` |
| `per_chapter[].aligned` | `per_chapter[].body_aligned` |
| (신규) | `per_chapter[].title_aligned` |
| (신규) | `per_chapter[].status` (ok/empty/fail) |
| (신규) | `per_chapter[].section_id` |
| (신규) | `per_chapter[].first_paragraph_idx` |

fallback path (content["chapters"] 없을 때, files.py dynamic endpoint 등)는 기존 debug 필드 그대로 유지(별도 분기).

### 4.6 구현 순서

| # | 작업 | 유형 | 예상 규모 |
|---|------|------|----------|
| 0 | A0 measurement 추가 (debug-only) | measurement | ~30 lines |
| 1 | chapter object schema + invariant assert 헬퍼 | A1 | ~30 lines |
| 2 | DB tool chapter loop에서 chapter object 수집 + `content["chapters"]` 전달 | A1 | ~30 lines |
| 3 | `assemble_hwpx_hybrid` 분기 추가: chapter object path | A1 | ~80 lines |
| 4 | flat split path / `_chapter_title_roles` 분기 / chapter_trees 파라미터 제거 | A1 | -50 lines |
| 5 | `build_chapter_trees` dead code 삭제 | A1 | -30 lines |
| 6 | region metadata attach (A2) | A2 | ~20 lines |
| 7 | section_id 필드 (값 0 고정) | A2 | ~5 lines |
| 8 | empty chapter region preserve (A4) | A1 | ~20 lines |
| 9 | debug schema 필드 변경 | A1 | ~20 lines |
| 10 | 검증 (민원인/조달청/CC7 + files.py dynamic endpoint) | 검증 | - |
| 11 | ROADMAP/handoff/13.6 문서 patch + 13.7b 계획 정정 commit | docs | - |

---

## 5. A0 — 병행 measurement (debug-only)

13.7a-A1 착수를 막지 않는 병행 measurement. 결과는 ROADMAP watch 항목 및 1d-fix stage 판단 자료.

### A0-1. 1d title_role 신뢰도

3개 양식 캐시에서 `chapter_types[*].title_role` vs `chapter_template_plan.seed.chapters[*].local_title_role` 비교.

기록:
- 양식별 mismatch 발생 chapter 수
- type_id ↔ chapter_idx ↔ 1d title_role ↔ local title_role 표
- 실제 paragraph role (handoff 기준) 일치 여부

코드 위치: `hwpx_analyzer.py`에 `measure_title_role_consistency()` 함수 추가. DB tool에서 호출 → `_debug_payload["title_role_consistency"]`.

### A0-2. Empty chapter 원인

chapter loop debug에 chapter별 empty 원인 기록:
- 2b LLM raw response 길이 (0이면 LLM 응답 없음)
- raw_items 길이 (0이면 parse 실패)
- normalized items 길이 (0이면 normalize 단계에서 제거)
- grammar validation reject 수
- pre-validation에서 source allocation에서 받은 source 길이

코드 위치: DB tool chapter loop에서 chapter object `_debug.empty_reason` 필드에 기록.

### A0 결과의 사용처

- 13.7a-A1 설계 영향 없음 (C 방향은 원칙에서 결정).
- 1d mismatch가 다른 양식에도 있으면 1d-fix stage 우선순위 ↑.
- empty 원인이 source 부족이면 13.7b 또는 별도 source allocation stage 후보.
- empty 원인이 LLM fail이면 2b prompt 안정화 stage 후보.

---

## 6. 검증 조건 (13.7a 완료 조건)

### 6.1 Chapter-region alignment

- 민원인: chapter 8 == region 8.
- 조달청: chapter 3 == region 3.
- 각 chapter `target_region_id`가 regions[i].id와 1:1 매칭.

### 6.2 Chapter object alignment

non-empty chapter (`status="ok"`) 전부:
- `title_item ↔ title_node` 1:1 (role/text 일치, invariant assert 통과)
- `len(body_items) == len(body_nodes)`
- `body_items[i].role == body_nodes[i].role`, text 일치

empty chapter (`status="empty"`):
- `status="empty"` 표기 확인
- region 전체 preserve 적용 확인 (region paragraph_indices 전부 header_indices에 포함)

### 6.3 Title 누락/중복

- region.first_paragraph_idx마다 정확히 1개 title paragraph 배치
- production 출력 paragraph 순회 시 동일 idx에 두 개 title 결합 없음

### 6.4 D11 concat (production 출력 텍스트 직접 검사)

- 민원인 production HWPX 결과 열어서 첫 30 paragraph text dump
- 결합 패턴 grep: `제\d+장.*Ⅰ`, `민원인의 위법행위.*목 적`, slot text + title text가 같은 paragraph에 결합
- 발견 시 blocker

### 6.5 Section1~4 fingerprint

분석되지 않은 section은 변동 없어야 함:
- section_count 동일
- 각 section의 secPr 존재 + orientation/page size/margin 동일
- 각 section의 paragraph_count, table_count 동일
- 각 section의 first/last text fingerprint 동일

### 6.6 Route regression

- 조달청: Ⅰ/Ⅱ/Ⅲ 구조 유지, local_pattern_override 유지, assembly fail=0
- CC7: shallow route 불변, `chapter_trees=None` 경로 유지, assembly fail=0
- files.py dynamic endpoint 1회 회귀: chapter_trees 없이 호출되어도 fallback path 동작

### 6.7 Debug field 존재

- `rewrite_alignment.chapter_count`, `region_chapter_count_match` 채워짐
- `per_chapter[]`에 `body_aligned`, `title_aligned`, `status`, `section_id`, `first_paragraph_idx` 채워짐
- chapter object 전부 invariant assert 통과 (assert fail 0건)
- `_debug_payload.title_role_consistency` 채워짐 (A0)

---

## 7. 13.7b: Multi-Section Analysis 확장 (13.7a 이후)

### 7.1 범위

1a 파이프라인 변경. 모든 section 분석.

### 7.2 구현 항목

#### B1. extract_section_xml → 모든 section 반환

현재 `section_names[0]`만 → 전체 section XML list. 호출자(analyze_hwpx, DB tool)도 multi-section 수용.

#### B2. Section별 1a 분석

토큰 관리 전략 — section analysis depth 원칙(아래) 준수.

#### B3. Document-level structure merge

**가장 위험한 부분**(원칙 1, 6).
- section-local `parent_idx` → document-global `parent_idx` 변환 (offset 누적, role_cluster 번호 reconcile)
- cross-section parent 관계 사전 진단
- section별 role_cluster fingerprint가 layout/style 차이로 흔들릴 가능성 → role clustering을 document-level에서 다시 할지, section-local cluster를 그대로 두고 merge할지 결정
- section4 "제2장"이 document chapter / attachment 내부 chapter / nested unit 중 무엇인지 1차 진단(13.6-A multi_section_diagnostic 결과 활용)

#### B4. Section-aware target_unit_plan

- region 표현을 `(section_id, section_local_idx_range)` + `global_idx_range` 둘 다 보존
- cross-section region 감지 시 validation fail
- 13.7a의 chapter object `section_id` 필드에 실 값 채워짐

#### B5. Section-aware generation/placement

- 2b 호출 시 target section 정보 전달
- chapter object 단위로 generation/placement (13.7a에서 schema 완성, 13.7b에서 multi-section으로 확장)
- section1~4 preserve가 "13.7a 한정 안전장치"에서 "분석 기반 판단"으로 전환

#### B6. Cache schema migration

- cache_schema_version 올림 + 기존 3개 양식 cache invalidate
- migration 안 함 (양식 수 적음)

#### B7. Source diagnostic schema 확장 (allocation watch evidence)

- chapter 단위 → (section, chapter) 단위로 source coverage 분해
- "source 있음 + generated 비어있음/엉뚱함" 패턴 카운트
- 이 카운트로 source allocation blocker 승격 여부 판단

### 7.3 Section analysis depth 원칙

1. lightweight analysis는 후보 전략이지 확정 아님. 토큰 비용을 이유로 section1~4 자동 축소 분석 X.
2. section별 content significance 진단 (heading density, body paragraph count, table presence, layout difference, generation target 가능성). 13.6-A diagnostic 활용.
3. significance 높거나 generation 대상 가능성 있는 section은 full/deeper analysis. 민원인 section4(193p, "제2장")처럼 본문성 content는 lightweight 금지.
4. lightweight 결과가 section heading/target region/본문 구조를 안정적으로 복원 못 하면 사용 X.
5. 13.7b 목표는 토큰 비용 최소화가 아니라 multi-section 문서 구조를 정확히 이해하는 것.
6. section3 빈 페이지처럼 content 없는 section은 lightweight 충분. 기준은 section index가 아니라 관측된 content significance.
7. section별 독립 full 1a 호출(A)이 정확도 baseline. 비용 최적화(B, C)는 구조 손실 없을 때만.
8. lightweight도 최소 기준 충족: heading tree, paragraph role/level 후보, table presence, target region 가능성, hierarchy signal. 단순 문단 수/text preview만으로 generation target 판단 X.

### 7.4 검증 기준 (13.7b)

- 민원인 section1~4 내용이 분석되고 target_unit_plan에 포함
- section4 (제2장) 내용이 generation 대상이 될 수 있음
- document-level paragraph indexing 일관성
- chapter object section_id에 실 값 채워짐
- 조달청 single-section regression 없음
- cache schema 호환 (invalidation 후 재생성)

### 7.5 완료 조건 (13.7b)

1. multi-section 분석 결과가 document-level structure에 통합
2. section-aware target_unit_plan 동작
3. 13.7a의 chapter object section_id에 실 값
4. section1~4 preserve가 "한정 안전장치"에서 "분석 기반 판단"으로 전환
5. template table의 section/region 위치 추적 가능한지 확인 → 14-table 착수 판단
6. source allocation watch evidence (B7) 유지

---

## 8. 하지 않을 것

| 항목 | 이유 |
|------|------|
| `_chapter_title_roles` union (chapter_template_plan.local_title_role 추가) | 임시 땜질, 두 source of truth 공존 (원칙 1, 6 위배) |
| flat content["body"]를 유지한 채 region paragraph idx로 split 복원 | 잃어버린 chapter boundary 복원 시도, 책임 분리 위배 |
| cache의 chapter_types.title_role 직접 patch | cache invalidation 후 다른 양식 재발 |
| 1d 자체 수정 | A0 measurement 전에는 안 함 |
| section1~4 generated placement (13.7a) | 1a section0만 분석 — 13.7b 범위 |
| source-to-template allocation redesign (13.7) | watch — evidence 부족 |
| chapter title adaptation (source에 맞게 제목 변경) 정책화 | 15 이후 또는 별도 stage |
| 강조표시 / run-level style / emphasis 적용 | later |
| table cell filling | 14-table |
| table 내용 source 검증 | 15 |
| D11 dual-use title/slot concat fix | watch — 13.7a 검증에서 regression만 확인 |
| section role classification | 금지 (설계 원칙 4) |
| section index / role_cluster 번호 하드코딩 | 금지 (설계 원칙 5) |
| 1a pipeline 변경 (13.7a) | 13.7b 범위 |
| chapter_id 필드 도입 | 의미 미정, source_chapter_idx + target_region_id로 충분 |
| title_item/title_node 둘 중 한쪽 제거 (13.7a) | 13.7a 변경 범위 제한. 통합은 별도 stage |
| chapter route flat split path 잔존 (deprecated fallback 등) | 잔존시킬 정당 이유 없음, dead path 잔존 금지 |
| chapter object schema에 ad-hoc 필드 추가 | schema 명시 외 필드 금지 |
| 안전장치를 근본 해결로 표기 | 원칙 1 — empty preserve, section1~4 preserve는 13.7a 한정 |
| 한 양식만 통과시키고 13.7a 완료 처리 | 원칙 14 — 3개 양식 전부 검증 통과 필수 |
| Production HWPX 본문에 "source 부족" 메모 삽입 | 원칙 25 — debug/report로만 |

---

## 9. 원칙 준수 확인

| 원칙 | 13.7a | 13.7b |
|------|-------|-------|
| 1. 임시 땜질 X, 최종 구조 우선 | O — flat 평탄화→복원 구조 자체 제거 | O |
| 2. 하드코딩 X | O — section index/role_cluster 번호 미사용 | O |
| 3. Template-first | O — chapter region이 placement target | O |
| 4. Chapter-local pattern preservation | O — chapter를 generation unit으로 보존 | O |
| 5. Multi-section section-aware | O — schema 도입(section_id), 13.7b에서 실값 | O — 실 분석 |
| 6. 책임 분리 | O — generation/assemble이 chapter 단위로 일관 | O |
| 7. 로그/검증 가능성 | O — rewrite_alignment schema 확장 + invariant assert | O |
| 8. blocker/watch/later 구분 | O — D11/source allocation은 watch | O |
| 14. 근거 있는 일반화 | O — 3개 양식 전부 검증 후 완료 | O |
| 16. 측정 후 구현 | O — A0 measurement 병행 | O |
| 25. Production/debug 구분 | O — empty/preserve는 debug에 기록, 본문에 메모 X | O |
| 26. Route별 검증 경로 존중 | O — shallow/files.py legacy 무영향 + 회귀 검증 | O |
| 27. 명시적 완료 조건 | O — 6장 검증 항목 명시 | O |

---

## 10. 인수인계 / 문서 patch 항목

13.7a 착수 전 patch 대상:

- `handoff_2026_05_13_for_13_7a.md`: "근본 원인: title=level=1" 단락 통째로 재작성 (이 계획서의 §2로 대체)
- `handoff_2026_05_13_stage13_6.md`: line 52, 81, 89의 "title=level=1" 가설 정정 + watch 추가 (assemble title boundary dependency mismatch)
- `ROADMAP.md`: 13.7a 설명 (region-first body_split → chapter-grouped + region-aware placement), 13.7 옛 stage detail (source allocation redesign)는 deprecated 표기
- `MEMORY.md`: 13.7a 새 정의 한 줄 추가

---

## 11. DB tool 변경 절차 주의

- DB tool은 DB에 저장, 런타임 exec. 웹 UI 도구 편집기를 열면 변경 날아갈 위험.
- 13.7a DB tool 변경: chapter loop에서 chapter object 수집 + `content["chapters"]` 전달 + `_debug_payload["title_role_consistency"]` 추가.
- 절차: `/app/backend`에서 python3 스크립트로 직접 DB update. 웹 UI 편집기 열지 말 것.
- 캐시 삭제 불필요 (13.7a는 assemble + DB tool chapter loop 변경, cache 이후 단계).

---

## 12. 검증 실행 절차

- 사용자가 직접 웹에서 양식 실행 (claude는 사용자 웹 UI 로그인 토큰 없음).
- claude는 debug 파일 분석:
  - `/tmp/hwpx_debug/10_assemble_result.json` → rewrite_alignment, chapter object alignment
  - `/tmp/hwpx_debug/08_2b_generation_by_chapter.json` → chapter object empty 원인
  - `/tmp/hwpx_debug_last.json` → title_role_consistency, multi_section_diagnostic
  - production HWPX → D11 concat, section1~4 fingerprint
- 검증 순서: 민원인 → 조달청 → CC7 → files.py dynamic endpoint 1회 회귀.

---

작성: 2026-05-13 (rev)
