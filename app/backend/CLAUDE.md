# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is the **backend** for an Open WebUI instance (customized for 전북특별자치도 / Jeonbuk province). It is a Python FastAPI application that provides a unified API layer over multiple AI providers (OpenAI, Ollama, Anthropic, Google GenAI) with chat, RAG/retrieval, image generation, audio, and collaboration features.

## Common Commands

### Run dev server
```bash
PORT=8080 bash dev.sh
# Runs uvicorn with --reload on port 8080
```

### Run production server
```bash
bash start.sh
# Also starts SSO server (sso/sso_server.py) as a background process
```

### Run tests
```bash
# All tests
pytest open_webui/test/

# Single test file
pytest open_webui/test/apps/webui/routers/test_auths.py

# Single test
pytest open_webui/test/apps/webui/routers/test_auths.py::test_name
```

### Database migrations (Alembic)
```bash
# Run from /app/backend/open_webui/ (where alembic.ini lives)
cd open_webui
alembic upgrade head
alembic revision --autogenerate -m "description"
```

## Architecture

### Entry point
- `open_webui/main.py` — FastAPI app with lifespan, middleware stack (CORS, sessions, compression, audit logging), and all router mounts.

### Configuration layers
- `open_webui/env.py` — Reads environment variables and `.env` file. Defines directory paths (`DATA_DIR`, `BACKEND_DIR`, etc.), database URL, Redis URL, device type, logging config. This is the lowest config layer.
- `open_webui/config.py` — Higher-level app config built on top of `env.py`. Includes feature flags, API keys/URLs for AI providers, and persistent config helpers that store settings in the database.

### Database
- **SQLAlchemy** (primary ORM) with Alembic migrations in `open_webui/migrations/`.
- **Peewee** also present (legacy) via `peewee-migrate` in `open_webui/internal/migrations/`.
- `open_webui/internal/db.py` — Engine creation, session management (`ScopedSession`, `get_db`, `get_session`). Supports both SQLite and PostgreSQL. Connection pooling configured via `DATABASE_POOL_*` env vars.
- Models in `open_webui/models/` — one file per domain entity (users, chats, files, knowledge, etc.). Each file defines SQLAlchemy models and a data-access class.

### Routers (API endpoints)
All in `open_webui/routers/`. Key routers:
- `auths.py` — signup, signin, JWT auth
- `openai.py`, `ollama.py` — proxy routes to AI providers
- `chats.py`, `channels.py` — chat/conversation management
- `retrieval.py` — RAG pipeline (embeddings, vector search, reranking)
- `files.py`, `knowledge.py` — document/knowledge base management
- `tasks.py` — background task orchestration (title gen, queries, etc.)

### Real-time / WebSocket
- `open_webui/socket/main.py` — Socket.IO server (python-socketio) for real-time chat, collaborative editing (pycrdt/Yjs), model status tracking. Uses Redis for pub/sub when `WEBSOCKET_MANAGER=redis`.

### SSO
- `open_webui/sso/sso_server.py` — Flask-based SSO server started alongside main app in production (`start.sh`).

### Retrieval / RAG
- `open_webui/retrieval/` — Embedding models, vector store integrations, document loaders, web search connectors.

### Storage
- `open_webui/storage/provider.py` — Abstraction for file storage (local filesystem or S3).

### Utilities
- `open_webui/utils/` — Auth helpers, chat processing, code interpreter, MCP client, PDF generation, Redis utilities, rate limiting, audit logging, middleware.

### Custom libraries
- `python-hwplib/`, `python-hwpxlib/` — Korean HWP/HWPX document format parsers (project-specific).

## Key Environment Variables

- `DATABASE_URL` — SQLAlchemy connection string (default: SQLite in data dir)
- `REDIS_URL` — Redis connection for caching and WebSocket pub/sub
- `WEBUI_SECRET_KEY` / `WEBUI_JWT_SECRET_KEY` — Auth secrets
- `OLLAMA_BASE_URLS`, `OPENAI_API_BASE_URLS`, `OPENAI_API_KEYS` — AI provider configs
- `ENABLE_OLLAMA_API`, `ENABLE_OPENAI_API` — Feature toggles for providers
- `GLOBAL_LOG_LEVEL` — Logging level (DEBUG/INFO/WARNING/ERROR)
- `PYTHONPATH=/app/backend` — Required for module resolution
