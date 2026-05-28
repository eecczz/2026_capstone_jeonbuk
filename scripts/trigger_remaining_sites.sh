#!/bin/bash
# 5/26 02:00 KST 첫 incremental 에서 안 돌아간 site 들을 manual trigger 로 처리.
#
# 사전 조건:
#   1. sudo kill -HUP 62  로 uvicorn worker reload (config 변경 반영)
#   2. /api/v1/crawler/trigger/site/{code} 가 admin auth 요구 → JWT 발급 필요
#
# 사용:
#   bash scripts/trigger_remaining_sites.sh
#
# 동작:
#   - admin signin 으로 JWT 토큰 발급
#   - 11개 site 에 대해 POST /trigger/site/{code}?mode=incremental 호출
#   - 각 호출은 background task 로 큐잉됨 (즉시 return)

set -euo pipefail

API_BASE="http://localhost:8080/api/v1"
ADMIN_EMAIL="sprinter@mail.go.kr"
ADMIN_PASSWORD='sprint26!'

# 5/26 02:00 incremental 에서 안 돌아간 site_code 목록.
# - gunsan_city, wanju_county: config base_url 수정 직후 (오늘 priority_paths 보강)
# - 나머지 9개: 이미 잘 적재돼 있는 시군 — 일별 incremental 로 변화분 갱신만 필요
SITES=(
  "gunsan_city"
  "wanju_county"
  "iksan_city"
  "imsil_county"
  "jeongeup_city"
  "jeonju_city"
  "jinan_county"
  "muju_county"
  "namwon_city"
  "sunchang_county"
  "jangsu_county"
)

echo "1) admin JWT 발급"
TOKEN=$(curl -sS -X POST "$API_BASE/auths/signin" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin).get("token",""))')

if [ -z "$TOKEN" ]; then
  echo "   ❌ JWT 발급 실패 — admin 인증 또는 서버 응답 확인"
  exit 1
fi
echo "   ✅ JWT 발급 OK (앞 16자: ${TOKEN:0:16}…)"

echo
echo "2) 11개 site manual trigger (incremental)"
for code in "${SITES[@]}"; do
  resp=$(curl -sS -X POST "$API_BASE/crawler/trigger/site/$code?mode=incremental" \
    -H "Authorization: Bearer $TOKEN")
  echo "   $code → $resp"
done

echo
echo "✅ 11개 site 큐잉 완료."
echo "   진행 상황:"
# bash 배열을 SQL IN 리스트로: " ".join → "','"
SQL_IN=$(printf "'%s'," "${SITES[@]}")
SQL_IN="${SQL_IN%,}"
echo "     PGPASSWORD='sprint26!' psql -h localhost -U admin -d customui -c \\"
echo "       \"SELECT site_code, COUNT(*) AS pages, to_timestamp(MAX(last_crawled_at)) AT TIME ZONE 'Asia/Seoul' AS last FROM crawled_page WHERE site_code IN ($SQL_IN) GROUP BY site_code ORDER BY last DESC NULLS LAST;\""
echo "     # 또는 tail -f /var/log/owi.log"
