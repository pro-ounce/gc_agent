#!/usr/bin/env bash
# Poll origin and deploy when a new commit lands — for a cron job or systemd timer on the
# box. Idempotent: exits 0 with no action when already up to date. Self-locating.
#
#   deploy/autopull.sh
# Env: BRANCH (default main), APP_DIR (default = checkout root).
set -euo pipefail

_SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(dirname "$_SD")}"
BRANCH="${BRANCH:-main}"

cd "$APP_DIR"
if ! git fetch --quiet origin "$BRANCH"; then
  echo "$(date '+%F %T') autopull: git fetch failed" >&2
  exit 1
fi

LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/${BRANCH}")"
if [ "$LOCAL" = "$REMOTE" ]; then
  exit 0   # nothing new — stay quiet so the timer log doesn't fill up
fi

echo "$(date '+%F %T') autopull: ${LOCAL:0:8} -> ${REMOTE:0:8} on ${BRANCH} — deploying"
exec "$_SD/deploy.sh"   # local deploy: fetch + reset + symlink + pip + restart
