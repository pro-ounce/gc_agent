#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# build-venv.sh — build the agent's Python 3.12 venv in-place at $APP_ROOT and
# install its deps, offline from the wheelhouse when present. Automates README
# §1c so a fresh host doesn't need the copy-paste. Run AFTER the source + (for
# air-gapped) the wheelhouse are in place; run BEFORE install.sh (supervisor).
#
# Run as the SERVICE USER (gcusr), not root, so the venv is owned correctly:
#   sudo -u gcusr APP_ROOT=/apps/gc_agent ./build-venv.sh
#
# Picks a Python 3.12 interpreter in this order: pyenv's $PYVER (3.12.8, the
# repo's pin) → python3.12 on PATH → system python3 if it's 3.12. None found →
# it tells you how to get one (dnf/apt or pyenv) and stops.
#
# Env overrides:
#   APP_ROOT     app root + venv (default /apps/gc_agent — venv built in-place)
#   PYVER        pyenv version to prefer (default 3.12.8, matches .python-version)
#   WHEELHOUSE   offline wheels dir (default $APP_ROOT/wheelhouse); absent → networked pip
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

APP_ROOT="${APP_ROOT:-/apps/gc_agent}"
PYVER="${PYVER:-3.12.8}"
WHEELHOUSE="${WHEELHOUSE:-$APP_ROOT/wheelhouse}"
REQ="$APP_ROOT/requirements.txt"

die() { echo "ERROR: $*" >&2; exit 1; }

test -f "$REQ"              || die "no requirements.txt at $REQ — copy the source first (README §1b)"
test -f "$APP_ROOT/app/main.py" || die "no app/ at $APP_ROOT — copy the source first (README §1b)"

echo "==> 1. find a Python 3.12 interpreter"
PY=""
if command -v pyenv >/dev/null 2>&1 && pyenv versions --bare 2>/dev/null | grep -qx "$PYVER"; then
  PY="$(pyenv root)/versions/$PYVER/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PY="$(command -v python3.12)"
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2]==(3,12) else 1)'; then
  PY="$(command -v python3)"
else
  cat >&2 <<EOF
ERROR: no Python 3.12 found. Install one, then re-run:
  RHEL 9:   sudo dnf install -y python3.12
  Ubuntu:   sudo apt-get install -y python3.12 python3.12-venv
  pyenv:    pyenv install $PYVER      (what the repo pins; README §1c)
EOF
  exit 1
fi
echo "    using: $PY ($("$PY" --version))"

echo "==> 2. create venv in-place at $APP_ROOT (bin/, lib/ beside app/)"
if [ -x "$APP_ROOT/bin/python" ]; then
  echo "    venv already present — reusing it (delete bin/ lib/ include/ pyvenv.cfg to rebuild)"
else
  "$PY" -m venv "$APP_ROOT" --prompt=agent
fi
VPY="$APP_ROOT/bin/python"

# Offline if the wheelhouse is there; otherwise networked pip (no custom pip.conf).
if [ -d "$WHEELHOUSE" ]; then
  PIP_ARGS=(--no-index --find-links "$WHEELHOUSE")
  echo "==> 3. install deps OFFLINE from $WHEELHOUSE"
else
  PIP_ARGS=()
  echo "==> 3. no wheelhouse at $WHEELHOUSE — installing with networked pip"
fi
"$VPY" -m pip install "${PIP_ARGS[@]}" --upgrade pip
"$VPY" -m pip install "${PIP_ARGS[@]}" -r "$REQ"

echo "==> 4. sanity import"
"$VPY" -c "import fastapi, uvicorn; print('    fastapi', fastapi.__version__, '/ uvicorn', uvicorn.__version__)" \
  || die "deps did not import — check the wheelhouse matches Linux/x86_64/py3.12"

echo "==> done. Venv: $VPY"
echo "    next: wire supervisor →  sudo APP_ROOT=$APP_ROOT ./install.sh   (then edit .env.local)"
