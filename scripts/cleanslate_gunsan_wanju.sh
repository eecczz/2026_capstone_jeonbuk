#!/bin/bash
# gunsan/wanju Y' clean slate — 기존 row + Qdrant chunks 다 삭제 → mode=full 재크롤.
#
# 흐름:
#   1. DELETE /api/v1/crawler/site/gunsan_city → crawled_page row + Qdrant chunks 삭제
#   2. DELETE /api/v1/crawler/site/wanju_county → 동일
#   3. POST /trigger/site/{code}?mode=full → BFS 결과 다 new 로 적재 (existing 없으니
#      content_hash 비교 안 함, ETag/Last-Modified 비교도 mode=full 이라 우회)
#
# 결과: BFS collected 277/131 URL 모두 success 로 적재 가능.
#
# ⚠️ DELETE 는 영구. 백업 의도 없음 — 어차피 BFS 가 다시 다 적재할 거니까.

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
echo "2) DELETE — crawled_page row + Qdrant chunks"
for code in "${SITES[@]}"; do
  resp=$(curl -sS -X DELETE "$API_BASE/crawler/site/$code" \
    -H "Authorization: Bearer $TOKEN")
  echo "   DELETE $code → $resp"
done

echo
echo "3) trigger mode=full"
for code in "${SITES[@]}"; do
  resp=$(curl -sS -X POST "$API_BASE/crawler/trigger/site/$code?mode=full" \
    -H "Authorization: Bearer $TOKEN")
  echo "   $code mode=full → $resp"
done

echo
echo "✅ 큐잉 완료. BFS 300s + URL process 5~15분 추정."
echo
echo "   진행 추적:"
echo "     PGPASSWORD='sprint26!' psql -h localhost -U admin -d customui -c \\"
echo "       \"SELECT site_code, COUNT(*) AS pages, COUNT(*) FILTER (WHERE status='success') AS ok, COUNT(*) FILTER (WHERE status='error') AS err, to_timestamp(MAX(last_crawled_at)) AT TIME ZONE 'Asia/Seoul' AS last FROM crawled_page WHERE site_code IN ('gunsan_city','wanju_county') GROUP BY site_code;\""
