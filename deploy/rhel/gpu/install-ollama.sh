#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# install-ollama.sh — install Ollama (the LLM runtime that serves the agent's
# model, default mistral-small:24b) as a systemd service on the GPU host.
#
# Manual install in BOTH the online and offline cases — same binary, same unit —
# so the air-gapped RHEL path is identical to the in-house Ubuntu rehearsal and
# we fully control the bind address, models dir and restart policy. (We do NOT
# pipe ollama.com/install.sh to a shell: it needs internet and hides the config.)
#
#   online  : the script fetches ollama-linux-amd64.tgz itself
#   offline : point it at a pre-downloaded tarball with OLLAMA_TGZ=/path/...tgz
#             (grab it on a connected host from
#              https://ollama.com/download/ollama-linux-amd64.tgz, or a pinned
#              https://github.com/ollama/ollama/releases/download/v<VER>/ollama-linux-amd64.tgz)
#
# GPU: once the NVIDIA driver is up (install-nvidia.sh), Ollama uses the GPU
# automatically — it ships its own bundled CUDA runtime, so no CUDA toolkit is
# needed. Install the driver FIRST, or Ollama falls back to (slow) CPU.
#
# Usage (run as root on the GPU host):
#   sudo ./install-ollama.sh install                      # install + start the service
#   sudo OLLAMA_PULL=1 ./install-ollama.sh install        # also pull OLLAMA_MODEL (needs internet)
#   sudo ./install-ollama.sh verify
#
# Env overrides:
#   OLLAMA_TGZ          pre-downloaded tarball → offline install
#   OLLAMA_BIND         listen addr (default 127.0.0.1:11434; use 0.0.0.0:11434
#                       ONLY if the agent runs on a DIFFERENT host — then
#                       firewall it to the agent's IP, it has no auth of its own)
#   OLLAMA_MODELS_DIR   model store (default /usr/share/ollama/.ollama/models;
#                       point at a data volume for the ~14 GB＋ of weights)
#   OLLAMA_MODEL        model to pull with OLLAMA_PULL=1 (default mistral-small:24b)
#   OLLAMA_PULL=1       pull the model after starting (online only)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

CMD="${1:-}"
OLLAMA_TGZ="${OLLAMA_TGZ:-}"
OLLAMA_BIND="${OLLAMA_BIND:-127.0.0.1:11434}"
OLLAMA_MODELS_DIR="${OLLAMA_MODELS_DIR:-/usr/share/ollama/.ollama/models}"
OLLAMA_MODEL="${OLLAMA_MODEL:-mistral-small:24b}"
OLLAMA_PULL="${OLLAMA_PULL:-0}"

UNIT=/etc/systemd/system/ollama.service
DL_URL="https://ollama.com/download/ollama-linux-amd64.tgz"
LOCAL_PORT="${OLLAMA_BIND##*:}"                 # 11434 from host:port

die() { echo "ERROR: $*" >&2; exit 1; }
need_root() { [ "$(id -u)" = 0 ] || die "run as root (sudo)"; }

fetch_tarball() {
  local dest="$1"
  if [ -n "$OLLAMA_TGZ" ]; then
    echo "    offline: using $OLLAMA_TGZ"
    test -f "$OLLAMA_TGZ" || die "OLLAMA_TGZ not found: $OLLAMA_TGZ"
    cp "$OLLAMA_TGZ" "$dest"
  else
    echo "    online: downloading $DL_URL"
    command -v curl >/dev/null 2>&1 || die "curl missing and no OLLAMA_TGZ given"
    curl -fSL "$DL_URL" -o "$dest" || die "download failed — on an air-gapped host set OLLAMA_TGZ"
  fi
}

