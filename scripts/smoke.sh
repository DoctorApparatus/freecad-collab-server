#!/usr/bin/env bash
# REST smoke test. Needs a running server with COLLAB_ADMIN_TOKEN set.
# Usage: COLLAB_ADMIN_TOKEN=... ./scripts/smoke.sh [base_url]
set -euo pipefail

base="${1:-http://127.0.0.1:14008}"
admin="${COLLAB_ADMIN_TOKEN:?set COLLAB_ADMIN_TOKEN}"
suffix="$(date +%s)"

need() { command -v "$1" >/dev/null || { echo "missing dependency: $1" >&2; exit 1; }; }
need curl
need jq

api() {
    local method="$1" path="$2" token="$3"; shift 3
    curl -fsS -X "$method" "$base$path" -H "Authorization: Bearer $token" "$@"
}

echo "1/6 readiness"
curl -fsS "$base/readyz" | jq -e '.status == "ready"' >/dev/null

echo "2/6 create users"
alice_token="$(api POST /v1/admin/users "$admin" -H 'content-type: application/json' \
    -d "{\"name\":\"alice-$suffix\"}" | jq -r .token.token)"
api POST /v1/admin/users "$admin" -H 'content-type: application/json' \
    -d "{\"name\":\"bob-$suffix\"}" >/dev/null

echo "3/6 create document"
doc="$(api POST /v1/documents "$alice_token" -H 'content-type: application/json' \
    -d '{"name":"smoke"}' | jq -r .id)"

echo "4/6 add member"
api PUT "/v1/documents/$doc/members" "$alice_token" -H 'content-type: application/json' \
    -d "{\"user_name\":\"bob-$suffix\",\"role\":\"editor\"}" | jq -e '.role == "editor"' >/dev/null

echo "5/6 snapshot round trip"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
printf 'PK\003\004smoke' > "$tmp/in.FCStd"
api PUT "/v1/documents/$doc/snapshot?seq=0" "$alice_token" --data-binary "@$tmp/in.FCStd" >/dev/null
api GET "/v1/documents/$doc/snapshot" "$alice_token" -o "$tmp/out.FCStd"
cmp -s "$tmp/in.FCStd" "$tmp/out.FCStd"

echo "6/6 cleanup"
api DELETE "/v1/documents/$doc" "$alice_token" >/dev/null

echo "smoke test passed (document $doc)"
