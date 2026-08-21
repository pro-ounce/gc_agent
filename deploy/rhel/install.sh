#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# install.sh — wire the GC Agent (ai-agent-service) into supervisor on RHEL,
# WITHOUT EPEL (host is not entitlement-registered) and WITHOUT internet.
# Supervisor is installed via pip from a shipped OFFLINE WHEELHOUSE; systemd
# runs supervisord.
#
# PREREQUISITE: the app venv is already built at $APP_ROOT (README §1 — pyenv 3.12.8),
# and the wheelhouse is unpacked at $WHEELHOUSE (default $APP_ROOT/wheelhouse).
#
# Run as root (or sudo) ON THE RHEL HOST:
#   sudo APP_ROOT=/apps/gc_agent SVC_USER=gcusr ./install.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

APP_ROOT="${APP_ROOT:-/apps/gc_agent}"
SVC_USER="${SVC_USER:-gcusr}"
SUP_VENV="${SUP_VENV:-/apps/supervisor}"                 # dedicated venv for supervisor
WHEELHOUSE="${WHEELHOUSE:-$APP_ROOT/wheelhouse}"         # offline wheels (see build-wheelhouse.sh)
LOG_DIR="/var/log/gc/ai-agent-service"
PROGRAM_CONF="/etc/supervisord.d/ai-agent-service.ini"
MAIN_CONF="/etc/supervisord.conf"
UNIT="/etc/systemd/system/supervisord.service"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Offline pip args if a wheelhouse is present; otherwise fall back to normal pip
# (assumes the host can reach an index — NO custom pip.conf is used).
if [ -d "$WHEELHOUSE" ]; then
  PIP_OFFLINE=(--no-index --find-links "$WHEELHOUSE")
  echo "    offline install from wheelhouse: $WHEELHOUSE"
else
  PIP_OFFLINE=()
  echo "    NOTE: no wheelhouse at $WHEELHOUSE — falling back to networked pip"
fi

echo "==> 1. Sanity: app venv + source present at $APP_ROOT"
test -x "$APP_ROOT/bin/python"  || { echo "ERROR: $APP_ROOT/bin/python missing — build the app venv first (README §1)"; exit 1; }
test -f "$APP_ROOT/app/main.py" || { echo "ERROR: $APP_ROOT/app/main.py missing — copy the source first"; exit 1; }
"$APP_ROOT/bin/python" -c "import uvicorn, fastapi" || { echo "ERROR: deps not installed in app venv — see README §1"; exit 1; }
echo "    app venv python: $("$APP_ROOT/bin/python" --version)"

echo "==> 2. Install supervisor via pip (no EPEL) into $SUP_VENV"
if [ ! -x "$SUP_VENV/bin/supervisord" ]; then
  python3 -m venv "$SUP_VENV"                            # system python3 (has asyncore; NOT 3.12)
  "$SUP_VENV/bin/pip" install "${PIP_OFFLINE[@]}" --upgrade pip
  "$SUP_VENV/bin/pip" install "${PIP_OFFLINE[@]}" supervisor
fi
SUPERVISORD="$SUP_VENV/bin/supervisord"
SUPERVISORCTL="$SUP_VENV/bin/supervisorctl -c $MAIN_CONF"
echo "    $("$SUP_VENV/bin/supervisord" --version) installed"

echo "==> 3. Service user: $SVC_USER"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd -r -s /sbin/nologin -d "$APP_ROOT" "$SVC_USER"

echo "==> 4. .env.local (mode 0600)"
if [ ! -f "$APP_ROOT/.env.local" ]; then
  cp "$APP_ROOT/.env.example" "$APP_ROOT/.env.local"
  echo "    created $APP_ROOT/.env.local from .env.example — EDIT IT before starting"
fi
chmod 600 "$APP_ROOT/.env.local"

echo "==> 5. Dirs + ownership"
mkdir -p "$LOG_DIR" /etc/supervisord.d /var/log/supervisor
chown -R "$SVC_USER":"$SVC_USER" "$APP_ROOT" "$LOG_DIR"

echo "==> 6. supervisor configs + systemd unit"
# Only install the main conf if absent, so we don't clobber an existing one.
[ -f "$MAIN_CONF" ] || install -m 0644 "$HERE/../supervisor/supervisord.conf" "$MAIN_CONF"
install -m 0644 "$HERE/../supervisor/ai-agent-service.conf" "$PROGRAM_CONF"
# Point the unit at the actual supervisor venv path (default in the file is /apps/supervisor).
sed "s#/apps/supervisor#${SUP_VENV}#g" "$HERE/../supervisor/supervisord.service" > "$UNIT"

echo "==> 7. Start supervisord (systemd), load program"
systemctl daemon-reload
systemctl enable --now supervisord
sleep 2
$SUPERVISORCTL reread
$SUPERVISORCTL update
$SUPERVISORCTL start ai-agent-service || $SUPERVISORCTL restart ai-agent-service

echo "==> Done. Status:"
$SUPERVISORCTL status ai-agent-service
echo "Smoke test:  curl -s http://localhost:8080/actuator/health"
