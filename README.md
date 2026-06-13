# 전북도청 대도민 음성 안내 챗봇 (Voice Chatbot)

실시간 음성 대화 기반의 전북특별자치도청 대도민 AI 안내원입니다.
도민이 마이크로 말을 걸면 VRM 3D 캐릭터가 응답을 말하고, 전북도청·직속기관
홈페이지 크롤링 지식베이스(RAG)를 근거로 답변합니다.

> 본 저장소는 **Open WebUI 포크(`2026_capstone_jeonbuk`)** 로, HWPX 문서 생성 등
> 다른 팀원의 기능과 같은 백엔드를 공유합니다. 본 README는 그 중 **"음성 챗봇"
> 서브시스템**을 다루며, 아래 ["담당 파일"](#담당-파일--기여-범위) 섹션의 파일들이
> 본 작업의 범위입니다.

---

## 개요

기존 텍스트 챗봇(`/api/v1/public/chat`)을 그대로 유지하면서, **음성 모드**를
별도 WebSocket 파이프라인으로 추가했습니다. 음성 모드의 핵심은 단순 STT→TTS가
아니라 **turn-taking / interrupt(barge-in) 품질**과 **체감 지연(latency) 은폐**,
그리고 **VRM 캐릭터를 통한 자연스러운 안내원 경험**입니다.

- 무인 키오스크/웹에서 **인증 없이** 누구나 말로 질문 → 음성으로 답변
- 사내 vLLM(OpenAI 호환) 기반 LLM/STT/TTS — **외부 API 비용 0**
- 크롤러가 매일 수집한 도청 홈페이지 콘텐츠를 RAG 근거로 사용

---

## 핵심 기능

### 🎙️ 실시간 음성 대화 (Pipecat 기반)
- **Silero VAD + Smart Turn** 으로 발화 경계/턴 종료 감지 (카페 수준 소음 대응)
- **Barge-in**: 답변 재생 중 사용자가 말하면 중단하고 새 질문 처리
- raw PCM 16kHz 16-bit mono **양방향 WebSocket 스트리밍** (별도 protobuf serializer 없이 최단 경로)
- 답변 중 들어온 VAD start 무시 등 turn 누수 방지 휴리스틱

### 🐶 VRM 3D 캐릭터 + 립싱크
- **Three.js + three-vrm** 으로 `character.vrm` 로드 (VRM 0.x)
- **TTS 오디오 진폭 → 입 blendshape** 매핑 실시간 립싱크 + 발음 다양화
- `idle_loop.vrma` idle 모션 + restless idle 클립으로 대기 중 생동감
- VRM 로드 직후 "안녕하세요" 인사 시작, 카메라 자동 보정
- 두 가지 비주얼 모드: **orb**(기본) / **character**

### ⚡ 체감 지연(latency) 은폐
- WS 연결 직후 **endpoint prewarm** (LLM KV cache / TTS / RAG warm) — 첫 답변 cold start 해소
- **heartbeat filler 음성**: 긴 LLM TTFB(최대 132s) 동안 자연스러운 filler 발화로 공백 메움
- 무한 filler 차단(LLM error/exception 시 종료), MIN_SENT_LEN 조정으로 첫 문장 빠른 합성

### 🧹 룰베이스 STT 후처리 / 쿼리 정제
- mini-LLM 제거 후 **룰 기반 cleaning** 으로 정제 지연 1.94s → <10ms
- 도메인 어휘 교정(예: `국취제` → `국민취업지원제도`), 한국어 발음/날짜 표현 정규화
- 의문 종결어미 → 명사형 변환으로 자막 매끄럽게, 지시대명사 치환(history 맥락)
- directedness 스코어링으로 배경 잡음/무관 발화 무시(`ignore`), 단답 보정, intent 힌트

### 📚 RAG 연동 (지식베이스 + 크롤러)
- 음성 경로도 텍스트 챗봇과 **동일한 RAG/도메인 휴리스틱/답변 humanize 재사용**
- voice_mode 에서는 GraphRAG(Neo4j) skip 으로 vLLM prefill 절약
- 전북도청 + 직속기관 12개 사이트 크롤링 KB 를 근거로 답변

### 🛡️ 대도민 공개 운영 안정화
- IP rate limit 기반 오남용 방지(WS 슬롯 제한 포함)
- 인증 없는 공개 엔드포인트로 키오스크/임베드 배포 가능

---

## 아키텍처

### 설계 철학
- **기존 경로 보존**: 텍스트 챗봇/HWPX 등 다른 기능을 건드리지 않고 음성 모드를 추가
- **품질 우선의 음성 흐름**: turn-taking/barge-in은 Pipecat의 검증된 VAD/Turn 파이프라인 채택
- **자체 RAG 재사용**: LLM/RAG 단은 우리 `public_chatbot` 내부 처리를 그대로 호출
- **비용 0 운영**: STT/TTS/LLM 모두 사내 vLLM(OpenAI 호환) endpoint 사용
- **체감 속도**: 정답 속도를 못 줄이면, prewarm + filler 로 "기다림"을 설계로 가린다

### 계층 구조

```
┌──────────────────────────────────────────────────────────┐
│                  Presentation Layer                        │
│   public-chatbot.html  (Three.js + three-vrm, Web Audio)   │
│   · VRM 캐릭터 렌더 / 립싱크 / idle 모션                    │
│   · 마이크 PCM 캡처 ↔ WebSocket 양방향 스트림              │
└──────────────────────────────────────────────────────────┘
                          │  ws  (raw PCM 16kHz)
┌──────────────────────────────────────────────────────────┐
│                   Application Layer                         │
│   voice_ws.py (/api/v1/public/voice-ws)                    │
│   public_chatbot.py (/api/v1/public/chat, /voice-chat)     │
└──────────────────────────────────────────────────────────┘
                          │
┌──────────────────────────────────────────────────────────┐
│                     Domain Layer                           │
│  ┌────────────────┐ ┌────────────────┐ ┌───────────────┐ │
│  │ Pipecat Pipe   │ │ Voice Cleaning │ │ RAG Pipeline  │ │
│  │ VAD/STT/TTS    │ │ (rule-based)   │ │ (도청 KB)     │ │
│  └────────────────┘ └────────────────┘ └───────────────┘ │
└──────────────────────────────────────────────────────────┘
                          │
┌──────────────────────────────────────────────────────────┐
│                 Infrastructure Layer                       │
│  사내 vLLM (OpenAI 호환): LLM / Cohere STT / Qwen3-TTS     │
│  Qdrant 벡터 DB · 크롤러 KB · Redis · PostgreSQL           │
└──────────────────────────────────────────────────────────┘
```

### 음성 파이프라인 (Pipecat 1.1)

```
ws(in) ─▶ Silero VAD ─▶ STT(Cohere) ─▶ RAG(JeonbukRAGProcessor) ─▶ TTS(Qwen3-TTS) ─▶ ws(out)
            발화경계        텍스트          정제+검색+LLM답변            음성 합성        PCM 스트림
```

- `voice_ws.py` 가 **RawPcmSerializer** 를 직접 정의해 binary frame ↔ `InputAudioRawFrame` /
  `OutputAudioRawFrame` ↔ binary frame 매핑을 처리 (Pipecat은 serializer 없으면 WS 메시지를 모두 skip)
- LLM/RAG 단은 자체 `_run_chat_internal`(public_chatbot)을 호출하는 `JeonbukRAGProcessor`로 처리

### 데이터 플로우 (음성 한 턴)

```
1. 사용자 발화 (마이크)
   ↓  브라우저: getUserMedia → PCM 16kHz LE mono → WS binary
2. Silero VAD 가 발화 시작/종료 감지 (배경 소음은 통과 차단)
   ↓
3. Cohere STT 로 텍스트화 → 룰베이스 cleaning(어휘교정/정규화/지시어 치환)
   ↓
4. directedness 판정: answer / clarify / ignore
   ↓ (answer 일 때만)
5. 도청 KB RAG 검색 → LLM 답변 생성 (heartbeat filler 로 지연 은폐)
   ↓
6. 답변 문장 단위로 Qwen3-TTS 합성 → PCM 스트림으로 WS 송출
   ↓
7. 브라우저: 오디오 재생 + 진폭 기반 VRM 입 립싱크 + 자막
```

---

## 주요 엔드포인트

모두 `/api/v1/public` prefix, **인증 없음(대도민 공개)**.

| 메서드 | 경로 | 설명 |
|--------|------|------|
| `GET`  | `/chatbot.html` | 챗봇 프론트엔드 |
| `POST` | `/chat` | 텍스트 챗봇 (multiturn, RAG) |
| `POST` | `/voice-chat` | 단발 음성 업로드(파일) → 답변 + audio_url |
| `WS`   | `/voice-ws` | **실시간 음성 대화** (Pipecat, PCM 양방향) |
| `GET`  | `/voice-audio/{filename}` | 합성된 TTS 오디오 서빙 |
| `GET`  | `/health` | 상태 점검 (model_id, stt/tts engine, rate limit) |

프론트엔드 직접 접근: `http://<host>:8080/static/public-chatbot.html`

---

## LLM / 음성 스택

| 역할 | 모델 (사내 vLLM, OpenAI 호환) |
|------|-------------------------------|
| 메인 LLM | Qwen3.5-397B-A17B-FP8 (사내 검증 active) · K-EXAONE-236B 등 시도 이력 |
| STT | Cohere Transcribe (`CohereLabs/cohere-transcribe-03-2026`, korean) |
| TTS | Qwen3-TTS (voice: Sohee) |

> 모델 선택 이력은 git log `feat(voice)` 커밋에 기록되어 있습니다. 외부 OpenAI는
> fallback/개발용이며 운영은 사내 endpoint로 비용 0.

---

## 환경 변수 (음성 모드)

```bash
# STT / TTS (사내 vLLM endpoint)
AUDIO_STT_ENGINE=cohere
AUDIO_STT_MODEL=cohere-transcribe
AUDIO_STT_OPENAI_API_BASE_URL=<사내 STT endpoint>
AUDIO_TTS_ENGINE=qwen
AUDIO_TTS_MODEL=qwen3-tts
AUDIO_TTS_VOICE=Sohee
AUDIO_TTS_OPENAI_API_BASE_URL=<사내 TTS endpoint>

# 공개 챗봇 / RAG
PUBLIC_CHATBOT_MODEL_ID=jeonbuk-public-chatbot
PUBLIC_CHATBOT_BASE_MODEL=<base model id>
PUBLIC_CHATBOT_KNOWLEDGE_ID=collection:<knowledge_base_id>   # 또는 legacy:jeonbuk_gov
```

자세한 운영 셋업/검증 절차는
[PUBLIC_CHATBOT_SETUP.md](app/backend/open_webui/tasks/PUBLIC_CHATBOT_SETUP.md),
RAG 바인딩/STT 후처리 상세는
[VOICE_CHATBOT_NOTES.md](app/backend/open_webui/tasks/VOICE_CHATBOT_NOTES.md) 참조.

---

## 사용법

### 서버 기동
```bash
cd app/backend
PORT=8080 bash dev.sh        # 개발 (uvicorn --reload)
# 또는 운영: bash start.sh
```

### 음성 대화 흐름
1. 브라우저로 `/static/public-chatbot.html` 접속
2. 비주얼 모드를 **character** 로 (또는 기본 orb)
3. 마이크 권한 허용 → 캐릭터(또는 orb) 클릭으로 음성 모드 진입
4. 말로 질문 → 캐릭터가 음성+자막+립싱크로 답변
5. 답변 중에도 말을 걸면(barge-in) 중단하고 새 질문 처리

### 헬스 체크
```bash
curl -s http://localhost:8080/api/v1/public/health | jq
```

---

## 담당 파일 / 기여 범위

음성 챗봇 작업에서 추가/담당한 파일:

**백엔드 — 라우터**
- [routers/voice_ws.py](app/backend/open_webui/routers/voice_ws.py) — Pipecat 실시간 음성 WS, PCM serializer, prewarm
- [routers/public_chatbot.py](app/backend/open_webui/routers/public_chatbot.py) — 공개 텍스트/음성 챗봇 라우터

**백엔드 — 유틸**
- [utils/voice_rag_pipeline.py](app/backend/open_webui/utils/voice_rag_pipeline.py) — `JeonbukRAGProcessor`, 음성용 RAG 파이프라인
- [utils/voice_cleaning_rules.py](app/backend/open_webui/utils/voice_cleaning_rules.py) — 룰베이스 STT 후처리/쿼리 정제
- [utils/voice_tts_text.py](app/backend/open_webui/utils/voice_tts_text.py) — TTS 한국어 발음/숫자 변환
- [utils/voice_pcm_serializer.py](app/backend/open_webui/utils/voice_pcm_serializer.py) — raw PCM 양방향 serializer
- [utils/public_chatbot_rate_limit.py](app/backend/open_webui/utils/public_chatbot_rate_limit.py) — IP rate limit / WS 슬롯

**프론트엔드 / 에셋**
- [static/public-chatbot.html](app/backend/open_webui/static/public-chatbot.html) — VRM 캐릭터·립싱크·마이크 PCM·WS UI
- `static/character.vrm` — VRM 캐릭터 모델
- `static/idle_loop.vrma` — idle 모션 클립

**문서**
- [tasks/VOICE_CHATBOT_NOTES.md](app/backend/open_webui/tasks/VOICE_CHATBOT_NOTES.md)
- [tasks/PUBLIC_CHATBOT_SETUP.md](app/backend/open_webui/tasks/PUBLIC_CHATBOT_SETUP.md)

**통합 지점(공유 파일, 등록만 추가)**
- `main.py` — `voice_ws` / `public_chatbot` 라우터 등록 (`/api/v1/public`), WS prewarm
- `config.py` — `PUBLIC_CHATBOT_*`, `AUDIO_STT_*`, `AUDIO_TTS_*`, `VOICE_*` 설정

---

## 확장하기

### 캐릭터 교체
`static/character.vrm` 를 VRM 0.x 모델로 교체. 카메라/headY는 로드 시 자동 측정·보정.

### TTS 음성/모델 변경
`AUDIO_TTS_MODEL` / `AUDIO_TTS_VOICE` 환경변수 변경. 발음 보정은 `voice_tts_text.py`에서.

### 도메인 어휘 교정 추가
`voice_cleaning_rules.py` 의 교정 테이블/동사 종결어미 set에 항목 추가.
(하드코딩된 특정 문구/날짜는 금지 — 룰/패턴으로만 일반화)

---

## 저장소 메모

- 프론트엔드 빌드 결과물은 Git에 추적하지 않습니다.
- 런타임 데이터, 로컬 DB, 업로드 파일, `.env` 는 `.gitignore` 로 제외합니다.
- 다른 팀원 기능(HWPX 문서 생성 등)은 동일 백엔드(`app/backend/open_webui/`)에 공존합니다.
