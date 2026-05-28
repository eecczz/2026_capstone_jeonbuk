#!/bin/bash
# gunsan_city / wanju_county 만 재 trigger — priority_paths 확장 후 효과 검증용.
#
# 사전 조건:
#   owi-restart  (또는 sudo kill -HUP 62) 로 워커 reload — v2 config 적용
#
# 사용:
#   bash scripts/trigger_gunsan_wanju.sh

set -euo pipefail

API_BASE="http://localhost:8080/api/v1"
ADMIN_EMAIL="sprinter@mail.go.kr"
ADMIN_PASSWORD='sprint26!'

SITES=("gunsan_city" "wanju_county")

echo "1) admin JWT 발급"
TOKEN=$(curl -sS -X POST "$API_BASE/auths/signin" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin).get("token",""))')

if [ -z "$TOKEN" ]; then
  echo "   ❌ JWT 발급 실패"
  exit 1
fi
echo "   ✅ JWT 발급 OK"

echo
echo "2) gunsan_city / wanju_county trigger (incremental)"
for code in "${SITES[@]}"; do
  resp=$(curl -sS -X POST "$API_BASE/crawler/trigger/site/$code?mode=incremental" \
    -H "Authorization: Bearer $TOKEN")
  echo "   $code → $resp"
done

echo
echo "✅ 큐잉 완료."
echo "   진행 추적 (10~30분 간격):"
echo "     PGPASSWORD='sprint26!' psql -h localhost -U admin -d customui -c \\"
echo "       \"SELECT site_code, COUNT(*) AS pages, COUNT(*) FILTER (WHERE status='success') AS ok, to_timestamp(MAX(last_crawled_at)) AT TIME ZONE 'Asia/Seoul' AS last FROM crawled_page WHERE site_code IN ('gunsan_city','wanju_county') GROUP BY site_code;\""
