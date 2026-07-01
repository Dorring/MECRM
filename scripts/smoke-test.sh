#!/usr/bin/env bash
# Real smoke test: register tenant+user -> login -> create lead -> list leads.
# Exit non-zero on any failure. Designed to run inside a container with curl.
# Usage (compose): docker-compose --profile smoke-test run --rm smoke-test
# Usage (host):    bash scripts/smoke-test.sh http://localhost:4000
set -euo pipefail

GATEWAY="${1:-http://gateway:4000}"
TENANT_SLUG="smoke-$(date +%s)"
EMAIL="${TENANT_SLUG}@example.com"
PASSWORD="SmokePass123!"

echo "SMOKE: target=$GATEWAY tenant=$TENANT_SLUG"

# 1. Register a new tenant + user
echo "SMOKE: register"
REG=$(curl -s -o /tmp/reg.json -w "%{http_code}" -X POST "$GATEWAY/api/v1/auth/register" \
  -H "Content-Type: application/json" \
  -d "{\"tenantName\":\"Smoke Co\",\"tenantSlug\":\"$TENANT_SLUG\",\"name\":\"Smoke User\",\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}")
echo "SMOKE: register -> HTTP $REG"
[ "$REG" = "201" ] || [ "$REG" = "200" ] || { echo "FAIL: register"; cat /tmp/reg.json; exit 1; }

# 2. Login
echo "SMOKE: login"
LOGIN=$(curl -s -o /tmp/login.json -w "%{http_code}" -X POST "$GATEWAY/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"tenantSlug\":\"$TENANT_SLUG\",\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}")
echo "SMOKE: login -> HTTP $LOGIN"
[ "$LOGIN" = "200" ] || [ "$LOGIN" = "201" ] || { echo "FAIL: login"; cat /tmp/login.json; exit 1; }
TOKEN=$(grep -oE '"accessToken"[[:space:]]*:[[:space:]]*"[^"]+"' /tmp/login.json | head -1 | sed -E 's/.*"([^"]+)"$/\1/')
[ -n "$TOKEN" ] || { echo "FAIL: no accessToken in login response"; cat /tmp/login.json; exit 1; }
echo "SMOKE: token acquired"

# 3. Authenticated write: create a lead
echo "SMOKE: create lead"
CREATE=$(curl -s -o /tmp/create.json -w "%{http_code}" -X POST "$GATEWAY/api/v1/leads" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"name":"Smoke Lead","email":"lead@example.com","company":"SmokeCo","status":"new"}')
echo "SMOKE: create lead -> HTTP $CREATE"
[ "$CREATE" = "201" ] || [ "$CREATE" = "200" ] || [ "$CREATE" = "202" ] || { echo "FAIL: create lead"; cat /tmp/create.json; exit 1; }

# 4. Authenticated read: list leads, expect the created lead present
echo "SMOKE: list leads"
LIST=$(curl -s -o /tmp/list.json -w "%{http_code}" -X GET "$GATEWAY/api/v1/leads?search=Smoke" \
  -H "Authorization: Bearer $TOKEN")
echo "SMOKE: list leads -> HTTP $LIST"
[ "$LIST" = "200" ] || { echo "FAIL: list leads"; cat /tmp/list.json; exit 1; }
grep -q "Smoke Lead" /tmp/list.json || { echo "FAIL: created lead not found in list"; cat /tmp/list.json; exit 1; }

# 5. Negative: no token -> 401
echo "SMOKE: unauthorized request"
NOAUTH=$(curl -s -o /dev/null -w "%{http_code}" -X GET "$GATEWAY/api/v1/leads")
echo "SMOKE: no-token -> HTTP $NOAUTH (expect 401)"
[ "$NOAUTH" = "401" ] || { echo "FAIL: unauthenticated request was not rejected (got $NOAUTH)"; exit 1; }

echo "SMOKE: ALL CHECKS PASSED"
