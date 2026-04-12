# 전북도청 대도민 공개 챗봇 — 운영 셋업 & 검증 가이드

이 문서는 Docker 환경에서 서버를 띄운 뒤, Phase 0~1(공개 챗봇 + 크롤러)을 검증하는 전체 절차입니다.

---

## 0. 전제 조건

- `entrypoint.sh` 로 Docker 컨테이너가 기동되어 있음
- PostgreSQL + Redis + FastAPI 서버가 `localhost:8080`에서 동작
- 관리자 계정으로 로그인 가능 (기본: `sprinter@mail.go.kr` / `sprint26!`)
- 환경변수 `WEB_LOADER_ENGINE=playwright`, `STT_ENGINE` 원하는 값 설정
- OpenAI API 키 설정됨 (GPT-4o-mini 사용)

---

## 1. 서버 기동 후 초기 확인

### 1-1. 마이그레이션이 자동 적용됐는지

서버 로그에서 다음 줄을 찾는다:
```
INFO [alembic.runtime.migration] Running upgrade b2c3d4e5f6a7 -> e7f8a9b0c1d2, Add crawled_page table for Jeonbuk homepage crawler
```

DB 직접 확인:
```bash
psql -U admin -d customui -c "\dt crawled_page"
psql -U admin -d customui -c "\d crawled_page"
```

`crawled_page` 테이블이 존재하고 컬럼 17개(url, site_code, institution, category, ...)가 있어야 함.

### 1-2. 스케줄러 시작됐는지

로그에서 다음 줄 확인:
```
INFO Crawler scheduler started: daily@2:00 Asia/Seoul, weekly_full=True
```

안 보이면:
- `CRAWLER_ENABLED=True` 환경변수 확인
- lifespan 에러 로그 확인

### 1-3. 라우터 등록 확인

```bash
curl -s http://localhost:8080/api/v1/public/health | jq
```

예상 응답:
```json
{
  "enabled": true,
  "model_id": "jeonbuk-public-chatbot",
  "knowledge_id": null,
  "stt_engine": "",
  "tts_engine": "",
  "rate_limit_per_minute": 10
}
```

### 1-4. 프론트엔드 확인

브라우저: `http://localhost:8080/static/public-chatbot.html`

- 헤더, 힌트 버튼, 입력창, 마이크 버튼이 모두 보이는지
- 브라우저 콘솔에 404 등 에러 없는지

---

## 2. Phase 0 검증 — 텍스트 챗봇 (KB 없이)

이 단계에선 Knowledge Base가 아직 없어서 RAG는 동작 안 함. **"기본 LLM 호출이 통하는지"** 만 확인.

### 2-1. curl 테스트

```bash
curl -s -X POST http://localhost:8080/api/v1/public/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"안녕하세요"}' | jq
```

예상 응답:
```json
{
  "reply": "안녕하세요! 전북특별자치도청 대도민 AI 안내원입니다. ...",
  "session_id": "uuid-...",
  "sources": [],
  "model": "gpt-4o-mini"
}
```

**주의**: `PUBLIC_CHATBOT_MODEL_ID=jeonbuk-public-chatbot` 이지만 아직 DB에 래퍼 모델이 없으므로, 자동으로 `PUBLIC_CHATBOT_BASE_MODEL=gpt-4o-mini`로 fallback 됨. 서버 로그에 경고가 찍힘:
```
WARNING public_chatbot model 'jeonbuk-public-chatbot' not found, falling back to 'gpt-4o-mini'
```

### 2-2. 멀티턴 테스트

```bash
curl -s -X POST http://localhost:8080/api/v1/public/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message":"그래서 전북도청 위치가 어디야?",
    "history":[
      {"role":"user","content":"안녕"},
      {"role":"assistant","content":"안녕하세요! 무엇을 도와드릴까요?"}
    ]
  }' | jq
```

응답에 이전 대화 맥락을 이해한 답변이 나와야 함.

### 2-3. 프론트 테스트

브라우저에서 힌트 버튼 클릭 → 응답 확인. 히스토리가 localStorage에 저장되는지 DevTools에서 확인.

---

## 3. Knowledge Base + Model 래퍼 생성 (RAG 활성화)

**이 단계가 RAG가 실제로 동작하는 조건**. Model 래퍼 없으면 LLM에 `query_knowledge_files` 도구가 주입되지 않아서 KB 검색이 안 됨.

### 3-1. 관리자 토큰 획득

