#!/bin/bash
# 크롤 워커 supervisor — 죽으면 자동 재시작 (최대 N회).
#
# crawler_worker 가 sitemap SSL / network error / DB lock 같은 일시적 사유로
# 죽을 수 있어 PG·Qdrant·BGE M3 같은 인프라가 살아있다는 가정 하에 자동 재시작.
#
# 사용:
#   nohup bash crawler_supervisor.sh > /tmp/crawler_supervisor.log 2>&1 &
#
# 환경: master uvicorn 의 env (VECTOR_DB / QDRANT_* / DATABASE_URL 등) 가 필요.

set -u
LOG="/tmp/crawler_worker.log"
MAX_RESTART=50
RESTART_INTERVAL_S=10
COUNT=0

# uvicorn master env 흡수 (없으면 동작 안 함)
if [ -r /proc/61/environ ]; then
  while IFS= read -r line; do
    case "$line" in
      DATABASE_URL=*|VECTOR_DB=*|QDRANT*=*|PYTHONPATH=*|REDIS_URL=*|CRAWLER*=*|WEBUI*=*)
        export "$line"
        ;;
    esac
  done < <(tr '\0' '\n' < /proc/61/environ)
fi

while [ "$COUNT" -lt "$MAX_RESTART" ]; do
  echo "[supervisor] $(date) starting crawler_worker (attempt $((COUNT+1)))" >> "$LOG"
  /usr/bin/python3.12 -m open_webui.tasks.crawler_worker \
      --site jeonbuk_main --mode full \
      >> "$LOG" 2>&1
  EC=$?
  echo "[supervisor] $(date) worker exited code=$EC" >> "$LOG"
  COUNT=$((COUNT+1))
  if [ "$EC" -eq 0 ]; then
    echo "[supervisor] $(date) clean exit — stop" >> "$LOG"
    break
  fi
  sleep "$RESTART_INTERVAL_S"
done

echo "[supervisor] $(date) gave up after $COUNT restarts" >> "$LOG"
