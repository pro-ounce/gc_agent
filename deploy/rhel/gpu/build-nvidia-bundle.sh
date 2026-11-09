#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# build-nvidia-bundle.sh — collect the NVIDIA driver RPMs for an OFFLINE
# (air-gapped) RHEL install. The GPU analogue of build-wheelhouse.sh.
#
# ONLY needed when the RHEL GPU host has no internet (like sparkbee). If the AWS
# host has NAT egress, skip this entirely and let install-nvidia.sh pull straight
# from NVIDIA's repo.
#
# Run on a CONNECTED RHEL host of the SAME major version as the target (RPMs and
# the precompiled kmod ABI are tied to the RHEL major release). Needs
# `dnf-plugins-core` and `createrepo_c`.
#
#   ./build-nvidia-bundle.sh                 # driver only (precompiled kmod stream)
#   WITH_TOOLKIT=1 ./build-nvidia-bundle.sh  # also pull the full CUDA toolkit (~GBs)
#   STREAM=latest-dkms ./build-nvidia-bundle.sh
#
# Produces:  ./nvidia-bundle/ (a self-contained dnf repo) + ./nvidia-bundle.tar.gz.
# Ship the tarball, unpack it on the air-gapped host, then:
#   sudo GPU_BUNDLE_DIR=/path/to/nvidia-bundle ./install-nvidia.sh install
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${OUT:-$HERE/nvidia-bundle}"
STREAM="${STREAM:-latest}"              # precompiled kmod by default; latest-dkms to compile on target
WITH_TOOLKIT="${WITH_TOOLKIT:-0}"

# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}" in rhel|rocky|almalinux|centos|ol) : ;; *) echo "ERROR: run on a RHEL-family host (got ${ID:-unknown})"; exit 1 ;; esac
MAJOR="${VERSION_ID%%.*}"
echo "==> target: rhel${MAJOR} x86_64, stream nvidia-driver:${STREAM}"

command -v createrepo_c >/dev/null 2>&1 || { echo "ERROR: createrepo_c missing — dnf install createrepo_c"; exit 1; }

dnf -y install dnf-plugins-core
dnf config-manager --add-repo \
  "https://developer.download.nvidia.com/compute/cuda/repos/rhel${MAJOR}/x86_64/cuda-rhel${MAJOR}.repo"
dnf clean expire-cache

mkdir -p "$OUT"
echo "==> downloading driver module RPMs (+deps) into $OUT"
# --downloadonly keeps the module RPMs; we rebuild metadata locally below.
dnf -y module install "nvidia-driver:${STREAM}" --downloadonly --downloaddir="$OUT"
if [ "$WITH_TOOLKIT" = 1 ]; then
  echo "==> downloading cuda-toolkit (+deps)"
  dnf -y install cuda-toolkit --downloadonly --downloaddir="$OUT"
fi

echo "==> building local repo metadata"
createrepo_c "$OUT"

echo "==> packaging"
tar czf "$HERE/nvidia-bundle.tar.gz" -C "$(dirname "$OUT")" "$(basename "$OUT")"
echo "==> done: $(find "$OUT" -name '*.rpm' | wc -l) RPMs → $HERE/nvidia-bundle.tar.gz"
echo "    ship it, then on the air-gapped host:"
echo "      tar xzf nvidia-bundle.tar.gz"
echo "      sudo GPU_BUNDLE_DIR=\$PWD/nvidia-bundle ./install-nvidia.sh install"
echo
echo "    NOTE: module metadata (modules.yaml) from a file repo can be finicky; if dnf"
echo "    reports no 'nvidia-driver' module stream, enable it explicitly or install the"
echo "    kmod/driver RPMs by name. VERIFY this bundle on a scratch RHEL host before prod."