```bash
ADMIN_TOKEN=$(curl -s -X POST http://localhost:8080/api/v1/auths/signin \
  -H "Content-Type: application/json" \
  -d '{"email":"sprinter@mail.go.kr","password":"sprint26!"}' \
  | jq -r .token)

echo $ADMIN_TOKEN
```

### 3-2. Knowledge Base 생성

```bash
KB_RESPONSE=$(curl -s -X POST http://localhost:8080/api/v1/knowledge/create \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "jeonbuk_gov",
    "description": "전북도청 + 직속기관 홈페이지 크롤링 지식베이스"
  }')

echo $KB_RESPONSE | jq
KB_ID=$(echo $KB_RESPONSE | jq -r .id)
echo "KB_ID=$KB_ID"
```

**중요**: 크롤러는 컬렉션명으로 `CRAWLER_COLLECTION_NAME` (기본 `jeonbuk_gov`) 를 사용. 하지만 Knowledge Base의 collection은 KB의 UUID를 사용함. 즉 **두 컬렉션이 다를 수 있음**.

두 가지 선택:
- **A) 크롤러가 KB UUID 컬렉션에 저장하도록 설정**: `CRAWLER_COLLECTION_NAME` 환경변수를 생성된 KB_ID 값으로 변경 후 재시작. 이 방법이 Model 래퍼 + 크롤러 RAG 를 한 번에 연결하는 **권장 방법**.
- **B) 크롤러는 `jeonbuk_gov` 에 저장, KB는 별도 관리**: 이 경우 Model 래퍼의 `meta.knowledge`에 `collection_name` 필드로 수동 지정해야 하는데, 기존 RAG 흐름이 `collection_name` legacy 포맷을 어떻게 처리하는지 주의 필요.

**권장**: Docker 재시작 시 `CRAWLER_COLLECTION_NAME=<KB_ID>` 환경변수 추가.

### 3-3. Model 래퍼 생성

```bash
SYSTEM_PROMPT=$(cat <<'EOF'
당신은 전북특별자치도청 대도민 AI 안내원입니다.
전북도청 및 직속기관의 홈페이지 내용을 바탕으로 도민의 질문에 답변합니다.

# 답변 규칙
1. 풍부하고 완결된 답변: 일정/장소/담당기관/연락처/신청방법/URL/주의사항을 한 번에 포함
2. 기관명 명시: "인재개발원에 따르면..."
3. 연락처·링크 제공: 담당 부서 전화, 홈페이지 URL
4. 추측 금지: 문서에 없으면 "정확한 정보를 찾지 못했습니다"
5. 여러 기관 통합: 비슷한 정보가 여러 기관에 있으면 모두 나열
6. 한국어 정중한 어투
EOF
)

curl -s -X POST http://localhost:8080/api/v1/models/create \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"id\": \"jeonbuk-public-chatbot\",
    \"name\": \"전북도청 대도민 안내\",
    \"base_model_id\": \"gpt-4o-mini\",
    \"meta\": {
      \"description\": \"전북특별자치도청 공개 안내 챗봇\",
      \"knowledge\": [
        {
          \"id\": \"$KB_ID\",
          \"type\": \"collection\",
          \"name\": \"전북도청 통합 지식베이스\"
        }
      ]
    },
    \"params\": {
      \"system\": $(echo "$SYSTEM_PROMPT" | jq -Rs .)
    }
  }" | jq
```

### 3-4. 래퍼 모델이 인식되는지 확인

```bash
curl -s http://localhost:8080/api/v1/public/health | jq
# model_id: "jeonbuk-public-chatbot" 표시 확인

curl -s -X POST http://localhost:8080/api/v1/public/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"인재개발원 교육과정 뭐 있어?"}' | jq
```

KB가 비어 있으니 "관련 정보를 찾지 못했습니다" 같은 답변이 나와야 함 (추측 금지 규칙 반영).

---

## 4. Phase 1 검증 — 크롤러

### 4-1. 사이트 설정 확인

```bash
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://localhost:8080/api/v1/crawler/sites | jq
```

12개 사이트 (본청 + 직속기관) 목록이 나와야 함.

### 4-2. 단일 사이트 먼저 테스트

인재개발원만 크롤링 (테스트용):

```bash
curl -s -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
  "http://localhost:8080/api/v1/crawler/trigger/site/hrd_jeonbuk?mode=full" | jq
```

응답:
```json
{"status": "queued", "site_code": "hrd_jeonbuk", "mode": "full"}
```

