#!/usr/bin/env bash
# Take a manual OpenSearch snapshot before a major push, via the agent's admin API.
# Blocks until the snapshot completes so a failed backup fails the deploy step.
#
# Usage:  deploy/os-snapshot.sh [label]
#   AGENT_URL   agent base URL (default http://localhost:17024 — run on the box so the
#               /admin IP allow-list passes as 127.0.0.1)
#   label       optional snapshot label, e.g. the release tag (default: pre-deploy)
#
# Example (in a pipeline, before rsync/restart):
#   deploy/os-snapshot.sh "pre-${RELEASE_TAG}" || { echo "backup failed, aborting deploy"; exit 1; }
set -euo pipefail

AGENT="${AGENT_URL:-http://localhost:17024}"
LABEL="${1:-pre-deploy}"

echo "→ Taking OpenSearch snapshot (label=${LABEL}) via ${AGENT}/admin/backup ..."
resp="$(curl -sS -w $'\n%{http_code}' -X POST "${AGENT}/admin/backup" \
  -H 'Content-Type: application/json' \
  -d "{\"label\":\"${LABEL}\",\"wait\":true}")"

code="$(printf '%s' "$resp" | tail -n1)"
body="$(printf '%s' "$resp" | sed '$d')"
printf '%s\n' "$body"

if [ "$code" != "200" ]; then
  echo "✗ Snapshot failed (HTTP ${code})." >&2
  exit 1
fi
echo "✓ Snapshot complete."
