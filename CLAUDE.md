# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 🚨 최우선 원칙 — 근본적 해결 (이거 안 지키면 시간 더 듭니다)

**매 작업(plan/fix/구현) 시작 전 반드시 다음을 수행한다.**

1. **memory의 `project_principles.md` 읽기 (27항 전체)**.
2. **이번 작업이 어떤 원칙과 관련 있는지 명시한다.**
3. **surface fix가 아니라 root cause를 찾는다.**
   - "이거만 고치면 될 것 같은데" — 위험 신호. root cause 찾기 전까지 fix 시작 X.
   - 증상 (wrong output) → 직접적 path (어떤 코드가 wrong element 출력?) → 그 path가 의존하는 logic (어떤 본보기 사용?) → 그 logic의 가정 (가정이 양식 evidence와 맞는가?). **여기까지 들어가야 root cause**.
   - 근본 해결 안 하고 surface fix → 추후 같은 문제 재발하면서 시간 누적 (실제 사례: 2026-05-15 3시간 wrong output 반복 fix).

4. **schema/idx 레벨 진단으로 멈추지 말 것**.
   - assembly success_count = 27/0이라도 출력 wrong일 수 있음.
   - 항상 **element-level (실제 XML output)** 직접 검증. 양식 파일을 zipfile로 열어 section.xml paragraph text 직접 추출.
   - 사용자가 wrong output 보여줄 때까지 기다리지 말 것.

5. **변경 시 다른 코드 path의 가정과 충돌하는지 확인**.
   - Phase A 후 Phase B-1이 Phase A 가정 어기는 case 발생 (이번 세션). 매 변경 시 영향 범위 검토.

6. **시간이 없어 보일수록 근본 해결이 우선**.
   - "deadline이라 빠르게" → surface fix → 사용자가 wrong output 발견 → 다음 fix → 또 wrong → 시간 누적.
   - 30분 추가 들여 근본 해결하면 3시간 절약.

원칙 위반 사례 기록은 project_principles.md 끝의 "위반 사례" 섹션에 누적.

## Project Overview

Open WebUI instance customized for 전북특별자치도 (Jeonbuk province). Full-stack AI chat platform with multi-provider LLM support (OpenAI, Ollama, Anthropic, Google GenAI), RAG/retrieval, image generation, audio, collaborative editing, and Korean document format (HWP/HWPX) support.

**The frontend source is not in this repo** — only the pre-built SvelteKit output in `app/build/`. All active development happens in the Python backend under `app/backend/`.

## Common Commands

### Backend dev server
```bash
cd app/backend
PORT=8080 bash dev.sh
# Runs uvicorn with --reload, CORS allows localhost:5173 and :8080
```

### Production server
```bash
cd app/backend
bash start.sh          # Starts FastAPI + SSO server (background process)
bash start_windows.bat # Windows alternative
```

### Frontend (build output only — no source in repo)
```bash
cd app
npm run build          # Rebuild frontend (requires source checkout)
npm run dev            # Dev server with Vite (requires source checkout)
npm run dev:5050       # Dev server on port 5050
```

### Tests
```bash
# Backend (from app/backend/)
pytest open_webui/test/                                        # All tests
pytest open_webui/test/apps/webui/routers/test_auths.py        # Single file
pytest open_webui/test/apps/webui/routers/test_auths.py::test_name  # Single test

# Frontend
cd app && npm run test:frontend   # Vitest
cd app && npm run cy:open         # Cypress E2E
```

### Linting & Formatting
```bash
cd app
npm run lint                # All (frontend + types + backend)
npm run lint:frontend       # ESLint
npm run format              # Prettier
npm run format:backend      # Black (Python)
npm run check               # svelte-check + TypeScript
```

### Database Migrations (Alembic)
```bash
cd app/backend/open_webui
alembic upgrade head                           # Apply migrations
alembic revision --autogenerate -m "message"   # Create new migration
```

## Architecture

### Backend entry point
`app/backend/open_webui/main.py` — FastAPI app with lifespan manager, middleware stack (CORS, sessions, compression, audit logging), Socket.IO integration, and all router mounts. Serves the pre-built SvelteKit frontend as static files.

