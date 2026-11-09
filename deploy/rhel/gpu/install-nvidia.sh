#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# install-nvidia.sh — install the NVIDIA driver (+ optional CUDA toolkit) so the
# GPU box can serve the agent's LLM (Ollama / mistral-small:24b) on the GPU.
#
# Distro-aware so the SAME script we rehearse in-house on Ubuntu is the one we
# run on the eventual RHEL host:
#   • RHEL / Rocky / Alma 8|9  — NVIDIA CUDA dnf repo, PRECOMPILED kmod module
#     stream by default (no EPEL, no DKMS, no compiler — matches this fleet's
#     air-gapped, EPEL-free convention; see ../README.md §2). `--dkms` switches
#     to the DKMS stream for hosts that pin a non-stock kernel.
#   • Ubuntu / Debian — NVIDIA CUDA apt repo, `cuda-drivers` (DKMS). The in-house
#     test path.
#
# Targets AWS GPU instances g5 (A10G) / g6 (L4) / g6e (L40S): the PUBLIC data-
# center driver, NOT the AWS GRID/vWS driver — compute/inference needs no license.
# CUDA toolkit is NOT installed unless `--with-toolkit`: Ollama bundles its own
# CUDA runtime and the kernel driver already provides libcuda, so inference only
# needs the driver.
#
# Usage (run as root on the GPU host):
#   sudo ./install-nvidia.sh install            # install driver, then REBOOT
#   sudo ./install-nvidia.sh verify             # after reboot: prove the GPU is up
#   sudo ./install-nvidia.sh install --with-toolkit --dkms
#
# Air-gapped RHEL (no internet): build an offline RPM bundle first on a connected
# RHEL host of the SAME major version with build-nvidia-bundle.sh, ship it, then
#   sudo GPU_BUNDLE_DIR=/path/to/nvidia-bundle ./install-nvidia.sh install
#
# Env overrides:
#   GPU_DRIVER_STREAM   RHEL module stream (default: latest  → precompiled kmod;
#                       latest-dkms is forced by --dkms)
#   GPU_BUNDLE_DIR      local RPM repo dir for the offline RHEL path
#   GPU_AUTO_REBOOT=1   reboot automatically at the end of `install`
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

CMD="${1:-}"; shift || true
WITH_TOOLKIT=0
USE_DKMS=0
for arg in "$@"; do
  case "$arg" in
    --with-toolkit) WITH_TOOLKIT=1 ;;
    --dkms)         USE_DKMS=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

GPU_BUNDLE_DIR="${GPU_BUNDLE_DIR:-}"
GPU_AUTO_REBOOT="${GPU_AUTO_REBOOT:-0}"
# Precompiled kmod stream by default (no EPEL/DKMS); --dkms overrides.
if [ "$USE_DKMS" = 1 ]; then
  GPU_DRIVER_STREAM="${GPU_DRIVER_STREAM:-latest-dkms}"
else
  GPU_DRIVER_STREAM="${GPU_DRIVER_STREAM:-latest}"
fi

die() { echo "ERROR: $*" >&2; exit 1; }
need_root() { [ "$(id -u)" = 0 ] || die "run as root (sudo)"; }

# shellcheck disable=SC1091
. /etc/os-release
OS_ID="${ID:-unknown}"
OS_MAJOR="${VERSION_ID%%.*}"

family() {
  case "$OS_ID" in
    rhel|rocky|almalinux|centos|ol) echo rhel ;;
    ubuntu|debian)                  echo deb  ;;
    *) echo "unsupported"           ;;
  esac
}

preflight() {
  [ "$(uname -m)" = x86_64 ] || die "these AWS GPU instances are x86_64; got $(uname -m)"
  if ! lspci 2>/dev/null | grep -qi 'nvidia'; then
    echo "WARNING: no NVIDIA device on the PCI bus — is this actually a GPU instance?"
  fi
}

# ── already installed? (idempotent) ──────────────────────────────────────────
already_up() { command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; }

