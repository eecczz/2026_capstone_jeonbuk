# 13-pre: Source Intake Decision Gate

13단계 진입 전, source가 생성기에 어떤 형태로 들어와야 하는지 최소 contract를 정리한다.

---

## 1. 현재 상태 요약

| 단계 | 산출물 | 상태 |
|------|--------|------|
| 12.0 | template_unit_observation | done, cached, v0.2 |
| 12.1 | content-only generation + marker reattach | done, production 적용 |
| 12.2 | target_unit_plan (paragraph-level region planning) | done, cached, v0.1 |

### target_unit_plan 현재 위치

- **아직 production route에 직접 쓰이지 않는 debug contract**
- cache에 저장되어 있지만, generation flow는 여전히 기존 2a/2b path로 실행
- region 정보 (slot / shallow_block / chapter / attachment)가 있지만, source allocation이나 generation strategy 분기에 사용되지 않음
- 즉, "계획은 있으나 실행에 연결되지 않은 상태"

### 3개 양식의 target_unit_plan 결과

| 양식 | regions |
|------|---------|
| 조달청 | slot(3p) + shallow_block(1p, 목차) + chapter x3 |
| CC7 | slot(2p) + shallow_block(19p) + attachment(2p) |
| 민원인 | slot(4p) + chapter x8 + attachment(101p) |

---

## 2. 13-pre가 필요한 이유

### 문제

13단계가 slot/shallow/chapter routing을 실제 generation에 연결하려면, **"source가 어떤 형태로 오는가"**가 먼저 정해져야 한다.

source contract 없이 route selection/generation을 구현하면:
- raw source를 generator에게 통째로 던지고 "알아서 해라" 구조가 됨
- 14(KB 연동) 시 source 형태가 바뀌면 13 전체를 rework해야 함
- source allocation이 generation 로직에 하드코딩될 위험

### 원하는 결과

- 13에서 만드는 source→region allocation이 **source 형태에 무관**하게 동작
- 기존 PDF/raw text 직접 업로드도, 향후 KB/Note 연동도 같은 contract으로 감쌈
- 14에서는 "contract을 채우는 새 adapter"만 추가하면 됨

---

## 3. Source 형태 구분

현재 시스템에서 source가 생성기에 도달하는 경로:

| source 타입 | 저장 형태 | 채팅 첨부 시 전달 | 비고 |
|------------|-----------|-----------------|------|
| **Note** | `data.content.md` (마크다운 1장) | 전문 한 덩어리 | TipTap 에디터, 협업 가능 |
| **File** | 업로드된 파일 (PDF, DOCX 등) | 추출된 텍스트 전문 | 현재 HWPX 도구의 source |
| **Knowledge Base** | 파일 컬렉션 (N개) | RAG → 관련 chunk만 | 벡터 검색 기반 |

### 현재 HWPX 도구의 source 입력

```
사용자가 PDF 파일을 채팅에 첨부
  → retrieval layer가 텍스트 추출
  → 도구에 text blob 전달
  → split_source_by_chapters로 chapter별 분배
  → 2b generation
```

source는 항상 **하나의 text blob**.

---

## 4. RAG chunk vs 관련 파일 전문 비교

### RAG chunk만 generation source로 쓰는 경우

**장점:**
- 이미 구현됨 (vector search infra 존재)
- 관련성 높은 내용만 추출 — 토큰 절약
- 불필요한 내용 자동 제거

**단점:**
- 단편적 — 300~500자 조각, 맥락 끊김
- 순서 보장 없음 — 원본에서의 위치/흐름 소실
- 양 부족 — chunk 5~10개로 업무계획 10페이지를 못 채움
- 표/이미지 등 구조적 요소 파편화
- similarity threshold에 따라 누락 발생 (recall 불완전)
- **문서 생성 ≠ Q&A**: Q&A는 정답 1~2문장이면 되지만, 문서 생성은 원문의 흐름/구조/분량이 필요