### Configuration (two layers)
- `open_webui/env.py` — Low-level: reads `.env` and environment variables. Defines `DATA_DIR`, `BACKEND_DIR`, database URL, Redis URL, device type, logging config.
- `open_webui/config.py` (~138KB) — High-level: feature flags, AI provider settings, persistent config stored in database via `PersistentConfig` helpers.

### Database
- **SQLAlchemy** (primary) with Alembic migrations in `open_webui/migrations/`.
- **Peewee** (legacy) with peewee-migrate in `open_webui/internal/migrations/`.
- `open_webui/internal/db.py` — Engine, session management (`ScopedSession`), supports SQLite and PostgreSQL.
- Models in `open_webui/models/` — one file per entity, each with SQLAlchemy model + data-access class.
- Default production database: PostgreSQL (`admin:sprint26!@localhost:5432/customui`).

### Routers (`open_webui/routers/`)
30 routers under `/api/v1/`. Key ones:
- `auths.py` — signup/signin/JWT
- `openai.py`, `ollama.py` — AI provider proxies
- `chats.py`, `channels.py` — chat management
- `retrieval.py` (~129KB) — RAG pipeline, embeddings, vector search, reranking
- `files.py`, `knowledge.py` — document/knowledge base management
- `tasks.py` — background task orchestration
- `audio.py`, `images.py` — media generation endpoints

### Real-time
`open_webui/socket/main.py` — Socket.IO (python-socketio) for WebSocket chat, collaborative editing (pycrdt/Yjs), model status. Uses Redis pub/sub when `WEBSOCKET_MANAGER=redis`.

### SSO
`open_webui/sso/sso_server.py` — Flask-based SSO server, started alongside main app in production. Uses HMAC-based token generation with `SSO_SHARED_SECRET`.

### RAG / Retrieval (`open_webui/retrieval/`)
- `loaders/` — Document loaders (PDF, PPTX, DOCX, HWP, etc.)
- `models/` — Embedding model management
- `vector/` — Vector store integrations (ChromaDB, Weaviate, OpenSearch, Pinecone, Milvus, Qdrant)
- `web/` — Web search connectors (DuckDuckGo, Firecrawl, etc.)
- `utils.py` (~50KB) — RAG pipeline orchestration

### Storage
`open_webui/storage/provider.py` — Abstraction over local filesystem or S3.

### Custom Korean Libraries
- `app/backend/python-hwplib/` — HWP document parser (uses jpype1 for Java interop)
- `app/backend/python-hwpxlib/` — HWPX document parser
- `open_webui/utils/hwp_generator.py`, `hwpx_analyzer.py` — Generation and analysis utilities

### Key Utilities (`open_webui/utils/`)
- `auth.py` — JWT handling, password hashing, user extraction
- `middleware.py` (~240KB) — Request/response middleware, audit logging
- `tools.py` (~50KB) — Tool execution framework
- `MCP/` — Model Context Protocol client support
- `redis.py` — Redis connection management

## Key Environment Variables

```bash
DATABASE_URL=postgresql://...      # SQLAlchemy connection string
REDIS_URL=redis://localhost:6379/1 # Cache and WebSocket pub/sub
WEBUI_SECRET_KEY=...               # Auth secret (auto-generated if missing)
SSO_SHARED_SECRET=wjsqnrai2025    # SSO token signing
OLLAMA_BASE_URLS=...               # Ollama API endpoints
OPENAI_API_BASE_URLS=...           # OpenAI-compatible API endpoints
OPENAI_API_KEYS=...                # API keys for OpenAI-compatible providers
GLOBAL_LOG_LEVEL=INFO              # DEBUG/INFO/WARNING/ERROR
PYTHONPATH=/app/backend            # Required for module resolution
ENABLE_WEBSOCKET_SUPPORT=true      # WebSocket features
WEBSOCKET_MANAGER=redis            # Use Redis for distributed WebSocket
```

## Docker / Deployment

`entrypoint.sh` orchestrates production startup:
1. PostgreSQL initialization (role: `admin`, db: `customui`)
2. Redis server
3. SSH server
4. Environment setup
5. FastAPI backend on port 8080

Initial admin account: `sprinter@mail.go.kr` / `sprint26!` (via `data/init_admin.sql`).

## Behavioral Guidelines

Behavioral guidelines to reduce common LLM coding mistakes. These bias toward caution over speed.

### 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.
