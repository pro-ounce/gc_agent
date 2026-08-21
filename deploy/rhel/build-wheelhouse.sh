#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# build-wheelhouse.sh — collect ALL wheels for an offline (air-gapped) install.
#
# Run this on a CONNECTED host that MATCHES prod: Linux x86_64 + Python 3.12.
# (Same OS/arch/python as the RHEL target — compiled wheels like pydantic-core,
#  uvloop, httptools, watchfiles are platform+abi specific.)
#
#   ./build-wheelhouse.sh                 # uses ../../requirements.txt
#   OUT=/tmp/wh ./build-wheelhouse.sh
#
# Produces:  ./wheelhouse/  and  ./wheelhouse.tar.gz  → ship the tarball to prod,
# unpack it to /apps/gc_agent/wheelhouse, then run install.sh (offline).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQ="${REQ:-$HERE/../../requirements.txt}"
OUT="${OUT:-$HERE/wheelhouse}"

test -f "$REQ" || { echo "ERROR: requirements not found: $REQ"; exit 1; }

echo "==> Python: $(python3 --version)   (must be 3.12.x, Linux x86_64)"
case "$(python3 -c 'import sys,platform;print(sys.version_info[:2],platform.machine(),platform.system())')" in
  *"(3, 12)"*x86_64*Linux*) : ;;
  *) echo "!! WARNING: this host is not Linux/x86_64/py3.12 — the wheels may not match prod." ;;
esac

mkdir -p "$OUT"
echo "==> Downloading app dependencies from $REQ"
python3 -m pip download -r "$REQ" -d "$OUT"

echo "==> Downloading bootstrap + supervisor (universal wheels)"
python3 -m pip download pip setuptools wheel supervisor -d "$OUT"

echo "==> Packaging"
tar czf "$HERE/wheelhouse.tar.gz" -C "$(dirname "$OUT")" "$(basename "$OUT")"
echo "==> Done: $(find "$OUT" -type f | wc -l) files → $HERE/wheelhouse.tar.gz"
echo "    Ship it, then on prod:  tar xzf wheelhouse.tar.gz -C /apps/gc_agent/  (→ /apps/gc_agent/wheelhouse)"