### 관련 파일 전문을 source로 쓰는 경우

**장점:**
- 맥락 보존 — 원본의 논리 흐름 유지
- 분량 충분 — 문서 생성에 필요한 양 확보
- 구조 활용 가능 — 마크다운 헤딩, 목차 등으로 split point 식별
- recall 100% — 관련 파일 내의 모든 정보 사용 가능
- 결정론적 — vector search의 randomness 없음

**단점:**
- 토큰 과다 — 파일이 크면 LLM context 초과 가능
- 불필요한 내용 포함 — generator가 자체 filtering 필요
- 파일 식별이 선행되어야 함 — "어떤 파일이 관련 있나" 판단 필요

### 추천 기본 방향

**RAG는 파일 식별(file selection)에 사용하고, 실제 generation source는 선택된 파일의 전문을 사용한다.**

### 반박 가능성

1. "파일이 100페이지면 어쩌나?" → 파일 내부 1차 filtering AI를 추가할 수 있음 (strategy D). 하지만 현재 양식의 source는 대부분 5~30페이지 수준이므로 즉시 문제는 아님.
2. "RAG chunk로도 충분한 경우가 있지 않나?" → slot region(날짜/기관명)은 chunk 1개로 충분할 수 있음. 하지만 chapter region은 불가. region별로 다른 전략을 쓰면 복잡도가 높아지므로, 기본은 전문 사용으로 통일하고 토큰 초과 시에만 filtering을 적용하는 게 단순.
3. "현재 서버에서 embedding이 꺼져 있는데?" → 맞음. `bypass_embedding_and_retrieval = true` 상태. RAG 기반 파일 식별은 14단계에서 embedding 복구 후 사용. 13에서는 기존처럼 사용자 직접 파일 지정 flow 유지.

---

## 5. 최소 source_blocks schema 제안

```python
@dataclass
class SourceBlock:
    source_block_id: str          # unique id within this generation session
    source_doc_id: str            # file_id or note_id (origin document)
    source_doc_title: str         # file name or note title
    block_type: str               # title | paragraph | bullet_list | table
                                  # | note | image | unknown
    title: Optional[str]          # heading text (if block_type == title)
    content: str                  # actual text content of this block
    order_index: int              # position within source document
    parent_block_id: Optional[str]  # hierarchical parent (heading→subheading)
    heading_path: list[str]       # e.g. ["Ⅰ. 추진배경", "1. 현황"]
    metadata: dict                # {file_id, page, section, extraction_method, ...}
    extraction_method: str        # "markdown_parse" | "pdf_text" | "manual"
    confidence: float             # 1.0 for markdown parse, lower for OCR/heuristic

    # optional (15단계 source_refs용 예약)
    char_offset_start: Optional[int]
    char_offset_end: Optional[int]
```

### source_blocks의 역할

- **어댑터 출력 형태를 통일**: PDF든 마크다운이든 source_blocks로 변환 후 사용
- **generation pipeline은 source_blocks만 알면 됨**: 원본 형태에 무관
- **allocation/evidence 추적 가능**: source_block_id로 "이 생성 결과는 어떤 source에서 왔는지" 기록

### schema가 과한가?

- `parent_block_id`, `heading_path`: 마크다운은 자연스럽게 채워짐, PDF flat text는 null/빈값으로 둬도 됨
- `confidence`, `extraction_method`: 13에서는 항상 1.0/"markdown_parse"일 수 있음. 하지만 14에서 PDF/OCR source 추가 시 필요해지므로 자리만 만들어 둠
- **13 최소 구현에서는 content, order_index, heading_path만 실제 사용**. 나머지는 metadata로 채워두되 allocation 로직에는 불참.

---

## 6. Source Retrieval/Selection Strategy

