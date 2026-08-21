#!/usr/bin/env bash
# One-command git-pull deploy for the GC Agent — run ON the box (no deploy-server hop).
# Pre-snapshot → fetch → hard-reset to origin → conditional pip → stamp BUILD_COMMIT →
# restart. `.env.local` (secrets) and `.venv` (bin/, lib/) are gitignored → never touched.
#
# Usage:   deploy/pull-deploy.sh [branch]
# Env:
#   APP_DIR        app root / git checkout        (default: /apps/gc_agent)
#   BRANCH         branch to deploy               (default: main, or $1)
#   SERVICE        supervisor program name        (default: ai-agent-service)
#   SKIP_SNAPSHOT  1 = skip the pre-deploy snapshot (use until the OS snapshot repo is
#                  registered; otherwise a failed backup aborts the deploy — by design)
#   SKIP_RESTART   1 = pull only, don't restart
set -euo pipefail

APP_DIR="${APP_DIR:-/apps/gc_agent}"
BRANCH="${1:-${BRANCH:-main}}"
SERVICE="${SERVICE:-ai-agent-service}"
PIP="${APP_DIR}/bin/pip"

_sha() { (sha256sum "$1" 2>/dev/null || shasum -a 256 "$1") | awk '{print $1}'; }

cd "$APP_DIR"
echo "==> GC Agent deploy | dir=${APP_DIR} branch=${BRANCH}"

# 1. Pre-deploy snapshot (the OLD, still-running agent takes it). A failed backup aborts.
if [ "${SKIP_SNAPSHOT:-0}" != "1" ]; then
  git fetch --quiet --prune origin "$BRANCH"
  target="$(git rev-parse --short "origin/${BRANCH}" 2>/dev/null || echo deploy)"
  echo "==> Pre-deploy snapshot (label=pre-${target}) ..."
  bash "${APP_DIR}/deploy/os-snapshot.sh" "pre-${target}"
else
  echo "==> Skipping pre-deploy snapshot (SKIP_SNAPSHOT=1)"
fi

# 2. Fetch + hard-reset — working tree becomes exactly origin/BRANCH.
req_before="$(_sha requirements.txt || true)"
echo "==> Fetching origin/${BRANCH} ..."
git fetch --prune origin "$BRANCH"
old="$(git rev-parse --short HEAD 2>/dev/null || echo none)"
git reset --hard "origin/${BRANCH}"
new="$(git rev-parse --short HEAD)"
echo "==> ${old} -> ${new}"
if [ "$old" = "$new" ]; then
  echo "==> Already up to date."
fi

# 3. Reinstall deps only if requirements.txt changed.
if [ "$(_sha requirements.txt || true)" != "$req_before" ]; then
  echo "==> requirements.txt changed — installing deps ..."
  "$PIP" install -q -r requirements.txt
else
  echo "==> requirements.txt unchanged — skipping pip install"
fi

# 4. Stamp the deployed commit into .env (gitignored; the app loads it via dotenv) so
#    /actuator/info reports the real build instead of "unknown".
touch .env
sed -i.bak '/^BUILD_COMMIT=/d; /^BUILD_TIME=/d' .env && rm -f .env.bak
{
  echo "BUILD_COMMIT=${new}"
  echo "BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >> .env

# 5. Restart.
if [ "${SKIP_RESTART:-0}" = "1" ]; then
  echo "==> Pulled ${new} (SKIP_RESTART=1 — not restarting)."
  exit 0
fi
echo "==> Restarting ${SERVICE} ..."
sudo supervisorctl restart "$SERVICE"

echo "==> Done. Deployed ${new}."
echo "    Verify: curl -s http://localhost:17024/actuator/info \\"
echo "      | python3 -c 'import sys,json;d=json.load(sys.stdin);print(\"commit:\",d[\"build\"][\"commit\"],\"| tools:\",d[\"runtime\"][\"tools_loaded\"])'"
