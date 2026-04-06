import black
import aiohttp
import logging
import markdown
import json
import datetime

from open_webui.models.chats import ChatTitleMessagesForm
from open_webui.config import DATA_DIR, ENABLE_ADMIN_EXPORT
from open_webui.constants import ERROR_MESSAGES
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from starlette.responses import FileResponse


from open_webui.utils.misc import get_gravatar_url
from open_webui.utils.pdf_generator import PDFGenerator
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.code_interpreter import execute_code_jupyter

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/gravatar")
async def get_gravatar(email: str, user=Depends(get_verified_user)):
    return get_gravatar_url(email)


class CodeForm(BaseModel):
    code: str


@router.post("/code/format")
async def format_code(form_data: CodeForm, user=Depends(get_admin_user)):
    try:
        formatted_code = black.format_str(form_data.code, mode=black.Mode())
        return {"code": formatted_code}
    except black.NothingChanged:
        return {"code": form_data.code}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/code/execute")
async def execute_code(
    request: Request, form_data: CodeForm, user=Depends(get_verified_user)
):
    if request.app.state.config.CODE_EXECUTION_ENGINE == "jupyter":
        output = await execute_code_jupyter(
            request.app.state.config.CODE_EXECUTION_JUPYTER_URL,
            form_data.code,
            (
                request.app.state.config.CODE_EXECUTION_JUPYTER_AUTH_TOKEN
                if request.app.state.config.CODE_EXECUTION_JUPYTER_AUTH == "token"
                else None
            ),
            (
                request.app.state.config.CODE_EXECUTION_JUPYTER_AUTH_PASSWORD
                if request.app.state.config.CODE_EXECUTION_JUPYTER_AUTH == "password"
                else None
            ),
            request.app.state.config.CODE_EXECUTION_JUPYTER_TIMEOUT,
        )

        return output
    else:
        raise HTTPException(
            status_code=400,
            detail="Code execution engine not supported",
        )


class MarkdownForm(BaseModel):
    md: str


@router.post("/markdown")
async def get_html_from_markdown(
    form_data: MarkdownForm, user=Depends(get_verified_user)
):
    return {"html": markdown.markdown(form_data.md)}


class ChatForm(BaseModel):
    title: str
    messages: list[dict]


class NewsRequest(BaseModel):
    employee_name: str


@router.post("/news")
async def proxy_news(request: Request, form_data: NewsRequest, user=Depends(get_verified_user)):
    target_url = request.app.state.config.NEWS_API_URL

    if not target_url:
        raise HTTPException(status_code=400, detail="News API URL not configured")

    redis = getattr(request.app.state, "redis", None)
    cache_key = None
    if redis:
        today = datetime.datetime.now().date().isoformat()
        user_identifier = user.email or user.id
        cache_key = f"news:{today}:{user_identifier}"

        cached = await redis.get(cache_key)
        if cached:
            try:
                return json.loads(cached)
            except Exception:
                pass

    ssl_config = getattr(request.app.state, "AIOHTTP_CLIENT_SESSION_SSL", False)

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(
                target_url,
                json={"employee_name": form_data.employee_name},
                ssl=ssl_config,
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json()

                if redis and cache_key:
                    try:
                        now = datetime.datetime.now()
                        tomorrow = (now + datetime.timedelta(days=1)).replace(
                            hour=0, minute=0, second=0, microsecond=0
                        )
                        ttl = int((tomorrow - now).total_seconds()) or 1
                        await redis.set(cache_key, json.dumps(payload), ex=ttl)
                    except Exception as e:
                        log.debug(f"Failed to cache news payload: {e}")

                return payload
    except Exception as e:
        log.exception(f"Failed to proxy news request: {e}")
        raise HTTPException(status_code=502, detail="Failed to fetch news")


@router.post("/pdf")
async def download_chat_as_pdf(
    form_data: ChatTitleMessagesForm, user=Depends(get_verified_user)
):
    try:
        pdf_bytes = PDFGenerator(form_data).generate_chat_pdf()

        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": "attachment;filename=chat.pdf"},
        )
    except Exception as e:
        log.exception(f"Error generating PDF: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/db/download")
async def download_db(user=Depends(get_admin_user)):
    if not ENABLE_ADMIN_EXPORT:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )
    from open_webui.internal.db import engine

    if engine.name != "sqlite":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DB_NOT_SQLITE,
        )
    return FileResponse(
        engine.url.database,
        media_type="application/octet-stream",
        filename="webui.db",
    )
