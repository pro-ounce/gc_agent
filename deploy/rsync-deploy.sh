#!/usr/bin/env bash
# Deploy to the target via rsync, mapping the local `app/` package to <PKG> on the target
# (the shared convention: app_<appname>). The local package stays named `app/`.
#
# Usage:   TARGET=gcusr@sparkbee deploy/rsync-deploy.sh
# Env:
#   TARGET        user@host of the target server            (required)
#   TARGET_ROOT   deploy root on the target                 (default: /apps/gc_agent)
#   PKG           package dir name on the target            (default: app_gc_agent)
#   SERVICE       supervisor program to restart after       (default: ai-agent-service)
#   NO_RESTART    1 = sync only, don't restart
set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$_SCRIPT_DIR")"                      # local checkout root
TARGET="${TARGET:?set TARGET=user@host}"
TROOT="${TARGET_ROOT:-/apps/gc_agent}"
PKG="${PKG:-app_gc_agent}"
SERVICE="${SERVICE:-ai-agent-service}"
COMMON=(-av --exclude='__pycache__' --exclude='.pytest_cache')

echo "→ package : $ROOT/app/  →  ${TARGET}:${TROOT}/${PKG}/"
rsync "${COMMON[@]}" --delete "$ROOT/app/" "${TARGET}:${TROOT}/${PKG}/"

# Everything else EXCEPT the package (renamed above) and secrets/venv (must survive).
# No --delete here: never remove the target's .env.local, venv, or the renamed package.
echo "→ rest    : $ROOT/  →  ${TARGET}:${TROOT}/  (excluding app/, secrets, venv)"
rsync "${COMMON[@]}" \
  --exclude='/app' --exclude='.env.local' --exclude='.env' \
  --exclude='.venv' --exclude='/bin' --exclude='/lib' --exclude='/lib64' \
  --exclude='.git' --exclude="/${PKG}" \
  "$ROOT/" "${TARGET}:${TROOT}/"

if [ "${NO_RESTART:-0}" = "1" ]; then
  echo "✓ synced (NO_RESTART=1)."
  exit 0
fi
echo "→ restart ${SERVICE} on ${TARGET}"
ssh "$TARGET" "sudo supervisorctl restart ${SERVICE} || sudo /apps/supervisor/bin/supervisorctl restart ${SERVICE}"
echo "✓ deployed."
