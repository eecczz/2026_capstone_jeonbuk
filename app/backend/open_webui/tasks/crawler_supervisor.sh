#!/bin/bash
# 크롤 워커 supervisor — 16개 사이트 순차 + 죽으면 자동 재시작.
#
# 본청 (jeonbuk_main) 부터 시작해서 직속기관 / 시군 사이트로 이어감.
# 각 사이트마다 worker exit code 0 (clean exit) 이면 다음 사이트로,
# non-zero (예외 종료) 면 같은 사이트 재시작 (최대 MAX_RESTART_PER_SITE 번).
#
# 사용:
#   nohup bash crawler_supervisor.sh > /tmp/crawler_supervisor.log 2>&1 &

set -u
LOG="/tmp/crawler_worker.log"
MAX_RESTART_PER_SITE=10
RESTART_INTERVAL_S=10

# 순차 처리할 사이트 목록 — 가장 큰 본청부터, 그 후 직속기관/공공기관
SITES=(
    "jeonbuk_main"
    "tour_jb"
    "jbares"
    "jihe_jeonbuk"
    "hrd_jeonbuk"
    "forest_jb"
    "kukakwon"
    "jma"
    "jbchild"
    "agriacademy"
    "jbba"
    "jb_jobcenter"
)

# uvicorn master env 흡수
SOURCE_ENV=""
if [ -r /proc/61/environ ]; then
    SOURCE_ENV="/proc/61/environ"
elif [ -r /tmp/owi.env ]; then
    SOURCE_ENV="/tmp/owi.env"
fi

if [ -n "$SOURCE_ENV" ]; then
    while IFS= read -r line; do
        case "$line" in
            DATABASE_URL=*|VECTOR_DB=*|QDRANT*=*|PYTHONPATH=*|REDIS_URL=*|CRAWLER*=*|WEBUI*=*)
                export "$line"
                ;;
        esac
    done < <(tr '\0' '\n' < "$SOURCE_ENV")
fi

: "${VECTOR_DB:=qdrant}"
: "${PYTHONPATH:=/app/backend}"
export VECTOR_DB PYTHONPATH

for SITE in "${SITES[@]}"; do
    echo "[supervisor] $(date) ▶ start site=$SITE" >> "$LOG"
    RETRIES=0
    while [ "$RETRIES" -lt "$MAX_RESTART_PER_SITE" ]; do
        /usr/bin/python3.12 -m open_webui.tasks.crawler_worker \
            --site "$SITE" --mode full \
            >> "$LOG" 2>&1
        EC=$?
        if [ "$EC" -eq 0 ]; then
            echo "[supervisor] $(date) ✓ site=$SITE clean exit" >> "$LOG"
            break
        fi
        RETRIES=$((RETRIES+1))
        echo "[supervisor] $(date) ✗ site=$SITE exited code=$EC retry=$RETRIES/$MAX_RESTART_PER_SITE" >> "$LOG"
        sleep "$RESTART_INTERVAL_S"
    done
    if [ "$RETRIES" -ge "$MAX_RESTART_PER_SITE" ]; then
        echo "[supervisor] $(date) ✗✗ site=$SITE gave up — moving to next" >> "$LOG"
    fi
done

echo "[supervisor] $(date) all sites done" >> "$LOG"
