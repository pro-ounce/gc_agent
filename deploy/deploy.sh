#!/usr/bin/env bash
# GC Agent deploy via git-sync. Run it ON the server, or trigger it from a remote machine
# by passing user@host — same ergonomics as the old rsync deploy, but the box pulls from git.
#
#   deploy/deploy.sh                     # on the server (from the checkout)
#   deploy/deploy.sh gcusr@sparkbee      # from your laptop → SSHes in and deploys
#
# Env (all optional):
#   REMOTE_DIR     checkout dir on the server        (default: /apps/gc_agent)
#   BRANCH         branch to deploy                  (default: main)
#   PKG            runtime package name              (default: app_gc_agent; PKG=app skips the symlink)
#   SERVICE        supervisor program               (default: ai-agent-service)
#   SKIP_SNAPSHOT  1 = skip the pre-deploy snapshot  (default: 1 until the OS snapshot repo is registered)
set -euo pipefail

TARGET="${1:-${TARGET:-}}"
REMOTE_DIR="${REMOTE_DIR:-/apps/gc_agent}"
BRANCH="${BRANCH:-main}"
PKG="${PKG:-app_gc_agent}"
SERVICE="${SERVICE:-ai-agent-service}"
SKIP_SNAPSHOT="${SKIP_SNAPSHOT:-1}"

# The on-box deploy — pure git-sync. Reads config from D_* env so it runs identically
# whether called locally or shipped over SSH via `declare -f`.
_deploy() {
  set -euo pipefail
  cd "${D_DIR:?}"
  echo "==> $(hostname):${D_DIR}  branch ${D_BRANCH}"

  # 1. Pre-deploy snapshot (the running agent takes it). A failed backup aborts the deploy.
  if [ "${D_SNAP}" != "1" ] && [ -x deploy/os-snapshot.sh ]; then
    echo "==> pre-deploy snapshot"
    bash deploy/os-snapshot.sh "pre-$(git rev-parse --short "origin/${D_BRANCH}" 2>/dev/null || echo deploy)"
  fi

  # 2. Fetch + hard-reset — working tree becomes exactly origin/BRANCH.
  git fetch --prune origin "${D_BRANCH}"
  OLD="$(git rev-parse --short HEAD 2>/dev/null || echo none)"
  git reset --hard "origin/${D_BRANCH}"
  NEW="$(git rev-parse --short HEAD)"
  echo "==> ${OLD} -> ${NEW}"

  # 3. Runtime package name: git ships app/; expose PKG as a symlink -> app (never a
  #    rename, which would fight git). Idempotent; survives future resets (.git/info/exclude).
  if [ "${D_PKG}" != "app" ] && [ -d app ] && [ ! -L "${D_PKG}" ]; then
    rm -rf "${D_PKG}"; ln -s app "${D_PKG}"; echo "==> linked ${D_PKG} -> app"
    grep -qxF "/${D_PKG}" .git/info/exclude 2>/dev/null || echo "/${D_PKG}" >> .git/info/exclude
  fi

  # 4. Deps only if requirements changed (or first deploy).
  if [ "${OLD}" = none ] || ! git diff --quiet "${OLD}" "${NEW}" -- requirements.txt; then
    echo "==> pip install (requirements changed / fresh)"
    ./bin/pip install -q -r requirements.txt
  fi

  # 5. Stamp the deployed commit into .env (gitignored) so /actuator/info shows the build.
  touch .env
  sed -i.bak '/^BUILD_COMMIT=/d; /^BUILD_TIME=/d' .env && rm -f .env.bak
  printf 'BUILD_COMMIT=%s\nBUILD_TIME=%s\n' "${NEW}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> .env

  # 6. Restart.
  sudo supervisorctl restart "${D_SVC}" 2>/dev/null \
    || sudo /apps/supervisor/bin/supervisorctl restart "${D_SVC}"
  echo "==> deployed ${NEW} on $(hostname)"
}

_ENV="D_DIR='${REMOTE_DIR}' D_BRANCH='${BRANCH}' D_PKG='${PKG}' D_SVC='${SERVICE}' D_SNAP='${SKIP_SNAPSHOT}'"

if [ -n "${TARGET}" ]; then
  echo "→ remote deploy → ${TARGET}:${REMOTE_DIR} (branch ${BRANCH})"
  ssh "${TARGET}" "$(declare -f _deploy); ${_ENV} _deploy"
else
  echo "→ local deploy (${REMOTE_DIR}, branch ${BRANCH})"
  eval "${_ENV} _deploy"
fi