| 전략 | 설명 | 장점 | 단점 | 13에서? |
|------|------|------|------|---------|
| **A. 사용자 직접 파일 선택** | 현재 flow (PDF 업로드) | 단순, 확실 | 사용자 수작업 | **기본값** |
| **B. RAG로 관련 파일 식별 → 전문** | KB에서 chunk 검색 → 출처 파일 선택 → 전문 획득 | 자동화, 맥락 보존 | embedding 필요 | 14에서 |
| **C. RAG chunk만 사용** | 검색된 chunk를 직접 source로 | 이미 구현됨 | 단편적, 문서 생성 부적합 | 사용 안 함 |
| **D. RAG 파일 식별 + 파일 내부 filtering AI** | B + "파일 내에서 관련 부분만 추출" | 토큰 절약, 정확 | 2단 AI 호출, 복잡 | 14 이후 검토 |
| **E. 사용자가 note/file/section 지정** | UI에서 특정 블록 선택 | 가장 정확, 의도 명확 | UI 개발 필요 | 14에서 |

### 추천 기본값

13단계: **A (사용자 직접 파일 선택)** — 현재와 동일. source_blocks adapter만 추가.
14단계: **B (RAG 파일 식별 → 전문)** 기본, E (사용자 지정) 보조.
15단계 이후: D (내부 filtering) 필요성 판단.

---

## 7. source→region allocation 설계

### 원칙

- 13.0에서는 **debug-first allocation**으로 시작
- hard routing 금지 — allocation은 proposal이며 validation으로 관측
- region별로 source_blocks를 매핑하되, 확정이 아닌 후보 + 근거 형태

### allocation_result schema

```python
@dataclass
class RegionAllocation:
    region_id: str                    # target_unit_plan의 region id
    unit_type: str                    # slot | shallow_block | chapter | attachment
    allocated_blocks: list[str]       # source_block_ids
    allocation_reason: str            # "heading_match" | "content_similarity" | "position" | "ai_decision"
    confidence: float                 # 0.0~1.0
    ambiguity_flags: list[str]        # ["multiple_candidates", "low_confidence", "no_match"]
    coverage_ratio: float             # allocated content / total source (비율)
```

### allocation 방식 (13.0 최소)

1. **heading_path 매칭**: source_block의 heading_path와 region의 description/title 비교
2. **position 기반**: source 순서와 template region 순서의 correspondence
3. **AI allocation** (option): 1+2가 ambiguous할 때만 AI 호출

### validation checks (unit_type별 차등)

**chapter / shallow_block:**
- **source sufficiency**: 해당 region에 할당된 source_blocks의 총 content 분량이 generation에 충분한가
- **coverage**: 최소 1개 source_block 할당됐는가
- **overlap**: 같은 source_block이 2개+ region에 중복 할당됐는가

**slot:**
- **value source 존재 여부**: source_block / user_input(채팅 메시지) / default(template 원본) / header_data(6.6) 중 하나 이상에서 값을 확보할 수 있는가
- source allocation required가 아닐 수 있음 (사용자 메시지에서 직접 추출 가능)

**attachment:**
- **원본 보존이면 source allocation required 아님**
- allocation 실패 = blocker가 아님 (skip/보존이 정상 동작)

**공통:**
- **orphan**: 어떤 region에도 할당되지 않은 source_block이 있는가 (warning, blocker 아님)
- **size mismatch**: slot region에 대량 content가 할당된 경우 (warning)

---

## 8. target_unit_plan과 source_blocks 연결 기준

### target_unit_plan이 제공하는 정보 (per region)

```python
{
    "region_id": "region_0",
    "unit_type": "chapter",
    "paragraphs": [5, 6, 7, 8, ...],     # template paragraph indices
    "description": "Ⅰ. 추진배경 및 목적",  # from template
    "boundary_evidence": {...}
}
```

### source_blocks이 제공하는 정보 (per block)

```python
{
    "source_block_id": "sb_003",
    "block_type": "paragraph",
    "heading_path": ["Ⅰ. 추진배경", "1. 현황"],
    "content": "2024년 기준 전북 지역 인구는..."
}
```

