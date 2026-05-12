# 2026 Capstone Jeonbuk

AI-assisted capstone project workspace based on a Svelte/Open WebUI frontend and a FastAPI backend.

## Tech Stack

- Frontend: SvelteKit, Vite, TypeScript
- Backend: FastAPI, SQLAlchemy, Redis, vector search dependencies
- Runtime: Docker-oriented local development

## Repository Notes

- Generated frontend build output is intentionally not tracked.
- Runtime data, local databases, uploaded files, and `.env` files are ignored.
- Copy the relevant environment example files before running local services.

## Local Development

```bash
cd app
npm install
npm run dev
```

```bash
cd app/backend
pip install -r requirements.txt
uvicorn open_webui.main:app --reload
```

Adjust commands as needed for the active backend entrypoint and local environment.
