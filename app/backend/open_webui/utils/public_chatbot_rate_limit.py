"""공개 챗봇 (public_chatbot + voice_ws) 의 IP 별 rate limit.

방지책 단일화 (사용자 결정) — IP 별 rate limit 만 유지, 다른 방지책 모두 제거됨:
  · WS 동시 연결 슬롯 (acquire/release_ws_slot)        — 제거
  · text input validation (한글 비율 / 길이)             — 제거
  · abuse pattern 추적 (hourly counter, alarm)          — 제거
  · admin 대시보드 (abuse-stats / usage-now / dashboard) — 제거
  · LLM domain guard prompt (도청 외 질문 거부)          — 제거

설정 (env override 가능):
    PUBLIC_CHATBOT_RATE_PER_MIN  (default 10)
    PUBLIC_CHATBOT_RATE_PER_DAY  (default 100)
    PUBLIC_CHATBOT_DAILY_CAP     (default 5000 — 사내 GPU 보호)
    PUBLIC_CHATBOT_ABUSE_DISABLED (default 0 — 1 이면 전체 OFF)

Redis 없으면 fail-open (제한 X).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from fastapi import HTTPException, Request, WebSocket

log = logging.getLogger(__name__)

RATE_PER_MIN = int(os.environ.get("PUBLIC_CHATBOT_RATE_PER_MIN", "10"))
RATE_PER_DAY = int(os.environ.get("PUBLIC_CHATBOT_RATE_PER_DAY", "100"))
DAILY_GLOBAL_CAP = int(os.environ.get("PUBLIC_CHATBOT_DAILY_CAP", "5000"))
ABUSE_DISABLED = os.environ.get("PUBLIC_CHATBOT_ABUSE_DISABLED", "0").lower() in ("1", "true", "yes")

_redis = None
_redis_failed = False


def _get_redis():
    """OWI 의 redis util 재사용. 실패하면 None — fail-open."""
    global _redis, _redis_failed
    if _redis_failed:
        return None
    if _redis is not None:
        return _redis
    try:
        from open_webui.utils.redis import get_redis_connection

        _redis = get_redis_connection(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
        return _redis
    except Exception as e:
        log.warning(f"rate_limit Redis init 실패 — fail-open: {e}")
        _redis_failed = True
        return None


def get_client_ip(req: Request | WebSocket) -> str:
    """X-Forwarded-For 우선 (reverse proxy 통과 시 진짜 IP)."""
    headers = getattr(req, "headers", {})
    xff = headers.get("x-forwarded-for") if headers else None
    if xff:
        return xff.split(",")[0].strip()
    client = getattr(req, "client", None)
    if client and getattr(client, "host", None):
        return client.host
    return "unknown"


def check_rate_limit(req: Request | WebSocket) -> dict[str, Any]:
    """IP rate limit 검사. {"allowed": bool, "reason": str, "retry_after": int}.

    분당 / 일당 / 전체 일일 cap 세 단계. Redis 없으면 fail-open.
    """
    if ABUSE_DISABLED:
        return {"allowed": True, "ip": get_client_ip(req), "disabled": True}
    redis = _get_redis()
    if redis is None:
        return {"allowed": True, "ip": get_client_ip(req)}

    ip = get_client_ip(req)
    now = int(time.time())

    try:
        # 1) 분당
        k_min = f"pcb:rl:m:{ip}:{now // 60}"
        n_min = redis.incr(k_min)
        if n_min == 1:
            redis.expire(k_min, 70)
        if n_min > RATE_PER_MIN:
            log.warning(f"rate_limit minute exceeded ip={ip} n={n_min}")
            return {
                "allowed": False,
                "reason": f"잠시만요. 분당 {RATE_PER_MIN}회 한도를 넘었어요.",
                "retry_after": 60 - (now % 60),
                "ip": ip,
            }

        # 2) 일당
        k_day = f"pcb:rl:d:{ip}:{now // 86400}"
        n_day = redis.incr(k_day)
        if n_day == 1:
            redis.expire(k_day, 86460)
        if n_day > RATE_PER_DAY:
            log.warning(f"rate_limit daily exceeded ip={ip} n={n_day}")
            return {
                "allowed": False,
                "reason": f"오늘은 {RATE_PER_DAY}회 한도를 다 사용하셨어요. 내일 다시 시도해 주세요.",
                "retry_after": 86400 - (now % 86400),
                "ip": ip,
            }

        # 3) 전체 일일 cap (사내 GPU 보호)
        k_gd = f"pcb:rl:gd:{now // 86400}"
        n_gd = redis.incr(k_gd)
        if n_gd == 1:
            redis.expire(k_gd, 86460)
        if n_gd > DAILY_GLOBAL_CAP:
            log.warning(f"rate_limit global cap exceeded n={n_gd}")
            return {
                "allowed": False,
                "reason": "오늘 챗봇 전체 사용량 한도에 도달했어요. 내일 다시 시도해 주세요.",
                "retry_after": 86400 - (now % 86400),
                "ip": ip,
            }
    except Exception as e:
        log.warning(f"rate_limit Redis 오류 — fail-open: {e}")
        return {"allowed": True, "ip": ip}

    return {"allowed": True, "ip": ip, "n_min": n_min, "n_day": n_day}


def enforce_rate_limit_http(req: Request) -> None:
    """HTTP endpoint 용 — 한도 초과 시 HTTPException 429 raise."""
    rl = check_rate_limit(req)
    if not rl["allowed"]:
        raise HTTPException(
            status_code=429,
            detail={"message": rl["reason"], "retry_after": rl.get("retry_after", 0)},
            headers={"Retry-After": str(rl.get("retry_after", 0))},
        )
