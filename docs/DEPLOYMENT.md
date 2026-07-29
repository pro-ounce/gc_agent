# Compass AI Ecosystem — Deployment Layout & Runbook

What to deploy, where, and how, to run the AI-capable ecosystem (Ollama-first, air-gap-friendly).

---

## 1. Topology & ports

```
                         ┌──────────── your network / behind gateway ────────────┐
Browser/FE ─cookie─▶ GATEWAY :19010 ─/api/agent/**─▶ AI AGENT :8080 (Python/FastAPI)
                         │  (Java, CI)                     │
                         │                                 ├─▶ OLLAMA  :11434   (LLM runtime)
                         │                                 ├─▶ OPENSEARCH :9200 (session/KV store)
                         │                                 └─▶ MCP :19170  (tool server, Java/CI)
                         │                                          │
                         └── domain services (auth :19020, administration :19030, …) ◀── proxied via gateway
```

| Component | Port | Runtime | Deployed via |
|-----------|------|---------|--------------|
| gateway (mw_gateway) | 19010 | Java 21 / Tomcat (WAR) | **CI** |
| authentication (mw_authentication) | 19020 | Java 21 / Tomcat (WAR) | **CI** |
| administration (mw_administration) | 19030 | Java 21 / Tomcat (WAR) | **CI** |
| mcp-service (mw_mcp) | 19170 | Java 21 / Tomcat (WAR) | **CI** |
| AI agent (gc/agent) | 8080 | Python 3.11+ / uvicorn | **new — container or systemd** |
| Ollama | 11434 | native service | **install (below)** |
| OpenSearch | 9200 | already in estate | existing |

---

## 2. Middleware services to deploy (via your CI)

**Required for the AI pipeline:**
- **gateway** — routes `/api/agent/**` → agent, `/api/mcp/**` → mcp; validates the cookie/JWT and
  issues the `X-INT-TKN` the agent and MCP trust.
- **authentication** — issues/validates the user token (browser cookie) and holds the DB-backed HMAC
  signing key logic. Auth for the whole chain depends on it.
- **mcp** (mcp-service) — the tool server the agent calls.
- **administration** — the only service exposing MCP tools today (4 tools). Deploy it so the agent has
  something to call. (Add more domain services here as you annotate `x-mcp` tools — see
  `mw_mcp/docs/ADDING-MCP-TOOLS.md`.)

**Build-time dependency (not a running service):**
- **commons** (gc-commons: logging, telemetry, jwtFilter, security-utils, bom, …) — built and published
  to your Maven repo; every service compiles against it. Ensure it's built before the services.

**Optional (as tool coverage grows):** crp, execution, formulation, revenue, smarthub, support,
reporting, pif, pife — deploy each via CI and annotate its endpoints to expose them as tools.

**Minimal working set:** `commons` (build) → `gateway` + `authentication` + `mcp` + `administration`.

---

## 3. Software / packages per component

**Middleware (all via CI, already standardized):** JDK 21, Maven (wrapper), Apache Tomcat, Oracle JDBC
(ojdbc11 via BOM). Nothing new.

**AI agent (new):**
- Python **3.11+** (3.12 tested; the repo pins 3.12.8 — the Dockerfile uses 3.11-slim, either is fine)
- pip + venv (or Docker)
- Python packages (`requirements.txt`): fastapi, uvicorn[standard], httpx, pydantic, pydantic-settings,
  PyJWT, passlib[bcrypt], cryptography, **opensearch-py**, **prometheus-client**, python-json-logger,
  python-dotenv, sse-starlette, python-multipart.

**LLM runtime:** **Ollama** + a model (default `llama3.1`). GPU recommended (NVIDIA/CUDA or Apple
Metal); CPU works but slower. Disk: ~5 GB for llama3.1 (8B).

**Store:** OpenSearch (existing). The agent auto-creates its `agent-kv` index on first boot.

**Observability (existing):** Prometheus (add the agent as a scrape target), your log stack.

---

## 4. Install — Ollama (LLM runtime)

**Online (has internet):**
```bash
# Linux
curl -fsSL https://ollama.com/install.sh | sh
# starts a systemd service on 127.0.0.1:11434

# pull the model (once)
ollama pull llama3.1            # 8B, ~4.7 GB;  or llama3.1:70b for higher quality
ollama list                    # verify
```

