#!/bin/bash
# gunsan_city / wanju_county 만 mode=full trigger.
#
# Why mode=full:
#   incremental 은 content_hash 동일 시 unchanged 로 패스 → 새 URL 만 적재.
#   BFS 가 X1 (300s) 로 collected URL 가 277/131 까지 늘었어도 incremental 은
#   거의 다 unchanged 처리. mode=full 은 content_hash 비교 없이 모두 fetch+save
#   → collected 277/131 다 적재 시도. quota max_pages=800 까지 채움.
#
# 사전 조건: 워커가 X1 (BFS=300s) 메모리 로딩됨 (master 16:50+ start).
#
# 사용:
#   bash scripts/trigger_full_gunsan_wanju.sh

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
echo "2) gunsan_city / wanju_county trigger (mode=full)"
for code in "${SITES[@]}"; do
  resp=$(curl -sS -X POST "$API_BASE/crawler/trigger/site/$code?mode=full" \
    -H "Authorization: Bearer $TOKEN")
  echo "   $code → $resp"
done

echo
echo "✅ 큐잉 완료."
echo "   mode=full 은 content_hash 비교 없이 모두 fetch+save."
echo "   collected 277/131 다 처리 → max_pages=800 까지 채움 (10~20분 추정)."
echo
echo "   진행 추적:"
echo "     PGPASSWORD='sprint26!' psql -h localhost -U admin -d customui -c \\"
echo "       \"SELECT site_code, COUNT(*) AS pages, COUNT(*) FILTER (WHERE status='success') AS ok, to_timestamp(MAX(last_crawled_at)) AT TIME ZONE 'Asia/Seoul' AS last FROM crawled_page WHERE site_code IN ('gunsan_city','wanju_county') GROUP BY site_code;\""