do_install() {
  need_root
  [ "$(uname -m)" = x86_64 ] || die "this tarball is amd64; host is $(uname -m)"
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "WARNING: no nvidia-smi — install the driver first (install-nvidia.sh) or Ollama runs on CPU."
  fi

  echo "==> 1. ollama service user"
  id -u ollama >/dev/null 2>&1 || useradd -r -s /sbin/nologin -U -d /usr/share/ollama ollama

  echo "==> 2. install binary to /usr/local (bin/ + lib/ with bundled CUDA libs)"
  local tmp; tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  fetch_tarball "$tmp/ollama.tgz"
  tar -C /usr/local -xzf "$tmp/ollama.tgz"
  test -x /usr/local/bin/ollama || die "extract did not yield /usr/local/bin/ollama"
  echo "    $(/usr/local/bin/ollama --version 2>/dev/null || echo 'ollama installed')"

  echo "==> 3. models dir: $OLLAMA_MODELS_DIR"
  mkdir -p "$OLLAMA_MODELS_DIR"
  chown -R ollama:ollama "$OLLAMA_MODELS_DIR"
  # make sure the service user can traverse into it (but don't chown parents we don't own)
  chmod o+rx "$OLLAMA_MODELS_DIR" 2>/dev/null || true

  echo "==> 4. systemd unit (bind $OLLAMA_BIND)"
  cat >"$UNIT" <<EOF
[Unit]
Description=Ollama Service
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/ollama serve
User=ollama
Group=ollama
Restart=always
RestartSec=3
Environment="OLLAMA_HOST=${OLLAMA_BIND}"
Environment="OLLAMA_MODELS=${OLLAMA_MODELS_DIR}"
Environment="PATH=/usr/local/bin:/usr/bin:/bin"
# keep the model resident so the first chat after idle isn't a cold reload:
Environment="OLLAMA_KEEP_ALIVE=-1"

[Install]
WantedBy=multi-user.target
EOF

  echo "==> 5. start"
  systemctl daemon-reload
  systemctl enable --now ollama

  echo "==> 6. wait for the API on 127.0.0.1:${LOCAL_PORT}"
  local i
  for i in $(seq 1 30); do
    if curl -fs "http://127.0.0.1:${LOCAL_PORT}/api/version" >/dev/null 2>&1; then break; fi
    sleep 1
  done
  curl -fs "http://127.0.0.1:${LOCAL_PORT}/api/version" >/dev/null 2>&1 \
    || die "service did not come up — check: journalctl -u ollama -n50"

  if [ "$OLLAMA_PULL" = 1 ]; then
    echo "==> 7. pull $OLLAMA_MODEL (this is several GB)"
    OLLAMA_HOST="127.0.0.1:${LOCAL_PORT}" /usr/local/bin/ollama pull "$OLLAMA_MODEL" \
      || die "pull failed — on an air-gapped host import the model instead (see README §Offline model)"
  else
    echo "==> model NOT pulled (OLLAMA_PULL unset). Online:  OLLAMA_HOST=127.0.0.1:${LOCAL_PORT} ollama pull $OLLAMA_MODEL"
    echo "    Air-gapped: copy the models dir from a connected host (README §Offline model)."
  fi
  echo "==> done. Verify:  sudo $0 verify"
}

do_verify() {
  systemctl is-active --quiet ollama || die "ollama.service not active — journalctl -u ollama"
  echo "==> service: active"
  echo "==> version: $(curl -fs "http://127.0.0.1:${LOCAL_PORT}/api/version" || echo '??')"
  echo "==> models:"
  OLLAMA_HOST="127.0.0.1:${LOCAL_PORT}" /usr/local/bin/ollama list || true
  echo "==> GPU use (from the service log):"
  if journalctl -u ollama --no-pager 2>/dev/null | grep -iE 'inference compute|library=cuda|looking for compatible gpus' | tail -3; then :; fi
  echo "    (empty above = Ollama saw no GPU → it will run on CPU; check install-nvidia.sh verify)"
  echo "==> live check: run a prompt in one shell and watch VRAM in another —"
  echo "    OLLAMA_HOST=127.0.0.1:${LOCAL_PORT} ollama run ${OLLAMA_MODEL} 'hello'   # then: nvidia-smi"
}

case "$CMD" in
  install) do_install ;;
  verify)  do_verify  ;;
  *) echo "usage: $0 {install|verify}   (env: OLLAMA_TGZ OLLAMA_BIND OLLAMA_MODELS_DIR OLLAMA_MODEL OLLAMA_PULL)" >&2; exit 2 ;;
esac