### 매칭 기준 (우선순위)

1. **heading text 일치**: region description과 source heading_path의 유사도
2. **순서 correspondence**: region 순서 = source block 순서 (같은 문서 구조를 따를 때)
3. **user intent**: 사용자 메시지에서 추출한 의도 ("사과에 대해" → 사과 관련 block 우선)
4. **AI 판단**: 위 기준으로 ambiguous할 때 AI가 최종 결정

### expected output shape (region별)

| unit_type | generation에 필요한 source 형태 |
|-----------|-------------------------------|
| slot | 값 1개 (날짜, 기관명 등). source에서 추출 또는 사용자 메시지에서 추출 |
| shallow_block | 짧은 text list (목차, 요약 등). source의 structure/headings로 생성 가능 |
| chapter | 충분한 분량의 content. source_blocks 여러 개 |
| attachment | skip 또는 원본 보존. source 불필요 |

---

## 9. 13단계 최소 구현 범위

### 13-pre: Source Intake Decision Gate (이 문서)

- source contract 정의
- source_blocks schema 확정
- adapter 경계 결정
- **구현 아님, 결정만**

### 13.0: source blob → source_blocks adapter

- 현재 source (PDF text blob) → source_blocks 변환
- 마크다운 source → heading 기반 block 분리
- PDF text → paragraph 단위 block 분리 (heading 없으면 flat)
- **최소 구현**: heading split + order_index + content 채우기

### 13.1: target_unit_plan + source_blocks → allocation debug

- region별 source_block 할당 (heading match + position 기반)
- allocation_result debug output (16_source_allocation.json 또는 유사)
- coverage/overlap/orphan validation
- **code-only allocation은 debug proposal이지 production hard decision이 아님**
  - low_confidence / no_match / multiple_candidates가 있으면 해당 region은 바로 generation route에 쓰지 않고 blocker/watch로 남김
  - high-confidence allocation만 generation에 전달, 나머지는 fallback (legacy path 또는 skip)
- AI allocation은 13.1b에서 선택적으로 추가 (ambiguity 관측 후 필요성 판단)

### 13.2: slot region — direct mapping 최소 버전

- slot region의 paragraphs에 대응하는 값을 source에서 추출
- header_data (6.6에서 이미 구현)와 동일 패턴 확장
- **기존 header slot 로직 재사용 가능**

### 13.3: shallow_block region — flat generation 최소 버전

- 2b보다 단순한 prompt: "이 source를 이 template 구조에 맞게 flat list로 정리해라"
- CC7 양식에서 검증 (현재 chapter_generation으로 우회되는 양식)
- content-only + marker reattach 그대로 사용

### 13.4: chapter region — 기존 path 재사용

- 기존 2a/2b generation path를 chapter region에 그대로 연결
- source_blocks 중 해당 region에 할당된 것만 추려서 2b source로 전달
- **새 구현 최소화** — allocation만 하고 generation은 기존 코드

### 13.5: attachment/table — skip/watch

- attachment region: 원본 보존 (assemble에서 건드리지 않음)
- table region: 현재 관측만 (observable failure 없으면 watch)
- **placeholder만, 구현 없음**

---

## 10. 13에서 하지 말아야 할 것

- Open Notebook/KB full implementation (14단계)
- source block editor UI (14단계)
- table cell filling full implementation (14 이후)
- source coverage validation full implementation (15단계)
- internal AI transition (16단계)
- marker rewrite retirement (Phase 3, 별도 timing)
- RAG 기반 파일 식별 구현 (embedding이 꺼져 있으므로 14에서)
- 14/15 일을 13에 끌어오기

---

## 11. 원칙

1. **code는 후보/evidence/proposal을 만들고, AI가 schema 안에서 판단한다.**
   - source_blocks 생성 = code (마크다운 파싱, heading split)
   - source→region allocation = code 후보 + AI 최종 판단 (ambiguous 시)

