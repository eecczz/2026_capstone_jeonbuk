"""공개 챗봇 (public_chatbot + voice_ws) 의 오남용 방지.

Phase A (회의 안건 7):
1. IP rate limit (분당/일당) — Redis counter + TTL
2. 전체 일일 cap — 사내 GPU 보호
3. WebSocket 동시 연결 IP 당 1개 — Redis SETNX (분산 OK)
4. Input validation (텍스트 길이 / 음성 클립 길이 / 비한글 비율)

위협 모델:
- 개인 LLM 으로 남용 (코딩/번역/일반 지식)
- 봇 스크립트 dump
- 음성 abuse (긴 발화 / 외국어 dump)

설정 (env override 가능):
    PUBLIC_CHATBOT_RATE_PER_MIN  (default 10)
    PUBLIC_CHATBOT_RATE_PER_DAY  (default 100)
    PUBLIC_CHATBOT_DAILY_CAP     (default 5000)
    PUBLIC_CHATBOT_WS_TTL_SEC    (default 1800 — WS 슬롯 자동 해제)
    PUBLIC_CHATBOT_MAX_TEXT_LEN  (default 500)
    PUBLIC_CHATBOT_MIN_KR_RATIO  (default 0.3 — 한글 비율 30% 미만 시 거부)

Redis 없으면 fail-open (제한 X) — 가용성 우선.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

from fastapi import HTTPException, Request, WebSocket

log = logging.getLogger(__name__)

# 설정
RATE_PER_MIN = int(os.environ.get("PUBLIC_CHATBOT_RATE_PER_MIN", "10"))
RATE_PER_DAY = int(os.environ.get("PUBLIC_CHATBOT_RATE_PER_DAY", "100"))
DAILY_GLOBAL_CAP = int(os.environ.get("PUBLIC_CHATBOT_DAILY_CAP", "5000"))
WS_TTL_SEC = int(os.environ.get("PUBLIC_CHATBOT_WS_TTL_SEC", "1800"))
MAX_TEXT_LEN = int(os.environ.get("PUBLIC_CHATBOT_MAX_TEXT_LEN", "500"))
MIN_KR_RATIO = float(os.environ.get("PUBLIC_CHATBOT_MIN_KR_RATIO", "0.3"))


_redis = None
_redis_failed = False


def _get_redis():
    """Redis connection (lazy + cache). 실패 시 None — fail-open."""
    global _redis, _redis_failed
    if _redis is not None or _redis_failed:
        return _redis
    try:
        from open_webui.env import (
            REDIS_URL,
            REDIS_SENTINEL_HOSTS,
        )
        from open_webui.utils.redis import get_redis_connection

        _redis = get_redis_connection(
            redis_url=REDIS_URL,
            redis_sentinels=REDIS_SENTINEL_HOSTS,
            decode_responses=True,
        )
    except Exception as e:
        log.warning(f"Redis 연결 실패 — rate limit fail-open: {e}")
        _redis_failed = True
        _redis = None
    return _redis


def get_client_ip(req: Request | WebSocket) -> str:
    """X-Forwarded-For 우선 (nginx/CF 등 reverse proxy 통과 시 진짜 IP)."""
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

    Redis 없으면 fail-open ({"allowed": True}).
    """
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


def acquire_ws_slot(ws: WebSocket) -> bool:
    """WebSocket 동시 연결 — IP 당 1개. Redis SETNX (분산 OK).

    획득 못 하면 False — caller 가 close(1008).
    """
    redis = _get_redis()
    if redis is None:
        return True
    ip = get_client_ip(ws)
    try:
        acquired = redis.set(f"pcb:ws:{ip}", "1", nx=True, ex=WS_TTL_SEC)
        return bool(acquired)
    except Exception as e:
        log.warning(f"WS slot 획득 오류 — 허용: {e}")
        return True


def release_ws_slot(ws: WebSocket) -> None:
    redis = _get_redis()
    if redis is None:
        return
    try:
        redis.delete(f"pcb:ws:{get_client_ip(ws)}")
    except Exception:
        pass


_KR_RE = re.compile(r"[가-힣]")


def korean_ratio(text: str) -> float:
    if not text:
        return 0.0
    n = sum(1 for c in text if _KR_RE.match(c))
    return n / max(1, len(text))


def validate_text_input(text: str) -> dict[str, Any]:
    """입력 텍스트 검증. {"ok": bool, "reason": str}.

    - 길이 MAX_TEXT_LEN 초과 거부
    - 한글 비율 MIN_KR_RATIO 미만 거부 (영어 dump / 외국어 abuse 차단)
      단 짧은 입력 (10자 이하) 은 한글 비율 체크 면제 (전화번호 등 정상 케이스)
    """
    if not text or not text.strip():
        return {"ok": False, "reason": "내용이 비어있어요."}
    if len(text) > MAX_TEXT_LEN:
        return {
            "ok": False,
            "reason": f"질문이 너무 길어요. {MAX_TEXT_LEN}자 이내로 줄여 주세요.",
        }
    if len(text) > 10 and korean_ratio(text) < MIN_KR_RATIO:
        return {
            "ok": False,
            "reason": "한국어로 질문해 주세요. (전북도청 안내 챗봇이에요)",
        }
    return {"ok": True}


def enforce_rate_limit_http(req: Request) -> None:
    """HTTP endpoint 용 — 한도 초과 시 HTTPException 던짐."""
    r = check_rate_limit(req)
    if not r["allowed"]:
        raise HTTPException(
            status_code=429,
            detail=r["reason"],
            headers={"Retry-After": str(r.get("retry_after", 60))},
        )


def enforce_text_input(text: str) -> None:
    """HTTP endpoint 용 — 입력 검증 실패 시 HTTPException."""
    v = validate_text_input(text)
    if not v["ok"]:
        raise HTTPException(status_code=400, detail=v["reason"])
