#!/bin/bash
# /app/backend 의 Python 파일 변경 감지 → 워커 자동 재시작.
# master 가 살아있으면 워커가 자동 spawn 되므로 워커만 kill 하면 충분.
#
# 사용:
#   sudo nohup bash /home/sprint/2026_capstone_jeonbuk/scripts/owi_autoreload.sh \
#     >> /tmp/owi_autoreload.log 2>&1 &
#
# 중지:
#   sudo pkill -f owi_autoreload.sh
#
# 동작:
#   - inotifywait 가 /app/backend/open_webui 안 **Python 파일만** 변경 감시
#   - static/migrations/__pycache__ 같은 디렉터리는 제외 (워커 startup 마다
#     custom.css 가 삭제되는 부수효과로 무한 reload 루프 만든 이전 버그 차단)
#   - 변경 감지 시 multiprocessing.spawn 워커들 kill → master 자동 spawn
#   - debounce 2초 (연속 변경 묶음 처리)

WATCH_DIR=/app/backend/open_webui
DEBOUNCE_SEC=2

echo "[$(date +%F\ %T)] owi_autoreload started, watching $WATCH_DIR (*.py only, excluding static/migrations)"

last_reload=0
inotifywait -m -r -q \
  -e modify,create,delete,move \
  --include '\.py$' \
  --exclude '/static/|/migrations/|/__pycache__/|\.pyc$' \
  --format '%T %w%f %e' \
  --timefmt '%H:%M:%S' \
  "$WATCH_DIR" 2>/dev/null | \
while read t file event; do
  now=$(date +%s)
  # debounce — 마지막 reload 후 N초 안엔 skip
  if [ $((now - last_reload)) -lt $DEBOUNCE_SEC ]; then
    continue
  fi
  echo "[$(date +%F\ %T)] $event $file → reloading workers"
  pids=$(pgrep -f "multiprocessing.spawn" 2>/dev/null)
  if [ -n "$pids" ]; then
    kill -9 $pids 2>/dev/null
    last_reload=$now
  else
    echo "[$(date +%F\ %T)] WARN: no multiprocessing.spawn workers found"
  fi
done
