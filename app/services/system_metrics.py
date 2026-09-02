"""
services/system_metrics.py
──────────────────────────
Host CPU / memory / disk / GPU metrics for the admin console's live Metrics tab.

Primary path uses **psutil** (portable, richer: per-core, swap, disk, load, boot time).
If psutil is unavailable it degrades to a dependency-free /proc reader (Linux) so the
endpoint never hard-fails. GPU stats come from **nvidia-smi** (NVIDIA driver required).

On the DGX GB10 (Grace-Blackwell, unified LPDDR5X shared by CPU + GPU) nvidia-smi reports
N/A for discrete GPU memory — the GPU's working memory *is* system RAM (`mem` block; the
client labels it "unified").

Deploy notes: `pip install psutil` (in requirements.txt) and the NVIDIA driver for GPU.
See docs/SYSTEM-METRICS.md.
"""
from __future__ import annotations

import subprocess
import time

from ..commons.logger import get_logger

log = get_logger(__name__)

try:
    import psutil  # type: ignore
    _HAS_PSUTIL = True
    psutil.cpu_percent(interval=None)   # prime the internal delta so the first read is real
except Exception:  # noqa: BLE001 — optional; /proc fallback below
    psutil = None  # type: ignore
    _HAS_PSUTIL = False

_boot_ts = time.time()

# /proc fallback keeps the last CPU sample so successive polls give an accurate interval %.
_last_cpu: dict[str, int | None] = {"idle": None, "total": None}


# ── /proc fallback (Linux, no dependency) ───────────────────────────────────────

def _proc_cpu_percent() -> float:
    def sample() -> tuple[int, int]:
        with open("/proc/stat") as f:
            vals = [int(x) for x in f.readline().split()[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return idle, sum(vals)
    try:
        idle, total = sample()
    except Exception:  # noqa: BLE001
        return 0.0
    li, lt = _last_cpu["idle"], _last_cpu["total"]
    if li is None:
        time.sleep(0.08)
        idle, total = sample()
        li, lt = (_last_cpu["idle"] or idle), (_last_cpu["total"] or total)
    _last_cpu["idle"], _last_cpu["total"] = idle, total
    dt, di = total - lt, idle - li
    return round(max(0.0, min(100.0, 100.0 * (1 - di / dt))), 1) if dt > 0 else 0.0


def _proc_mem() -> dict:
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k.strip()] = int(v.split()[0])
    except Exception:  # noqa: BLE001
        return {}
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    used = max(0, total - avail)
    return {"used_mb": round(used / 1024), "total_mb": round(total / 1024),
            "percent": round(100 * used / total, 1) if total else 0.0}


def _loadavg() -> list[float]:
    try:
        import os
        return [round(x, 2) for x in os.getloadavg()]
    except Exception:  # noqa: BLE001
        return [0.0, 0.0, 0.0]


# ── CPU / memory / disk (psutil-preferred) ──────────────────────────────────────

def _cpu() -> dict:
    if _HAS_PSUTIL:
        return {"percent": round(psutil.cpu_percent(interval=None), 1),
                "cores": psutil.cpu_count(logical=True) or 0,
                "per_core": [round(x, 1) for x in psutil.cpu_percent(interval=None, percpu=True)],
                "load": _loadavg()}
    return {"percent": _proc_cpu_percent(),
            "cores": _cpu_count_proc(), "per_core": [], "load": _loadavg()}


def _cpu_count_proc() -> int:
    try:
        with open("/proc/cpuinfo") as f:
            return sum(1 for line in f if line.startswith("processor"))
    except Exception:  # noqa: BLE001
        return 0


def _mem() -> dict:
    if _HAS_PSUTIL:
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        return {"used_mb": round(vm.used / 1048576), "total_mb": round(vm.total / 1048576),
                "percent": round(vm.percent, 1),
                "swap_used_mb": round(sw.used / 1048576), "swap_total_mb": round(sw.total / 1048576)}
    return _proc_mem()


def _disk() -> dict:
    if _HAS_PSUTIL:
        try:
            du = psutil.disk_usage("/")
            return {"used_gb": round(du.used / 1073741824, 1),
                    "total_gb": round(du.total / 1073741824, 1), "percent": round(du.percent, 1)}
        except Exception:  # noqa: BLE001
            return {}
    return {}


# ── GPU (nvidia-smi) ────────────────────────────────────────────────────────────

def _num(v: str) -> float | None:
    v = (v or "").strip()
    if v in ("", "[N/A]", "N/A", "[Not Supported]"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _gpus() -> list[dict]:
    q = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
        )
        if out.returncode != 0:
            return []
    except Exception as exc:  # noqa: BLE001 — no GPU/driver or nvidia-smi missing
        log.bind(func="gpus").debug(f"nvidia-smi unavailable: {exc}")
        return []
    gpus: list[dict] = []
    for line in out.stdout.strip().splitlines():
        c = [x.strip() for x in line.split(",")]
        if len(c) < 7:
            continue
        gpus.append({"index": int(c[0]) if c[0].isdigit() else c[0], "name": c[1],
                     "util": _num(c[2]), "mem_used_mb": _num(c[3]), "mem_total_mb": _num(c[4]),
                     "temp": _num(c[5]), "power": _num(c[6])})
    return gpus


# ── Inference placement (is the LLM on GPU or CPU?) ─────────────────────────────

def _inference() -> dict:
    """Where each loaded model actually runs — from Ollama's /api/ps: size_vram/size is
    the fraction on the GPU. Directly answers 'is the model on the GPU or still on CPU'."""
    import json
    import urllib.request
    from ..commons.config import cfg
    base = (cfg.OLLAMA_BASE_URL or "http://localhost:11434").rstrip("/")
    try:
        with urllib.request.urlopen(base + "/api/ps", timeout=3) as r:
            data = json.load(r)
    except Exception as exc:  # noqa: BLE001 — Ollama down / different backend
        log.bind(func="inference").debug(f"ollama /api/ps failed: {exc}")
        return {"available": False, "models": []}
    models = []
    for m in (data.get("models") or []):
        size = m.get("size") or 0
        vram = m.get("size_vram") or 0
        pct = round(100 * vram / size) if size else 0
        models.append({
            "name": m.get("name"), "size_mb": round(size / 1048576),
            "vram_mb": round(vram / 1048576), "gpu_percent": pct,
            "device": "GPU" if pct >= 99 else ("CPU" if pct <= 1 else "split"),
        })
    return {"available": True, "models": models}


def snapshot() -> dict:
    """One point-in-time reading of CPU, memory, disk, GPU and LLM placement. Call often."""
    return {
        "ts": int(time.time() * 1000),
        "source": "psutil" if _HAS_PSUTIL else "proc",
        "cpu": _cpu(),
        "mem": _mem(),
        "disk": _disk(),
        "gpu": _gpus(),
        "inference": _inference(),
        "uptime_seconds": int(time.time() - (psutil.boot_time() if _HAS_PSUTIL else _boot_ts)),
    }
