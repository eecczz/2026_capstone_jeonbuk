#!/bin/bash
# SPA playwright fallback 검증 — stub 1건만 있는 SPA site 한 곳 trigger.
# 결과로 stub 1건 → 100+ 건 으로 증가하면 fallback 동작 검증 완료.
#
# 사전 조건: owi-restart 로 worker reload (v5 코드 + playwright_engine 로딩)
#
# 사용:
#   bash scripts/trigger_spa_test.sh [site_code]
#   기본 검증 site = jcid (전북정보문화산업진흥원)

set -euo pipefail

SITE="${1:-jcid}"
API_BASE="http://localhost:8080/api/v1"
ADMIN_EMAIL="sprinter@mail.go.kr"
ADMIN_PASSWORD='sprint26!'

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
echo "2) $SITE 검증 trigger (incremental)"
resp=$(curl -sS -X POST "$API_BASE/crawler/trigger/site/$SITE?mode=incremental" \
  -H "Authorization: Bearer $TOKEN")
echo "   $SITE → $resp"

echo
echo "✅ 큐잉 완료."
echo "   playwright fallback 은 매 URL ~3초 → 작은 size 사이트라도 1~5분 대기"
echo
echo "   진행 추적:"
echo "     # 카운트"
echo "     PGPASSWORD='sprint26!' psql -h localhost -U admin -d customui -c \\"
echo "       \"SELECT COUNT(*) AS pages, COUNT(*) FILTER (WHERE status='success') AS ok, to_timestamp(MAX(last_crawled_at)) AT TIME ZONE 'Asia/Seoul' AS last FROM crawled_page WHERE site_code='$SITE';\""
echo
echo "     # playwright 로그 (loader 마커)"
echo "     sudo tail -f /var/log/owi.log | grep -E 'playwright|crawl4ai empty'"