# ── RHEL family ───────────────────────────────────────────────────────────────
rhel_install() {
  echo "==> RHEL ${OS_MAJOR}: driver stream nvidia-driver:${GPU_DRIVER_STREAM}"
  dnf -y install dnf-plugins-core

  if [ "$USE_DKMS" = 1 ]; then
    echo "    --dkms: installing kernel-devel/headers + build tools (needs EPEL for dkms)"
    dnf -y install "kernel-devel-$(uname -r)" "kernel-headers-$(uname -r)" gcc make || \
      die "kernel-devel for $(uname -r) not found — update the kernel or add the matching devel RPM"
    dnf -y install dkms || die "dkms unavailable — it lives in EPEL; precompiled (drop --dkms) avoids this"
  fi

  if [ -n "$GPU_BUNDLE_DIR" ]; then
    echo "    offline: local RPM repo at $GPU_BUNDLE_DIR"
    [ -d "$GPU_BUNDLE_DIR/repodata" ] || die "no repodata in $GPU_BUNDLE_DIR — rebuild with build-nvidia-bundle.sh"
    cat >/etc/yum.repos.d/nvidia-local.repo <<EOF
[nvidia-local]
name=NVIDIA CUDA (offline bundle)
baseurl=file://${GPU_BUNDLE_DIR}
enabled=1
gpgcheck=0
module_hotfixes=1
EOF
    REPOFLAGS=(--disablerepo='*' --enablerepo=nvidia-local)
  else
    echo "    online: adding NVIDIA CUDA repo for rhel${OS_MAJOR}"
    dnf config-manager --add-repo \
      "https://developer.download.nvidia.com/compute/cuda/repos/rhel${OS_MAJOR}/x86_64/cuda-rhel${OS_MAJOR}.repo"
    dnf clean expire-cache
    REPOFLAGS=()
  fi

  dnf -y "${REPOFLAGS[@]}" module install "nvidia-driver:${GPU_DRIVER_STREAM}"
  [ "$WITH_TOOLKIT" = 1 ] && dnf -y "${REPOFLAGS[@]}" install cuda-toolkit

  echo "==> regenerating initramfs (nouveau is blacklisted by the driver RPM)"
  dracut -f
  systemctl enable nvidia-persistenced 2>/dev/null || true
}

# ── Debian family (Ubuntu — the in-house rehearsal) ───────────────────────────
deb_install() {
  local repo_id="${OS_ID}${VERSION_ID//./}"     # ubuntu22.04 -> ubuntu2204
  echo "==> ${OS_ID} ${VERSION_ID}: NVIDIA CUDA apt repo (${repo_id}), cuda-drivers"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y wget "linux-headers-$(uname -r)" build-essential dkms
  wget -qO /tmp/cuda-keyring.deb \
    "https://developer.download.nvidia.com/compute/cuda/repos/${repo_id}/x86_64/cuda-keyring_1.1-1_all.deb"
  dpkg -i /tmp/cuda-keyring.deb
  apt-get update
  apt-get install -y cuda-drivers
  [ "$WITH_TOOLKIT" = 1 ] && apt-get install -y cuda-toolkit
  update-initramfs -u
}

do_install() {
  need_root; preflight
  if already_up; then
    echo "==> driver already loaded; nothing to do:"; nvidia-smi
    return 0
  fi
  case "$(family)" in
    rhel) rhel_install ;;
    deb)  deb_install  ;;
    *)    die "unsupported distro: $OS_ID $VERSION_ID" ;;
  esac
  echo
  echo "==> driver packages installed. A REBOOT is required to unload nouveau and load nvidia."
  if [ "$GPU_AUTO_REBOOT" = 1 ]; then
    echo "    GPU_AUTO_REBOOT=1 → rebooting now"; sleep 2; reboot
  else
    echo "    reboot, then verify:  sudo $0 verify"
  fi
}

do_verify() {
  command -v nvidia-smi >/dev/null 2>&1 || die "nvidia-smi not found — driver not installed, or reboot pending"
  nvidia-smi >/dev/null 2>&1 || die "nvidia-smi present but the kernel module isn't loaded — did you reboot?"
  echo "==> GPU(s):"
  nvidia-smi --query-gpu=index,name,driver_version,memory.total,memory.used --format=csv
  echo "==> CUDA driver API version (from the driver):"
  nvidia-smi | awk -F'CUDA Version: ' '/CUDA Version/{print "    "$2}' | sed 's/ *|.*//'
  nvidia-smi -pm 1 >/dev/null 2>&1 && echo "    persistence mode: ON" || echo "    persistence mode: could not enable (non-fatal)"
  if command -v nvcc >/dev/null 2>&1; then
    echo "==> CUDA toolkit: $(nvcc --version | awk '/release/{print $5,$6}')"
  else
    echo "==> CUDA toolkit not installed (expected — Ollama bundles its own runtime)"
  fi
  if command -v ollama >/dev/null 2>&1; then
    echo "==> Ollama is installed — after 'ollama run', confirm GPU use with:  nvidia-smi  (watch memory.used rise)"
  fi
  echo "==> OK: GPU is visible and usable."
}

case "$CMD" in
  install) do_install ;;
  verify)  do_verify  ;;
  *) echo "usage: $0 {install|verify} [--with-toolkit] [--dkms]" >&2; exit 2 ;;
esac