서버 로그 모니터링:
```
INFO crawl_site START: hrd_jeonbuk mode=full
INFO discover_urls: site=hrd_jeonbuk collected=XX
... (페이지별 로드 로그) ...
INFO crawl_site DONE: hrd_jeonbuk stats={'new': XX, ...}
```

### 4-3. 크롤링 상태 확인

```bash
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://localhost:8080/api/v1/crawler/status | jq

curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  "http://localhost:8080/api/v1/crawler/pages?site_code=hrd_jeonbuk&limit=20" | jq
```

### 4-4. DB + 벡터 DB 확인

```bash
# SQL
psql -U admin -d customui -c "SELECT COUNT(*), status FROM crawled_page WHERE site_code='hrd_jeonbuk' GROUP BY status;"

psql -U admin -d customui -c "SELECT url, title, category, institution, chunks_count FROM crawled_page WHERE site_code='hrd_jeonbuk' LIMIT 10;"

# ChromaDB (vector DB directory)
ls -la /data/vector_db/
```

### 4-5. RAG 기반 질의 테스트

```bash
curl -s -X POST http://localhost:8080/api/v1/public/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"인재개발원 교육과정 뭐 있어?"}' | jq
```

예상: 크롤링 결과를 기반으로 실제 교육 과정 이름/일정/연락처 포함된 답변.

답변에 출처가 포함되어 있으면 sources 배열에 내용이 찰 것. 없으면 LLM이 도구를 호출 안 한 것 → model.meta.knowledge 설정 재확인 필요.

### 4-6. 전체 사이트 크롤링

로그 확인 준비 후:
```bash
curl -s -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://localhost:8080/api/v1/crawler/trigger/full | jq
```

12개 사이트 각각 수 분~수십 분 소요. 서버 로그 모니터링 필수. 한 사이트 실패해도 다음 사이트는 계속됨 (독립 예외 처리).

### 4-7. 증분 크롤링 테스트

전체 크롤링 완료 후, 곧바로 증분 실행:
```bash
curl -s -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://localhost:8080/api/v1/crawler/trigger/incremental | jq
```

로그에서 대부분 `unchanged` 로 찍히는지 확인 (ETag / Last-Modified / content_hash 비교 동작).

### 4-8. 일별 배치 스케줄 검증

환경변수 `CRAWLER_DAILY_HOUR`를 현재 시각 + 5분으로 임시 변경 → 서버 재시작 → 5분 뒤 로그에 자동 실행 기록 확인.

---

## 5. Phase 2a 검증 — 음성 입력

### 5-1. Cohere Transcribe 사용 시

환경변수:
```
STT_ENGINE=cohere
AUDIO_STT_COHERE_MODEL=CohereLabs/cohere-transcribe-03-2026
AUDIO_STT_COHERE_LANGUAGE=korean
```

**주의**:
- 최초 호출 시 HuggingFace 모델 다운로드 (~8GB, 수 분)
- GPU 16GB VRAM 권장 (없으면 CPU로 떨어져 매우 느림)

### 5-2. 브라우저에서 음성 테스트

1. `/static/public-chatbot.html` 접속
2. 마이크 권한 허용
3. 🎤 버튼 누르고 "인재개발원 교육과정" 말하기
4. 다시 🎤 눌러 정지 → 서버 전송
5. 인식된 텍스트 + 답변 확인
6. 브라우저 TTS로 답변 재생 (한국어)

### 5-3. curl 테스트

```bash
# 음성 파일 준비 (예: test.wav)
curl -s -X POST http://localhost:8080/api/v1/public/voice-chat \
  -F "file=@test.wav" \
  -F "history_json=[]" | jq
```

응답:
```json
{
  "question": "인재개발원 교육과정 뭐 있어요",
  "reply": "...",
  "session_id": "...",
  "sources": [...],
  "audio_url": null
}
```

---

## 6. 답변 품질 검증 질문

실제로 답변이 유용한지 체감 검증:

| # | 질문 | 기대 답변 포인트 |
|---|------|------------------|
| 1 | 다음 주에 있는 정보화교육 어떤 것이 있나요? | 날짜 범위 이해, 여러 기관 통합 |
| 2 | 어린이창의체험관 예약 어떻게 해요? | 기관명, 연락처, 링크 |
| 3 | 농업 기술 지원사업 종류 알려주세요 | 농업기술원 + 농식품인력개발원 통합 |
| 4 | 도립미술관 이번 달 전시 뭐야? | 상대 시간 이해 |
| 5 | 전북에서 일자리 찾으려면 어디로 가야 해? | 일자리센터 + 관련 기관 |
| 6 | 국악 배우려면 어디로 가? | 도립국악원 |
| 7 | 내 집 앞 도로 공사 언제 끝나? | 도로관리사업소 (도청 하위 페이지) |
| 8 | 전북도청 대표번호 뭐야? | 본청 연락처 |

