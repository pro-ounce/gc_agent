# Admin Metrics & Activity — dependencies and deploy runbook

The admin console (`/admin` → **Metrics** and **Activity** tabs) shows live host CPU/GPU/
memory, whether the LLM is running on **GPU or CPU**, hand-drawn charts + a ticker, and a
live feed of chat turns (prompt → answer → tools → latency → tokens → errors) for
performance and response-validity analysis.

This document lists everything the feature needs so it can be stood up on another server.

---

## What is installed / required

| Requirement | Purpose | Notes |
|---|---|---|
| **`psutil`** (Python pkg, `requirements.txt`) | CPU %, per-core, memory, swap, disk, load, boot time | Falls back to a dependency-free `/proc` reader (Linux) if missing, so the endpoint never hard-fails |
| **`nvidia-smi`** (from the NVIDIA driver) | GPU utilisation, temperature, power, memory | Already present wherever the NVIDIA driver is installed. No GPU/driver ⇒ the GPU card just shows "—" |
| **Ollama** reachable at `OLLAMA_BASE_URL` | LLM device placement (`/api/ps` → `size_vram/size`) | Answers "is the model on GPU or CPU". If Ollama is down, the badge shows "No model loaded" |
| Agent's in-memory **log ring** (built-in) | The Activity feed (turns, prompts, answers, errors) | Nothing to install; capped at 800 recent records in process memory |

No other new dependencies. Charts + ticker are pure `<canvas>`/CSS (no chart library) so the
admin console keeps its strict CSP (`script-src 'self'`).

### GB10 / unified-memory note
On the DGX **GB10** (Grace-Blackwell, unified LPDDR5X shared by CPU and GPU), `nvidia-smi`
reports **N/A** for discrete GPU memory. The dashboard detects this and shows **system RAM**
as the GPU's memory, labelled "(unified)". This is expected, not a bug.

---

## Install steps (new server)

1. **Pull the code** (the metrics module + admin assets ship in the repo):
   - `app/services/system_metrics.py`, `app/routers/admin.py`, `app/static/admin.{html,js}`.

2. **Install Python deps** into the agent venv. `requirements.txt` already contains
   `psutil>=5.9`, so the standard deploy picks it up:
   ```bash
   cd /apps/gc_agent && deploy/pull-deploy.sh main     # runs pip when requirements.txt changed
   ```
   Manual install if needed (⚠ the venv's `bin/pip` shebang can be broken — use `python -m pip`):
   ```bash
   /apps/gc_agent/bin/python -m pip install "psutil>=5.9"
   ```
   `psutil` ships prebuilt wheels for `x86_64` and `aarch64` (the GB10 is aarch64).

3. **GPU**: ensure `nvidia-smi` is on `PATH` and returns data:
   ```bash
   nvidia-smi --query-gpu=index,name,utilization.gpu,temperature.gpu,power.draw \
     --format=csv,noheader,nounits
   ```

4. **Ollama**: confirm it answers (used for the GPU/CPU placement badge):
   ```bash
   curl -s http://localhost:11434/api/ps | python3 -m json.tool     # size_vram vs size per model
   ollama ps                                                        # human view: PROCESSOR column
   ```

5. **Restart** the agent (the deploy script does this): `supervisorctl restart ai-agent-service`.
   Static admin files are served from disk (`FileResponse`), so `admin.html/js` changes need
   only a `git pull` — no restart.

---

## Endpoints (all behind the `/admin` IP guard — `ACTUATOR_ALLOWED_IPS`)

| Endpoint | Returns |
|---|---|
| `GET /admin/system` | `{ cpu, mem, disk, gpu[], inference{models[]}, source, uptime_seconds }` |
| `GET /admin/turns?limit=N` | recent chat turns: `question, answer, tools, total_ms, llm_ms, tools_ms, tokens_in/out, outcome, errors[]` |
| `GET /actuator/info` | existing runtime + aggregate metrics (turns, LLM, tokens, tools, MCP, HTTP) |

The Metrics tab polls `/admin/system` + `/actuator/info` every ~2.5s (only while visible);
the Activity tab polls `/admin/turns` every ~3s. Both have a "live" toggle.

---

## Verify it works

```bash
# 1) System snapshot — source should be 'psutil', inference should show the device
curl -s http://localhost:17024/admin/system | python3 -m json.tool

# 2) Is the model on GPU?  Look for inference.models[].device == "GPU" and gpu_percent 100
curl -s http://localhost:17024/admin/system \
  | python3 -c 'import sys,json;print([(m["name"],m["device"],m["gpu_percent"]) for m in json.load(sys.stdin)["inference"]["models"]])'

# 3) Activity feed — run a turn, then read it back
curl -s "http://localhost:17024/admin/turns?limit=5" | python3 -m json.tool
```

Expected on the sparkbee GB10: `source: psutil`, GPU `NVIDIA GB10`, and
`inference: qwen2.5:14b → GPU 100%`.

---

## How "GPU or CPU" is determined

Ollama's `/api/ps` reports, per loaded model, `size` (total weights) and `size_vram`
(bytes resident in GPU memory). The dashboard computes `gpu_percent = size_vram / size`:

- **100%** → fully on GPU (badge: ⚡ green)
- **0%** → fully on CPU (badge: ⚠ amber)
- **in between** → split across GPU+CPU (badge: ◑ violet)

This is the ground truth for whether inference is actually GPU-accelerated — GPU
*utilisation* (from nvidia-smi) only spikes during active inference and is 0% at idle even
when the model is resident on the GPU.
