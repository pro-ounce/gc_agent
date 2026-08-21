#!/usr/bin/env bash
# Send a chat turn from the box for testing — no browser/gateway needed.
# Mints a short-lived platform token via the agent's own signing key
# (mint_service_token, iss=GC360) and posts it to the chat endpoint with auth.
#
# Usage:   deploy/chat-test.sh "your message" [session_id]
# Env:
#   APP_DIR      app checkout / venv root      (default: /apps/agent)
#   AGENT_PKG    python package name           (default: auto — app or app_<name>)
#   AGENT_URL    agent base URL                (default: http://localhost:17024)
#   AGENT_PATH   chat endpoint                 (default: /api/chat)
set -euo pipefail

APP_DIR="${APP_DIR:-/apps/agent}"
AGENT="${AGENT_URL:-http://localhost:17024}"
PATH_="${AGENT_PATH:-/api/chat}"
PY="${APP_DIR}/bin/python"
MSG="${1:-hello there}"
SID="${2:-cli-test}"

# Detect the package name (app in the repo, app_<name> after the deploy rename).
PKG="${AGENT_PKG:-}"
if [ -z "$PKG" ]; then
  for d in app app_*; do
    [ -f "$APP_DIR/$d/main.py" ] && { PKG="$d"; break; }
  done
  PKG="${PKG:-app}"
fi

# Mint a token (skip building the whole app — we only need config + jwt_handler).
TOKEN="$(cd "$APP_DIR" && APP_SKIP_INIT=1 "$PY" -c "from ${PKG}.rbac.jwt_handler import mint_service_token; print(mint_service_token() or '')")"
if [ -z "$TOKEN" ]; then
  echo "✗ mint failed — check GC_USER_JWT_SECRET is set and GC_MINT_DISCOVERY_TOKEN != false in .env.local" >&2
  exit 1
fi

BODY="$(python3 -c 'import json,sys; print(json.dumps({"session_id": sys.argv[1], "message": sys.argv[2]}))' "$SID" "$MSG")"
echo "→ POST ${AGENT}${PATH_}  (as $(cd "$APP_DIR" && APP_SKIP_INIT=1 "$PY" -c "from ${PKG}.commons.config import cfg; print(cfg.GC_SERVICE_USERNAME)" 2>/dev/null || echo service))"
curl -s "${AGENT}${PATH_}" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/json' \
  -d "$BODY" | python3 -m json.tool
