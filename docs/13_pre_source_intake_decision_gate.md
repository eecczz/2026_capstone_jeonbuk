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

### source와 양식의 관계

- source는 "양식에 채울 외부 내용"이며, 양식(template) 자체와 독립적
- 같은 source를 서로 다른 양식에 적용할 수 있음 (예: 같은 PDF를 조달청/CC7/민원인 양식에 각각 적용)
- CC7 전용 source가 없어도 blocker 아님 — 조달청 test source를 CC7에 적용하여 검증 가능
- source가 특정 양식 형식과 맞지 않으면 generator가 재구성/요약해야 함

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

### 13단계 minimal schema (dict/TypedDict)

```python
# 13.0 최소 구현: plain dict 또는 TypedDict
class SourceBlock(TypedDict):
    source_block_id: str        # unique id (e.g. "sb_000", "sb_001")
    content: str                # actual text content
    order_index: int            # position within source document
    heading_path: list[str]     # e.g. ["Ⅰ. 추진배경", "1. 현황"], 없으면 []
```

### 14단계 확장 후보 (13에서는 구현하지 않음)

```python
# 14에서 필요 시 추가
source_doc_id: str            # file_id or note_id
source_doc_title: str         # file name or note title
block_type: str               # title | paragraph | bullet_list | table | unknown
parent_block_id: Optional[str]  # hierarchical parent
metadata: dict                # {page, section, extraction_method, ...}
confidence: float             # 1.0 for markdown, lower for OCR
char_offset_start: Optional[int]  # 15단계 source_refs용
char_offset_end: Optional[int]
```

### 설계 근거

- **13에서는 4개 필드만**: `source_block_id`, `content`, `order_index`, `heading_path`
- 나머지는 14(KB 연동, multi-file)에서 정식 @dataclass로 확장
- plain dict 사용 → import/dependency 최소, 테스트 용이
- allocation 로직은 이 4개만으로 동작 (heading_path로 매칭, order_index로 position 비교)

### source_blocks의 역할

- **어댑터 출력 형태를 통일**: PDF든 마크다운이든 source_blocks로 변환 후 사용
- **generation pipeline은 source_blocks만 알면 됨**: 원본 형태에 무관
- **allocation/evidence 추적 가능**: source_block_id로 "이 생성 결과는 어떤 source에서 왔는지" 기록

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

- 13.1에서는 **debug-first allocation**으로 시작
- region별로 source_blocks를 매핑하되, 확정이 아닌 후보 + 근거 형태
- allocation의 가치는 unit_type과 region 구성에 따라 다름 (아래 참조)

### allocation 가치의 차이 (region 구성별)

**chapter-dominant 양식 (조달청, 민원인):**
- heading match / position 기반 allocation이 핵심 가치 제공
- source의 "Ⅰ. 배경" → template의 chapter 1 region 매핑이 실질적 분배 기능

**shallow-dominant 양식 (CC7):**
- shallow_block region이 하나 크면 source→region allocation은 trivial (source 전체 → 하나의 region)
- 이 경우 allocation은 공통 interface/debug layer로서의 의미가 더 큼
- shallow generator에는 source 전체 또는 broad source block을 주는 것이 자연스러움
- source_blocks는 generation control이 아니라 evidence/debug 역할

### allocation_result schema (minimal dict)

```python
# 13.1 최소 구현
allocation_result = {
    "region_id": str,              # target_unit_plan의 region id
    "unit_type": str,              # slot | shallow_block | chapter | attachment
    "allocated_block_ids": list,   # source_block_ids
    "allocation_reason": str,      # "heading_match" | "position" | "broad_assign" | "ai_decision"
    "confidence": float,           # 0.0~1.0
    "ambiguity_flags": list,       # ["multiple_candidates", "low_confidence", "no_match"]
}
```

### allocation 방식 (13.1 최소)