2. **code heuristic hard decision 금지.**
   - heading이 일치하면 높은 confidence로 할당하되, 불일치 = 할당 안 함이 아님
   - ambiguity_flags로 남기고 AI에게 위임

3. **특정 문서명/기관명/role_cluster 기반 분기 금지.**

4. **derived_mode_label 기반 hard routing 금지.**
   - target_unit_plan은 route selection의 **강한 입력값**이지만 hard switch가 아님
   - route planner는 unit_type / source allocation confidence / ambiguity_flags / source sufficiency를 함께 보고 결정
   - 13 최소 구현에서는: high-confidence chapter region에 대해 legacy chapter path를 **우선 후보**로 둔다 (확정이 아님)

5. **target_unit_plan도 hard switch가 아니라 evidence/plan으로 취급한다.**
   - plan이 "chapter x3"라고 해서 반드시 3개를 만드는 것이 아님
   - source가 부족하면 2개만 될 수 있고, 그건 validation에서 잡음
   - route planner가 confidence/sufficiency를 종합 판단하며, 단일 필드로 분기하지 않음

6. **validation은 structural check 중심.**
   - coverage / overlap / invalid index / source block missing / size mismatch
   - "내용이 좋은가"는 validation 범위 밖 (AI 품질은 별도)

7. **ambiguous한 것은 hard rule로 닫지 말고 ambiguity_flags/watch로 남긴다.**

---

## 12. Blocker / Watch / Later 기준

| 항목 | 판정 | 근거 |
|------|------|------|
| source contract 부재로 13 route 잘못 설계될 위험 | **open blocker → 이 문서가 해소안, 리뷰 합의 전까지 open** | contract 없이 가면 14에서 rework |
| RAG chunk-only generation | **watch** | 현재 사용 안 하므로 실해 없음. 단, 14에서 이 방향 가지 않도록 기록 |
| Open Notebook full 구현 | **later (14)** | 13은 adapter interface만 |
| 기존 PDF/raw text adapter | **13.0 범위** | 현재 source를 source_blocks로 변환하는 최소 adapter |
| embedding 꺼져 있음 | **watch** | 13에서는 사용자 직접 파일 선택으로 우회. 14 진입 시 embedding 복구 필요 |
| table region generation | **later (14+)** | 13.5에서 placeholder만 |
| source coverage validation | **later (15)** | 13에서는 allocation debug만 |
| multi-file source (파일 2개+) | **watch** | schema는 multi-doc ready, 13 구현은 single-doc first. multi-file input은 watch/debug로 남김 |

---

## 13. 반박/대안 섹션

### 반박 1: source_blocks schema가 과하다

**주장**: 현재 source는 항상 PDF 1개 → text blob 1개. source_blocks로 분해하는 게 오버엔지니어링 아닌가?

**검토**:
- 맞는 부분: 13.0~13.4를 source_blocks 없이 구현하는 것도 가능. 기존 text blob + heading split만으로도 충분.
- 하지만: 14에서 KB 연동 시 multi-file source가 오면, text blob 가정이 깨짐. 이때 source_blocks가 없으면 allocation 로직을 다시 짜야 함.
- **결론**: source_blocks는 "interface만 정의하고, 13.0 adapter는 minimal"로 가면 과하지 않다. heading split 결과를 source_blocks dataclass에 넣는 것뿐.

### 반박 2: 13-pre를 생략해도 된다

**주장**: source contract는 "text blob"으로 이미 합의됐으니, adapter 없이 바로 13.1로 가면 안 되나?

**검토**:
- 맞는 부분: 단일 파일 + text blob이면 adapter가 identity function일 수 있음.
- 하지만: 13.1에서 source→region allocation을 만들 때, "source의 단위가 뭔가"를 모르면 allocation 대상을 정할 수 없음. text blob 전체? 문단? heading 섹션?
- **결론**: source_blocks = "heading 기반으로 나눈 text chunk"라는 최소 정의만 있으면 13.1이 깔끔하게 시작됨. 13-pre는 이 정의를 내리는 것이고, 구현량은 거의 없다.

