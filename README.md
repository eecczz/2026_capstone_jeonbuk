# 2026 Capstone Jeonbuk

## 프로젝트 개요

Svelte/Open WebUI 기반 프론트엔드와 FastAPI 백엔드를 활용한 AI 보조 캡스톤 프로젝트 작업 공간입니다. 문서 처리, 백엔드 API, Redis, 벡터 검색 관련 의존성을 포함해 AI 서비스 형태로 확장할 수 있도록 구성했습니다.

## 기술 스택

- Frontend: SvelteKit, Vite, TypeScript
- Backend: FastAPI, SQLAlchemy
- Database/Cache: PostgreSQL 계열 의존성, Redis
- AI/Data: 벡터 검색 관련 의존성
- Runtime: Docker 기반 로컬 개발 환경

## 저장소 관리 기준

- 프론트엔드 빌드 결과물은 Git에 추적하지 않습니다.
- 런타임 데이터, 로컬 DB, 업로드 파일, `.env` 파일은 `.gitignore`로 제외합니다.
- 실행 전 필요한 환경 변수 예시 파일을 복사해 로컬 환경에 맞게 설정합니다.

## 로컬 실행

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

실제 실행 명령은 사용 중인 백엔드 진입점과 로컬 환경에 맞게 조정할 수 있습니다.