1. **heading_path 매칭**: source_block의 heading_path와 region의 description/title 비교
2. **position 기반**: source 순서와 template region 순서의 correspondence
3. **broad assign**: single-region 양식이면 source 전체를 해당 region에 할당 (trivial case)
4. **AI allocation** (option): 1+2+3이 ambiguous할 때만 AI 호출. 13.1b에서 선택적 추가.

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
    "content": "2024년 기준 전북 지역 인구는...",
    "heading_path": ["Ⅰ. 추진배경", "1. 현황"],
    "order_index": 3
}
```

### 매칭 기준 (우선순위)

1. **heading text 일치**: region description과 source heading_path의 유사도
2. **순서 correspondence**: region 순서 = source block 순서 (같은 문서 구조를 따를 때)
3. **broad assign**: region이 1개뿐이면 source 전체 할당 (trivial)
4. **user intent**: 사용자 메시지에서 추출한 의도 ("사과에 대해" → 사과 관련 block 우선)
5. **AI 판단**: 위 기준으로 ambiguous할 때 AI가 최종 결정

### expected output shape (region별)

| unit_type | generation에 필요한 source 형태 |
|-----------|-------------------------------|
| slot | 값 1개 (날짜, 기관명 등). source에서 추출 또는 사용자 메시지에서 추출 |
| shallow_block | source 전체 또는 broad block. template 구조에 맞게 재구성/요약 |
| chapter | 충분한 분량의 content. source_blocks 여러 개 |
| attachment | skip 또는 원본 보존. source 불필요 |

---

## 9. 13단계 실행 순서와 구현 범위

### 실행 순서와 작업 성격

| 순서 | sub-stage | 성격 | 핵심 |
|------|-----------|------|------|
| 1 | 13.0-design | 설계 (코드 아님) | shallow generator input/output contract |
| 2 | 13.0-impl | 기반/debug-only | source_blocks adapter (최소 변환) |
| 3 | 13.1 | 기반/debug-only | allocation debug (region↔source 매핑) |
| 4 | **13.3** | **creative/new generation** | **shallow_block generator (핵심 신규 작업)** |
| 5 | 13.2/13.4 | 검증/연결 확인 | slot/chapter 기존 path regression check |
| 6 | 13.5 | skip/watch | attachment/table preserve only |
| 7 | 13.6 | 후보 (조건부) | multi-section distribution decision gate |

### 13-pre: Source Intake Decision Gate (이 문서)

- source contract 정의
- source_blocks schema 확정
- adapter 경계 결정
- routing 정책 명시
- fallback chain 구체화
- shallow generator contract 방향 제시
- **구현 아님, 결정만**

### 13.0-design: shallow generator input/output contract

**13.0 adapter 구현 전에 소비자(shallow generator)의 contract을 먼저 정의한다.**

adapter를 만들고 나서 "shallow generator가 이 형태를 원하지 않았다"가 나오면 rework이므로, consumer-first 설계.

**input contract:**
- target_unit_plan의 shallow_block region (paragraphs, description, boundary_evidence)
- 해당 region의 template paragraph samples/shape (role, marker, level 정보)
- source text 전체 또는 broad source_blocks
- user instruction / source summary (available이면)

**output contract:**
- content-only body items (marker 없음, 12.1 marker/content 분리 원칙 유지)
- flat/shallow paragraph list (2b tree의 id/parent_id 구조보다 단순)
- template region의 paragraph shape를 참고하되, 정확한 paragraph 수는 hard requirement 아님
- 각 item: `{role, content}` 최소. id/parent_id는 shallow에서 불필요할 수 있음 (설계 시 결정)

**assemble contract:**
- 기존 marker reattach 흐름 재사용 (generate_expected_marker_normalized → reattach)
- shallow_block region 안에서만 replacement/append 발생
- region 외부 paragraphs는 건드리지 않음

**구현 아님 — 문서/주석/schema 수준으로 정리만.**

### 13.0-impl: source blob → source_blocks adapter (debug-only)

- 현재 source (PDF text blob) → source_blocks 변환
- heading regex split이 되면 split, flat text면 single broad block
- **generation output 변경 금지** — debug file 추가만
- **edge case handling, heading detection 고도화, allocation 개선 금지**
- required fields: `source_block_id`, `content`, `order_index`, `heading_path`
- output: `16_source_blocks.json`
- cache invalidation 불필요 (source_blocks는 cache 이후 단계, generation 무관)

### 13.1: target_unit_plan + source_blocks → allocation debug

- region별 source_block 할당 (heading match + position + broad assign)
- allocation_result debug output (`17_source_allocation.json`)
- coverage/overlap/orphan validation
- **code-only allocation은 debug proposal이지 production hard decision이 아님**
  - low_confidence / no_match / multiple_candidates → fallback (unit_type별, section 11 참조)
  - high-confidence allocation → 13.3에서 generation input으로 사용
- shallow single-region에서는 broad assignment가 자연스러움 (trivial case)
- AI allocation은 13.1b에서 선택적 추가 (ambiguity 관측 후 필요성 판단)
- **source imbalance evidence**: 조달청 source chunks [154, 219, 40360]처럼 극심한 불균형이 있으면 allocation debug에 기록. 13.0에서 고치지 않고, 13.1에서 관측만.

### 13.3: shallow_block generator (핵심 신규 작업)

- **13단계의 실제 creative/new generation work**
- 13.0-design에서 정의한 contract에 따라 구현
- source 전체 또는 broad source block을 input으로 받음
- content-only + marker reattach 그대로 사용
- **대표 검증 양식: CC7**
  - CC7 전용 source가 필요하지는 않음 — 실행 시 주어진 source를 shallow_block 형태로 재구성
  - 조달청 test source를 CC7에 적용하여 검증 가능
  - source가 CC7 형식과 너무 안 맞으면 synthetic/minimal fixture 준비
- **성공 기준**: CC7이 legacy chapter 2a로 억지 분해되지 않고, shallow_block region 기반 정상 생성
- **13단계 완료 판단은 CC7 shallow generation 확인 전에 하지 않음**

### 13.2/13.4: slot/chapter — 검증/연결 확인

**성격: 개발이 아니라 regression 확인. 기존 path가 깨지지 않았는지 검증.**

- 13.2 slot: 기존 header_data (6.6) 로직이 그대로 동작하는지 확인. 새 코드 없이 기존 path 유지.
- 13.4 chapter: 기존 `2a → split_source_by_chapters → 2b` path가 그대로 동작하는지 확인.
- 실제 코드 변경이 필요하면 그때 scope 정의. 단순 확인이면 13.3 completion criteria에 포함.

### 13.5: attachment/table — preserve/skip/watch

- attachment region: 원본 보존 (assemble에서 건드리지 않음)
- table region: 현재 관측만 (observable failure 없으면 watch)
- **table cell filling은 13에서 하지 않음** — 별도 14-table 단계로 분리 (section 18 참조)
- **placeholder만, 구현 없음**

### 13.6 후보: multi-section distribution decision gate

**조건부 — 아래 조건 확인 시만 구현 범위에 진입:**

- 민원인 output에서 "content가 section[0]에 몰려 최종 문서가 사용 불가" 수준의 failure가 확인됨
- 13.3 완료 후, CC7 (추정 single-section)은 정상이지만 민원인 (5 sections)에서 layout이 깨짐

**그때까지는:**
- watch 유지
- 13.0~13.3 진행 후 민원인 output의 section/layout 상태를 관측
- observable failure가 watch 수준이면 13.6은 실행하지 않음

### split_source_by_chapters와의 공존 정책

**13.0~13.3에서 기존 chapter path를 건드리지 않는다.**

```
현재 (유지됨):
  조달청/민원인 → 2a → split_source_by_chapters → 2b → assemble

