# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
