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

# 전체 OFF 스위치 — 테스트 중 디버그 / 데모. ENV="1" 또는 "true" 면 모든 가드 패스.
ABUSE_DISABLED = os.environ.get("PUBLIC_CHATBOT_ABUSE_DISABLED", "0").lower() in ("1", "true", "yes")
MAX_TEXT_LEN = int(os.environ.get("PUBLIC_CHATBOT_MAX_TEXT_LEN", "500"))
MIN_KR_RATIO = float(os.environ.get("PUBLIC_CHATBOT_MIN_KR_RATIO", "0.3"))


# abuse pattern 알람 임계값 (hourly 누적)
ABUSE_ALARM_THRESHOLDS = [5, 20, 100]


_redis = None
_redis_failed = False


def _track_abuse(ip: str, action: str) -> None:
    """abuse 행동 hourly count. 임계값 도달 시 WARNING — admin 모니터링 hook.

    action: rate_min / rate_day / global_cap / text_too_long / non_korean /
            ws_slot_busy
    """
    redis = _get_redis()
    if redis is None:
        return
    try:
        import time as _t
        bucket = int(_t.time()) // 3600
        k = f"pcb:abuse:{ip}:{action}:{bucket}"
        n = redis.incr(k)
        if n == 1:
            redis.expire(k, 3700)
        if n in ABUSE_ALARM_THRESHOLDS:
            log.warning(
                f"ABUSE pattern detected: ip={ip} action={action} "
                f"count={n}/hour — 의심 사용자 가능성"
            )
    except Exception:
        pass


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
            _track_abuse(ip, "rate_min")
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
            _track_abuse(ip, "rate_day")
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
            _track_abuse(ip, "global_cap")
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
    """WebSocket 동시 연결 — IP 당 1개. Redis SET (overwrite).

    같은 IP 의 이전 slot 은 강제 대체. 새로고침/탭 전환이 정상 흐름이므로
    SETNX 로 거부하면 사용자가 30분간 차단됨 (이전 finally release 가
    PipelineRunner cleanup 후라 race condition). 정책상 마지막 ws 가 우선.

    Abuse 가드는 별도 — 분당 RATE_PER_MIN 이 이미 IP 당 동시 폭증을 막음.
    """
    if ABUSE_DISABLED:
        return True
    redis = _get_redis()
    if redis is None:
        return True
    ip = get_client_ip(ws)
    try:
        # ex=WS_TTL_SEC 만으로 자동 timeout — 비정상 termination 시 30분 후 reset
        redis.set(f"pcb:ws:{ip}", "1", ex=WS_TTL_SEC)
        return True
    except Exception as e:
        log.warning(f"WS slot 기록 오류 — fail-open: {e}")
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


def enforce_text_input(text: str, ip: str | None = None) -> None:
    """HTTP endpoint 용 — 입력 검증 실패 시 HTTPException + abuse 추적."""
    v = validate_text_input(text)
    if not v["ok"]:
        if ip:
            action = "text_too_long" if "너무 길어요" in v["reason"] else "non_korean"
            _track_abuse(ip, action)
        raise HTTPException(status_code=400, detail=v["reason"])


# ────────────────────────────────────────────────────────────────────────
# Phase C — 관측 (Redis scan 으로 abuse counter 집계)
# ────────────────────────────────────────────────────────────────────────


_ACTION_LABELS = {
    "rate_min": "분당 한도 초과",
    "rate_day": "일당 한도 초과",
    "global_cap": "전체 일일 cap 도달",
    "ws_slot_busy": "WS 동시 연결 차단",
    "text_too_long": "텍스트 길이 초과",
    "non_korean": "한국어 외 입력",
}