답변에 다음이 포함되어야 함:
- ✅ 정확한 기관명
- ✅ 연락처 (전화/이메일)
- ✅ 홈페이지 URL
- ✅ 구체적 정보 (일정/장소/대상)
- ❌ "거기 있어요" 같은 모호한 답변
- ❌ 추측·환각

품질이 약하면 다음 단계 고려:
- 시스템 프롬프트 보강
- max_depth/max_pages 늘려서 더 많은 페이지 수집
- GraphRAG (Phase 3) 도입
- 모델을 gpt-4o-mini → gpt-4o로 업그레이드

---

## 7. 문제 해결 체크리스트

### 크롤링은 되는데 답변이 "모른다"고만 나올 때
- Model 래퍼의 `meta.knowledge[0].id` 가 실제 Knowledge Base UUID와 일치하는지
- `CRAWLER_COLLECTION_NAME` 과 KB의 collection name이 일치하는지 → **핵심**
- 서버 로그에서 `query_knowledge_files` 도구가 호출됐는지 확인
- ChromaDB 컬렉션에 실제 청크가 저장됐는지 확인

### "LLM 모델을 찾을 수 없습니다" 에러
- `PUBLIC_CHATBOT_BASE_MODEL` 이 실제로 OpenAI API에 존재하는지 (`gpt-4o-mini`)
- OpenAI API 키 설정 확인
- 서버 로그에서 `get_all_models` 실패 로그 확인

### Cohere Transcribe 로드 실패
- `transformers` 패키지 버전 확인
- HuggingFace 접속 가능한지
- GPU VRAM 부족 → STT_ENGINE을 `openai` 또는 `""`(faster-whisper) 로 임시 변경

### 크롤링이 너무 느림
- `WEB_LOADER_ENGINE=safe_web` 로 변경 (playwright보다 빠름, JS 렌더링 없음)
- `max_pages_per_site` 줄이기
- `CRAWLER_REQUEST_DELAY_MS` 조정 (너무 크면 느림)

### 크롤링 중 에러 많이 남
- 서버 로그에서 어떤 에러인지 확인
- 해당 사이트만 재크롤링: `/api/v1/crawler/trigger/site/{code}?mode=full`
- 문제 있는 사이트 URL 패턴을 `crawler_sites.py` `excluded_path_patterns`에 추가

---

## 8. 다음 단계 결정 트리

```
Phase 0~1 검증 결과
   │
   ├─ 답변 품질 충분? (출처 포함, 구체적)
   │   ├─ YES → 배포 준비 (커밋 + robots.txt 구현 + rate limit Redis 전환)
   │   └─ NO  → 아래 가지 중 선택
   │
   ├─ 카테고리 통합 약함? ("정보화교육 전부" 류 질문에 일부 기관만 답변)
   │   └─ YES → Phase 3 (GraphRAG) 진행
   │
   ├─ 시간 민감 질문 틀림? ("다음 주" 해석 오류)
   │   └─ YES → 시스템 프롬프트에 더 명시적 날짜 주입
   │
   ├─ 환각 많음?
   │   └─ YES → 모델을 gpt-4o로 업그레이드, temperature=0
   │
   └─ 음성 품질 부족?
       └─ Phase 2b (audio.py 리팩터링 + 서버 TTS) 진행
```

---

## 9. 주요 파일 참조

- 설정: `app/backend/open_webui/config.py` (끝부분 PUBLIC_CHATBOT_*, CRAWLER_*)
- 공개 챗봇 라우터: `app/backend/open_webui/routers/public_chatbot.py`
- 크롤러 라우터: `app/backend/open_webui/routers/crawler.py`
- 크롤러 로직: `app/backend/open_webui/tasks/crawler.py`
- 사이트 설정: `app/backend/open_webui/tasks/crawler_sites.py`
- CrawledPage 모델: `app/backend/open_webui/models/crawler.py`
- 마이그레이션: `app/backend/open_webui/migrations/versions/e7f8a9b0c1d2_add_crawled_page_table.py`
- 프론트엔드: `app/backend/open_webui/static/public-chatbot.html`
- 스케줄러 등록: `app/backend/open_webui/main.py` (lifespan 함수 내부)