13.0~13.3 추가 (병렬, debug + shallow path):
  source text → source_blocks (debug) → allocation (debug)
  CC7 shallow → shallow generator (new) → assemble
```

- `split_source_by_chapters`는 13.0~13.3 기간 동안 chapter path에서 계속 사용
- source_blocks는 debug/관측 및 shallow path 전용
- **13.4 시점에서 판단**: chapter path를 source_blocks allocation으로 대체할지, 기존 split_source_by_chapters를 유지할지
- 두 시스템이 같은 결과를 내는지 비교는 allocation debug에서 관측 (강제 전환 아님)

---

## 10. Routing 정책

### routing은 필요하다

- region 구성에 따라 다른 generator를 호출하는 것은 routing이며, 이를 금지하는 것이 아님
- shallow_block region에는 shallow generator, chapter region에는 2b generator — 이건 정상적인 route selection

### 금지하는 것

- `derived_mode_label` 기반 hard switch (단일 string으로 전체 분기)
- 특정 문서명/기관명/role_cluster 번호 기반 분기
- 특정 paragraph index 기반 분기
- 단일 heuristic (`paragraph_count > N` 같은) 기반 hard decision

### route planner가 보는 입력

route planner는 다음을 **함께** 보고 region별 route를 결정한다:
1. `target_unit_plan.regions` — region 목록과 각 unit_type
2. allocation confidence — source가 해당 region에 충분히 할당되었는가
3. source sufficiency — 할당된 source의 분량이 generation에 충분한가
4. ambiguity_flags — allocation에서 불확실성이 있었는가

### route selection 결과는 evidence를 남긴다

```python
route_decision = {
    "region_id": str,
    "selected_route": str,       # "shallow_generator" | "chapter_2b" | "slot_direct" | "preserve" | "fallback_*"
    "decision_inputs": {
        "unit_type": str,
        "allocation_confidence": float,
        "source_sufficiency": str,   # "sufficient" | "insufficient" | "trivial"
        "ambiguity_flags": list,
    },
    "fallback_used": bool,
    "fallback_reason": Optional[str],
}
```

---

## 11. Fallback 정책 (unit_type별)

### chapter fallback

| 조건 | 동작 |
|------|------|
| allocation high-confidence + source sufficient | chapter 2b generation (정상) |
| allocation low-confidence | legacy chapter path (기존 2a/2b 그대로) |
| source insufficient | legacy chapter path with warning |

- chapter의 fallback = **legacy chapter path** (기존 2a/2b). 이미 검증된 경로이므로 안전.

### shallow_block fallback

| 조건 | 동작 |
|------|------|
| source sufficient (broad assign 포함) | shallow generator (정상) |
| source insufficient but exists | shallow generator with broad source (available 전체 사용) |
| source completely missing | debug stop + watch (generation 건너뜀) |

- shallow_block의 fallback은 **broad source로 shallow generator 재시도** 또는 **debug stop**
- **shallow_block을 legacy chapter 2a로 보내는 것은 기본 fallback이 아님**
  - 이는 명시적 blocker 상황에서만 temporary escape로 허용
  - 허용 조건: shallow generator 자체가 구현 전이거나 치명적 실패 시
  - route_decision에 `"fallback_reason": "shallow_generator_unavailable"` 기록 필수

### slot fallback

| 조건 | 동작 |
|------|------|
| source에서 값 추출 성공 | 추출 값 사용 |
| user_input(채팅 메시지)에서 추출 가능 | user_input 사용 |
| header_data(6.6)에서 매핑 존재 | header_data 사용 |
| 위 전부 실패 | template 원본 보존 (default) |

- slot의 fallback chain: source → user_input → header_data → default(원본 보존)
- 모든 단계에서 실패해도 최소 원본 보존으로 문서가 깨지지 않음

### attachment fallback

| 조건 | 동작 |
|------|------|
| 기본 | 원본 보존 (preserve) |
| source에서 대체 내용이 명시적으로 제공됨 | 대체 적용 (14 이후) |

- attachment의 fallback = **항상 preserve/skip**. allocation 실패가 blocker 아님.

---

## 12. 13에서 하지 말아야 할 것

- Open Notebook/KB full implementation (14단계)
- source block editor UI (14단계)
- table cell filling (별도 14-table 단계)
- source coverage validation full implementation (15단계)
- internal AI transition (16단계)
- marker rewrite retirement (Phase 3, 13 완료 후 별도 decision gate)
- RAG 기반 파일 식별 구현 (embedding이 꺼져 있으므로 14에서)
- 14/15 일을 13에 끌어오기
- shallow_block을 legacy chapter 2a로 보내는 것을 기본 routing으로 삼기
- split_source_by_chapters 대체 (13.4 판단 전까지 기존 path 유지)
- source imbalance 교정 (13.0 adapter에서 고치지 않음, 13.1에서 관측만)
- multi-section distribution (13.6 조건부 후보, 즉시 구현 아님)

---

## 13. 원칙

1. **code는 후보/evidence/proposal을 만들고, AI가 schema 안에서 판단한다.**
   - source_blocks 생성 = code (마크다운 파싱, heading split)
   - source→region allocation = code 후보 + AI 최종 판단 (ambiguous 시)

2. **code heuristic hard decision 금지.**
   - heading이 일치하면 높은 confidence로 할당하되, 불일치 = 할당 안 함이 아님
   - ambiguity_flags로 남기고 AI에게 위임

3. **특정 문서명/기관명/role_cluster 기반 분기 금지.**

4. **routing은 region 목록 기반이며 단일 label이 아니다.**
   - `derived_mode_label`, 문서명, role_cluster, 특정 index, 단일 heuristic 기반 hard switch 금지
   - route planner는 `target_unit_plan.regions`, unit_type, confidence, source sufficiency, ambiguity_flags를 함께 보고 결정
   - region 목록 기반 routing은 허용하되, 결정에 evidence/confidence/ambiguity를 남긴다

5. **target_unit_plan도 hard switch가 아니라 evidence/plan으로 취급한다.**
   - plan이 "chapter x3"라고 해서 반드시 3개를 만드는 것이 아님
   - source가 부족하면 2개만 될 수 있고, 그건 validation에서 잡음
   - route planner가 confidence/sufficiency를 종합 판단하며, 단일 필드로 분기하지 않음

6. **validation은 structural check 중심.**
   - coverage / overlap / invalid index / source block missing / size mismatch
   - "내용이 좋은가"는 validation 범위 밖 (AI 품질은 별도)

7. **ambiguous한 것은 hard rule로 닫지 말고 ambiguity_flags/watch로 남긴다.**

8. **consumer-first 설계: 소비자 contract을 먼저 정의하고, supplier를 그에 맞춰 만든다.**

---

## 14. Blocker / Watch / Later 기준

| 항목 | 판정 | 근거 |
|------|------|------|
| source contract 부재 | **resolved** | 이 문서가 해소안, 리뷰 합의로 closed |
| CC7 실행 시 source text 정상 전달 | **13.3 전 확인** | source 전달 경로 동작 확인 (PDF skip 영향) |
| RAG chunk-only generation | **watch** | 현재 사용 안 함. 14에서 이 방향 가지 않도록 기록 |
| Open Notebook full 구현 | **later (14)** | 13은 adapter interface만 |
| 기존 PDF/raw text adapter | **13.0 범위** | 현재 source → source_blocks 변환 |
| embedding 꺼져 있음 | **watch** | 13은 사용자 직접 파일 선택으로 우회 |
| table cell filling | **later (14-table)** | 13.5에서 preserve/skip만. 별도 단계 |
| source coverage validation | **later (15)** | 13에서는 allocation debug만 |
| multi-file source (파일 2개+) | **watch** | schema는 multi-doc ready, 13은 single-doc first |
| test source flat text 가능성 | **watch** | flat이면 broad single block. adapter를 과하게 만들지 않음 |
| PDF extraction skip 영향 | **watch** | 기존 PDF는 content 있음. 새 PDF 시 empty 가능 |
| source imbalance [154, 219, 40360] | **watch → 13.1 evidence** | 13.0에서 고치지 않고 allocation debug에서 관측 |
| multi-section distribution (민원인) | **watch → 13.6 조건부** | observable failure 확인 시만 승격 |
| marker rewrite retirement | **later (Phase 3)** | 13 완료 후 별도 decision gate |

---

## 15. 반박/대안 섹션

### 반박 1: source_blocks schema가 과하다

**주장**: 현재 source는 항상 PDF 1개 → text blob 1개. source_blocks로 분해하는 게 오버엔지니어링 아닌가?

**검토**:
- 맞는 부분: 13.0~13.4를 source_blocks 없이 구현하는 것도 가능. 기존 text blob + heading split만으로도 충분.
- 하지만: 14에서 KB 연동 시 multi-file source가 오면, text blob 가정이 깨짐. 이때 source_blocks가 없으면 allocation 로직을 다시 짜야 함.
- **결론**: 13에서는 minimal dict (4 fields)로 시작. 14에서 정식 @dataclass로 확장. heading split 결과를 dict에 넣는 것뿐이므로 과하지 않다.

### 반박 2: 13-pre를 생략해도 된다

**주장**: source contract는 "text blob"으로 이미 합의됐으니, adapter 없이 바로 13.1로 가면 안 되나?

**검토**:
- 맞는 부분: 단일 파일 + text blob이면 adapter가 identity function일 수 있음.
- 하지만: 13.1에서 source→region allocation을 만들 때, "source의 단위가 뭔가"를 모르면 allocation 대상을 정할 수 없음.
- **결론**: source_blocks = "heading 기반으로 나눈 text chunk"라는 최소 정의만 있으면 13.1이 깔끔하게 시작됨.

### 반박 3: RAG를 파일 식별에만 쓰는 전제가 맞나?

**검토**: 파일 전문 사용이 문서 생성에 적합하다는 판단 유지. RAG chunk는 파일 식별용으로만.

### 반박 4: 더 단순한 구조가 가능한가?

**검토**: region별 다른 strategy가 필요하다는 점은 변하지 않음. 13.0에서는 minimal dict (4 fields)로 시작.

### 반박 5: allocation을 code로만 하고 AI를 안 쓰면?

**검토**: 13.1은 code-only allocation으로 시작. ambiguity가 높으면 13.1b에서 AI allocation 추가. 원칙(측정 후 구현)에 부합.

### 반박 6: shallow_block을 그냥 chapter 2a/2b로 보내면 안 되나?

**검토**: shallow_block을 chapter 2a로 보내는 것은 기본 fallback이 아님. 13.3의 존재 이유가 바로 이 차이를 해소하는 것. 임시 escape로만 허용.

---

## 16. 핵심 성공 기준

### 13단계 전체

- target_unit_plan이 production generation flow에 연결됨
- region별로 적합한 generator가 호출됨 (routing 동작)
- allocation + route_decision + generation 결과가 debug에 추적 가능

### 13.3 (핵심 신규 작업)

- **CC7이 legacy chapter 2a로 억지 분해되지 않는다**
- **target_unit_plan의 shallow_block region을 기반으로 shallow generator가 정상 호출된다**
- 주어진 source를 CC7 shallow_block 형태로 재구성/요약한 content가 생성된다
- content-only + marker reattach가 shallow context에서도 정상 동작한다
- 생성 결과가 template의 19-paragraph region에 적합한 분량/구조를 가진다
- **13단계 완료 판단은 CC7 shallow generation 확인 전에 하지 않음**

### 13단계 완료 최소 quality gate

- output HWPX가 template 대비 실제 text diff를 가짐 (빈 문서 아님)
- generated content가 XML에 정상 삽입됨 (mark_dirty 이후 serialize에 반영)
- 조달청/민원인 기존 path regression 없음 (assemble success, grammar pass 유지)
- CC7 shallow output이 사람이 볼 때 최소 사용 가능 (빈 공간/깨진 구조 없음)
- debug success와 실제 output XML diff가 일치 (debug는 성공인데 output이 비어있는 case 없음)

### 검증 방법

- CC7 template + 조달청 test source (또는 적합한 source) 조합으로 실행
- route_decision에서 `selected_route: "shallow_generator"` 확인
- 조달청/민원인 template은 기존 source로 실행 → regression 없는지 확인
- fallback이 아닌 정상 경로로 실행됐는지 debug log에서 확인

---

## 17. 13.0 진입 전 cheap check

13.0-design 시작 전 확인할 최소 관측:

1. **test source 구조**: 기존 test PDF의 추출 텍스트가 heading 구조인지 flat text인지
   - heading 있으면: heading split adapter 구현
   - flat text면: broad single block. adapter를 과하게 만들지 않음
   - **어느 쪽이든 blocker 아님** — 대응 방식이 다를 뿐
   - **결과**: heading 3개 있음, exact match 성공, chunk_lengths [154, 219, 40360]
2. **CC7 source 전달 경로**: CC7 실행 시 source text가 비어 있지 않은지 (PDF skip 영향)
   - **결과**: 기존 업로드된 파일 사용 시 문제 없음 (content already in DB)
3. **existing allocation**: 기존 split_source_by_chapters의 output과 source_blocks 비교 가능한지
   - **결과**: 07b_source_split_decision에서 title/position/chunk_length 확인 가능

---

## 18. 단계 경계 정리

| 단계 | 범위 | 산출물 |
|------|------|--------|
| **13-pre** | source contract 결정 (이 문서) | 결정 문서, schema, routing/fallback 정책 |
| **13** | source_blocks adapter + allocation debug + shallow generator + regression check | target_unit_plan 기반 generation 동작 (CC7 shallow 포함) |
| **14** | KB/Open Notebook source intake | RAG 파일 식별 → 전문 source 획득 경로 |
| **14-table** | table contract + template-side table filling MVP | 표 셀 채우기 (14와 별도 scope) |
| **15** | source evidence / coverage validation | 생성 결과 ↔ source 추적, 누락/환각 검출 |
| **Phase 3** | marker rewrite retirement | content-only reattach 안정 확인 후 safety net 제거 |
| **16** | internal AI transition | 외부→내부 AI 전환 |

### 14와 14-table 분리 근거

- 14 본작업: KB에서 RAG로 관련 파일을 식별하고 전문 source를 가져오는 경로. source input 문제.
- 14-table: template 표 셀을 source 내용으로 채우는 generation 문제. source input과 독립.
- 같은 단계에 넣으면 scope 과부하. 별도 분리하여 각각 clear한 완료 기준 유지.
- 14-table은 14 완료 후 또는 14와 병렬 가능. 순서 강제 아님.

### Phase 3 (marker rewrite retirement) 진입 조건

13 완료 후 별도 decision gate. 조건:
- content-only reattach가 3+ 양식에서 conflict=0, residual=0 유지
- rewrite가 actual correction을 수행하는 빈도가 0에 수렴
- star_depth 재현 이슈 없음 또는 debug 가능
- 실제 output XML에서 marker가 정상인지 diff 확인

---

## 19. 요약: 13단계 진입 조건

다음이 확인되면 13.0-design 시작:

1. [x] source contract 제안: 파일 전문 1~N개 텍스트, source_blocks로 변환
2. [x] adapter 경계 제안: 13에서는 PDF/text → source_blocks adapter만. KB adapter는 14.
3. [x] target_unit_plan이 production에 미연결 상태임을 인지
4. [x] region별 generation strategy 차이 인지 (slot ≠ shallow ≠ chapter)
5. [x] schema는 multi-doc ready, 13 구현은 single-doc first
6. [x] routing 정책 명시 (region 목록 기반, 단일 label 금지)
7. [x] fallback chain 구체화 (unit_type별, shallow→chapter 2a fallback 제한)
8. [x] 핵심 성공 기준 정의 (CC7 shallow_block 정상 생성)
9. [x] split_source_by_chapters 공존 정책 명시 (기존 path 유지)
10. [x] shallow generator consumer-first 설계 방향 제시
11. [x] cheap check 완료 (source 구조 heading 확인, PDF skip watch)
12. [ ] **이 문서 리뷰 합의 (open blocker)**

---

최종 수정: 2026-05-11