def collect_abuse_stats(window_hours: int = 24) -> dict[str, Any]:
    """Redis 의 pcb:abuse:* counter 들을 hour 단위로 집계.

    Args:
        window_hours: 최근 N 시간 (default 24)

    Returns:
        {
          "window_hours": 24,
          "total_blocked": N,
          "by_action": {"rate_min": M1, ...},
          "top_ips": [{"ip": "...", "count": K, "actions": {...}}],
          "hourly": [{"hour": "YYYY-MM-DD HH", "count": N}],
          "alarms_active": [...]
        }
    """
    redis = _get_redis()
    if redis is None:
        return {"error": "redis unavailable"}

    now = int(time.time())
    bucket_now = now // 3600
    bucket_min = bucket_now - window_hours + 1

    by_action: dict[str, int] = {}
    by_ip: dict[str, dict[str, Any]] = {}
    hourly: dict[int, int] = {b: 0 for b in range(bucket_min, bucket_now + 1)}
    total = 0
    alarms: list[dict[str, Any]] = []

    try:
        # SCAN 으로 pcb:abuse:{ip}:{action}:{bucket} 키 전부 순회.
        # 대량 차단 환경에서는 SCAN 이 안전 (KEYS 차단).
        cursor = 0
        while True:
            cursor, keys = redis.scan(cursor=cursor, match="pcb:abuse:*", count=200)
            for key in keys:
                # 키 포맷: pcb:abuse:{ip}:{action}:{bucket}
                # ip 안에 : 가 들어가지는 않으니 split(":", 4) 로 충분
                parts = key.split(":")
                if len(parts) != 5:
                    continue
                _, _, ip, action, bucket_str = parts
                try:
                    bucket = int(bucket_str)
                except ValueError:
                    continue
                if bucket < bucket_min:
                    continue
                try:
                    n = int(redis.get(key) or 0)
                except Exception:
                    continue
                if n <= 0:
                    continue

                total += n
                by_action[action] = by_action.get(action, 0) + n
                if ip not in by_ip:
                    by_ip[ip] = {"ip": ip, "count": 0, "actions": {}}
                by_ip[ip]["count"] += n
                by_ip[ip]["actions"][action] = by_ip[ip]["actions"].get(action, 0) + n
                hourly[bucket] = hourly.get(bucket, 0) + n
                # 임계값 도달 → alarm
                if n >= ABUSE_ALARM_THRESHOLDS[0]:
                    alarms.append({
                        "ip": ip,
                        "action": action,
                        "action_label": _ACTION_LABELS.get(action, action),
                        "count": n,
                        "hour": time.strftime(
                            "%Y-%m-%d %H:00",
                            time.localtime(bucket * 3600),
                        ),
                    })
            if cursor == 0:
                break

        # 상위 의심 IP (count 내림차순) 10개
        top_ips = sorted(by_ip.values(), key=lambda x: x["count"], reverse=True)[:10]

        # 시간대별 (오래된 것 → 최근)
        hourly_list = [
            {
                "hour": time.strftime("%Y-%m-%d %H:00", time.localtime(b * 3600)),
                "count": c,
            }
            for b, c in sorted(hourly.items())
        ]

        # alarm 정렬 (count 내림차순)
        alarms_sorted = sorted(alarms, key=lambda x: x["count"], reverse=True)[:20]

        return {
            "window_hours": window_hours,
            "total_blocked": total,
            "by_action": {
                k: {"count": v, "label": _ACTION_LABELS.get(k, k)}
                for k, v in sorted(by_action.items(), key=lambda x: -x[1])
            },
            "top_ips": top_ips,
            "hourly": hourly_list,
            "alarms": alarms_sorted,
            "thresholds": {
                "alarm_at": ABUSE_ALARM_THRESHOLDS,
                "rate_per_min": RATE_PER_MIN,
                "rate_per_day": RATE_PER_DAY,
                "daily_global_cap": DAILY_GLOBAL_CAP,
            },
        }
    except Exception as e:
        log.exception(f"collect_abuse_stats failed: {e}")
        return {"error": str(e)}


def collect_current_usage(window_hours: int = 1) -> dict[str, Any]:
    """현재 활성 사용자 (분/일 카운터). 실시간 사용 패턴."""
    redis = _get_redis()
    if redis is None:
        return {"error": "redis unavailable"}

    now = int(time.time())
    by_ip_min: dict[str, int] = {}
    by_ip_day: dict[str, int] = {}
    ws_active: list[str] = []

    try:
        # 분당 — 최근 5분
        for offset in range(5):
            bucket = (now // 60) - offset
            cursor = 0
            while True:
                cursor, keys = redis.scan(
                    cursor=cursor, match=f"pcb:rl:m:*:{bucket}", count=200
                )
                for key in keys:
                    parts = key.split(":")
                    if len(parts) != 5:
                        continue
                    ip = parts[3]
                    try:
                        n = int(redis.get(key) or 0)
                    except Exception:
                        continue
                    by_ip_min[ip] = by_ip_min.get(ip, 0) + n
                if cursor == 0:
                    break

        # 일당
        day_bucket = now // 86400
        cursor = 0
        while True:
            cursor, keys = redis.scan(
                cursor=cursor, match=f"pcb:rl:d:*:{day_bucket}", count=200
            )
            for key in keys:
                parts = key.split(":")
                if len(parts) != 5:
                    continue
                ip = parts[3]
                try:
                    by_ip_day[ip] = int(redis.get(key) or 0)
                except Exception:
                    continue
            if cursor == 0:
                break

        # 활성 WS slots
        cursor = 0
        while True:
            cursor, keys = redis.scan(cursor=cursor, match="pcb:ws:*", count=200)
            for key in keys:
                parts = key.split(":")
                if len(parts) == 3:
                    ws_active.append(parts[2])
            if cursor == 0:
                break

        # 전체 일일 호출
        gd_key = f"pcb:rl:gd:{day_bucket}"
        try:
            total_today = int(redis.get(gd_key) or 0)
        except Exception:
            total_today = 0

        top_min = sorted(
            [{"ip": ip, "count": n} for ip, n in by_ip_min.items()],
            key=lambda x: -x["count"],
        )[:10]
        top_day = sorted(
            [{"ip": ip, "count": n} for ip, n in by_ip_day.items()],
            key=lambda x: -x["count"],
        )[:10]

        return {
            "total_calls_today": total_today,
            "daily_global_cap": DAILY_GLOBAL_CAP,
            "active_ws_count": len(ws_active),
            "active_ws_ips": ws_active[:20],
            "active_users_last5min": len(by_ip_min),
            "active_users_today": len(by_ip_day),
            "top_users_last5min": top_min,
            "top_users_today": top_day,
        }
    except Exception as e:
        log.exception(f"collect_current_usage failed: {e}")
        return {"error": str(e)}