**Air-gapped (no internet on the server):**
```bash
# 1. On an internet-connected box: pull once, then export the model blobs
ollama pull llama3.1
tar -C ~/.ollama -czf ollama-llama3.1.tgz models
# 2. Also grab the Ollama binary (github releases) for the target OS/arch.
# 3. On the air-gapped server: install the binary, then
mkdir -p /usr/share/ollama/.ollama && tar -C /usr/share/ollama/.ollama -xzf ollama-llama3.1.tgz
ollama list                    # should show llama3.1
```

**Expose to the agent (if agent is on another host):**
```bash
# Ollama binds loopback by default; to allow the agent host to reach it:
sudo systemctl edit ollama      # add:  [Service]\nEnvironment="OLLAMA_HOST=0.0.0.0:11434"
sudo systemctl restart ollama
# then firewall 11434 to the agent host only.
```
Verify: `curl http://<ollama-host>:11434/api/tags` → lists models.

---

## 5. Install — AI agent

**Option A — systemd (host Python):**
```bash
cd /opt && git clone <repo> agent && cd agent        # or copy the gc/agent tree
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env.local                            # then edit (see §6)
# run (production): 2 workers
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 2
```
Wrap that in a systemd unit (Restart=always, EnvironmentFile=/opt/agent/.env.local).

**Option B — Docker:**
```bash
cd gc/agent
docker build -t compass-agent:latest .                # python:3.11-slim, non-root, healthcheck on :8080
docker run -d --name compass-agent -p 8080:8080 --env-file .env.local compass-agent:latest
```
(The bundled `docker-compose.yml` also brings up Redis — **not needed** now; we use OpenSearch. Set
`STORE_BACKEND=opensearch` and you can drop the redis service.)

---

## 6. Configure the agent (`.env.local`)

```ini
ENV=production
ROOT_PATH=/ai-agent-service          # must equal SERVICE_NAME in the gateway route (§7)

# Auth — trust the gateway (deployment-agnostic)
PLATFORM_AUTH_MODE=gateway
GC_JWT_SECRET=<sealed copy of the DB HMAC signing key used by authentication-service>
GC_JWT_ALGORITHM=HS512

# LLM — Ollama (self-hosted)
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://<ollama-host>:11434
LLM_MODEL=llama3.1

# MCP tool server
JAVA_MCP_BASE_URL=http://<mcp-host>:19170/mcp-service/mcp

# Store — OpenSearch (no Redis)
STORE_BACKEND=opensearch
OPENSEARCH_URL=https://<opensearch-host>:9200
OPENSEARCH_USERNAME=<user>
OPENSEARCH_PASSWORD=<pass>
OPENSEARCH_INDEX=agent-kv

# CORS / observability
CORS_ORIGINS=https://<your-fe-origin>
METRICS_ENABLED=true
ACTUATOR_ALLOWED_IPS=                 # e.g. 127.0.0.1,::1 for loopback-only scraping
```

---

## 7. Wire into the platform (gateway)

1. **Route** — insert the agent into the gateway's `APPS_MW_CONFIGS_V` (DB), then restart the gateway:
   `serviceCode='agent'`, `serviceName='ai-agent-service'`, `portNumber=8080`, `isActive='Y'`.
2. **Public routes** — add to `SecurityRouteRegistry.PUBLIC_ROUTES`:
   `"/**/api/agent/actuator/**"` (scrape/probes) and, if desired, `"/**/api/agent/api/health"`.
3. **Prometheus** — add a scrape target for `/actuator/prometheus` on the agent (direct on :8080, or via
   the gateway route). Metric names match Micrometer (`http_server_requests_seconds`) — dashboards apply.

---

## 8. Smoke test

```bash
curl http://<agent>:8080/actuator/health/liveness        # {"status":"UP"}
curl http://<agent>:8080/actuator/health                 # store/mcp/llm all UP -> 200
curl http://<agent>:8080/actuator/prometheus | head       # metrics
# through the gateway (authenticated FE session):
#   POST /gateway-service/api/agent/api/chat  {"session_id":"s1","message":"list users named John"}
#   -> agent picks an admin tool -> MCP -> administration -> answer; session doc appears in agent-kv
```

Readiness (`/actuator/health/readiness`) returns 200 only when **store + mcp** are reachable.

---

## 9. Air-gap checklist
- Ollama model transferred offline (§4); no outbound calls with `LLM_PROVIDER=ollama`.
- Python wheels: build a local wheelhouse (`pip download -r requirements.txt -d wheels/`) and
  `pip install --no-index --find-links wheels/ -r requirements.txt` on the server.
- No external services touched: LLM = Ollama (local), store = OpenSearch (local), tools = your services.