### 반박 3: RAG를 파일 식별에만 쓰는 전제가 맞나?

**주장**: RAG chunk를 직접 source로 써도, 충분한 top-k (20~50개)를 가져오면 분량 문제는 해결되지 않나?

**검토**:
- chunk 50개 x 300자 = 15,000자. 분량은 어느 정도 확보됨.
- 하지만: chunk 50개의 순서가 뒤섞이고, 같은 문단이 중복 chunk로 나올 수 있으며, heading 구조가 소실됨.
- 문서 생성에서 "흐름"은 단순 내용보다 중요함. "배경 → 현황 → 목표 → 추진방향"의 논리 순서가 chunk에서는 보장 안 됨.
- **결론**: 파일 전문 사용이 문서 생성에 적합하다는 판단 유지. RAG chunk는 "어떤 파일이 관련 있나" 판별에만 사용.

### 반박 4: 더 단순한 구조가 가능한가?

**주장**: source_blocks, allocation, confidence... 이거 다 빼고, 그냥 "text blob → 2a split → 2b generation"을 region 단위로 반복하면 안 되나?

**검토**:
- 가장 단순한 접근. region마다 기존 2a/2b를 돌리면 코드 변경 최소.
- 하지만:
  - slot region에 2a/2b는 과함 (날짜 하나 넣는 데 AI 호출 2번?)
  - shallow_block에 2b tree generation은 과함
  - chapter에만 적합
- **결론**: region별 다른 strategy가 필요하다는 점은 변하지 않음. 다만 source_blocks schema의 복잡도는 줄일 수 있음. **13.0에서는 `{block_id, content, heading_path, order_index}` 4개 필드만으로 시작하고, 나머지는 14에서 확장해도 됨.**

### 반박 5: allocation을 code로만 하고 AI를 안 쓰면?

**주장**: heading match + position만으로 allocation하고, AI를 빼면 latency도 줄고 단순해지지 않나?

**검토**:
- 현재 양식에서는 heading match만으로 충분할 가능성 높음 (3개 양식 모두 source heading이 template heading과 유사).
- AI 없이 시작하고, allocation quality가 낮으면 그때 AI를 추가하는 게 맞음.
- **결론**: 13.1은 code-only allocation으로 시작. ambiguity가 높으면 13.1b에서 AI allocation 추가. 원칙(측정 후 구현)에 부합.

---

## 14. 단계 경계 정리

| 단계 | 범위 | 산출물 |
|------|------|--------|
| **13-pre** | source contract 결정 (이 문서) | 결정 문서, schema 정의 |
| **13** | source_blocks adapter + allocation debug + region별 generation 최소 연결 | production에서 target_unit_plan 기반 generation 동작 |
| **14** | KB 연동 (RAG 파일 식별 → 전문), source block editor, table contract | KB/Note에서 source를 가져오는 경로 |
| **15** | source evidence / coverage validation | 생성 결과 ↔ source 추적, 누락/환각 검출 |

---

## 15. 요약: 13단계 진입 조건

다음이 확인되면 13.0 구현 시작:

1. [x] source contract 제안: 파일 전문 1~N개 텍스트, source_blocks로 변환
2. [x] adapter 경계 제안: 13에서는 PDF/text → source_blocks adapter만. KB adapter는 14.
3. [x] target_unit_plan이 production에 미연결 상태임을 인지
4. [x] region별 generation strategy 차이 인지 (slot ≠ shallow ≠ chapter)
5. [x] schema는 multi-doc ready, 13 구현은 single-doc first
6. [ ] **이 문서 리뷰 후 진행 합의 (open blocker)**

---

최종 수정: 2026-05-11
