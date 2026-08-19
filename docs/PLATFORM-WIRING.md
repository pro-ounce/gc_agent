# AI Agent — Platform Wiring Plan

**Goal:** make the Python **AI Agent** a first-class service in the gc (Compass) platform —
routed behind the gateway, integrated with gc auth, config-clean, and deployable — running a
self-hosted Ollama LLM.

**Scope of this doc:** the *platform-wiring* milestone only. MCP tool coverage (exposing all 12
services, not just administration) and a FE assistant UI are separate, later milestones.

---

## 1. Components (recap)

| Piece | What it is | Tech | Role |
|-------|-----------|------|------|
| **AI Agent** (`gc/agent`) | conversation + LLM agentic loop | Python / FastAPI, :8080 | the "brain" — reason, decide, converse |
| **MCP server** (`mw_mcp`) | tool catalog + executor over gc APIs | Java / Spring, :19170 `/mcp-service/mcp` | the "hands" — what can be done + do it |
| **Gateway** (`mw_gateway`) | edge routing + auth | Java / Spring Cloud Gateway MVC, :19010 | validates cookie/JWT, routes, vouches |

Flow: `FE → gateway → agent → MCP → (gateway →) real service`.

---

## 2. Target topology

```
Browser/FE
  └─(accessToken cookie)─▶ GATEWAY :19010
                              │  validates cookie/JWT with the DB-held HMAC key
                              │  route  /api/agent/**  ──▶  ai-agent-service :8080  (Python)
                              │  forwards: X-INT-TKN (HS512, iss=GC_INTERNAL, sub=userId, 300s)
                              │            X-AR-KEY (role), X-TRACE-ID, X-TIME-ZONE
                              ▼
                          AI AGENT  (verifies X-INT-TKN → trusts gateway → user context)
                              │  Ollama agentic loop; forwards the SAME X-INT-TKN to MCP
                              ▼
                          MCP :19170 /mcp-service/mcp   (validates X-INT-TKN; caller=gateway-service ✓)
                              │  proxies each tool call back through the gateway
                              ▼
                          real gc service (fresh X-INT-TKN)
```

---

## 3. Auth design — "gateway verifies cookies; agent verifies the gateway"

**Facts:**
- The **access token (JWT)** lives in the **browser cookie** (`accessToken`, encrypted).
- The **HMAC signing key** is a **DB setting** (loaded server-side by `SecretKeysManager`).
- The gateway strips the user `Authorization` and forwards an internal **`X-INT-TKN`**
  (HS512, iss `GC_INTERNAL`, aud `GC_INTERNAL_API`, `X_TP=SVC_PER_USER`, `sub=userId`, 300s TTL).

**Decision: the agent authenticates by verifying the gateway's `X-INT-TKN`** (not the cookie, not the
user JWT). Rationale — this is **deployment-agnostic**:
- **Same server / private subnet:** works; token presence + validity is the trust signal.
- **Different server:** still works — the signed short-lived token is cryptographic proof the request
  came from a holder of the DB key (the gateway). No reliance on network isolation.

**What the agent needs:** the HMAC key, injected **once as a sealed deploy secret** materialized from
the DB value (the agent never reads the DB live; DB remains the source of truth). If a zero-secret
agent is required, substitute **mTLS** (agent trusts the gateway's client cert) — same result, more
cert infra.

**Baseline hardening (all deployments):** TLS in transit; firewall/allowlist so only the gateway host
can reach the agent's port.

**Role/permission mapping:** extract `authorities` (`ADMIN`/`USER`) from the token (or `X-AR-KEY`) and
map to the agent's roles (`viewer`/`user`/`operator`/`admin`). Keep the agent's own `/api/auth/login`
for **standalone/dev only**; in-platform, auth = verified `X-INT-TKN`.

---

## 4. Steps

**S1 — Gateway route (data change, no code)** — `APPS_MW_CONFIGS_V`
Insert `serviceCode='agent'`, `serviceName='ai-agent-service'`, `portNumber=8080`, `isActive='Y'`.
Gateway then serves `/api/agent/**` → `http://<agent-host>:8080/ai-agent-service/...`.

**S2 — Agent `ROOT_PATH`** — `agent/app/main.py`
`FastAPI(root_path=os.getenv("ROOT_PATH","") or None)`; deploy with `ROOT_PATH=/ai-agent-service`.

**S3 — Auth (this milestone's core)** — `agent/app/rbac/`
- New middleware path: verify `X-INT-TKN` (HS512, iss `GC_INTERNAL`, aud `GC_INTERNAL_API`,
  `X_TP=SVC_PER_USER`, `leeway=30`) → build `request.state.user` from `sub`/`uname`/authorities.
- Config: `PLATFORM_AUTH_MODE=gateway` (verify X-INT-TKN) vs `standalone` (own login); `GC_JWT_ALG=HS512`;
  `GC_JWT_SECRET` = sealed key; `GC_INTERNAL_ISSUER=GC_INTERNAL`, `GC_INTERNAL_AUDIENCE=GC_INTERNAL_API`.
- Keep own HS256 login only for `standalone`.

**S4 — Agent → MCP** — `agent/app/mcp/client.py`
Forward the incoming `X-INT-TKN` header to MCP (caller stays `gateway-service` → no allowlist change).
`JAVA_MCP_BASE_URL=http://<mcp-host>:19170/mcp-service/mcp` (remove hardcoded `proounce.com` default in
`app.py:13`). Reconcile the tool path contract (`/tools` vs `/mcp` + `/mcp/tools`).

**S5 — Config / health / CORS**
Real env values, strong secrets, `CORS_ORIGINS` = real FE origins; add `/api/agent/api/health` to the
gateway `PUBLIC_ROUTES` so health is unauthenticated while chat stays protected.

**S6 — Deploy**
Python container (`:8080`) + Redis, plus **Ollama** runtime (in-house / air-gap). Add to the platform's
container orchestration; firewall the agent port to the gateway.

---

## 5. LLM provider — self-hosted Ollama

- `LLM_PROVIDER=ollama` — runs fully in-house/air-gapped, no external calls, no cost.
- The provider is pluggable behind the `LLM_PROVIDER` flag if another backend is ever needed.

---

## 6. Cleanups / out-of-scope (tracked, not this milestone)

- Reconcile the two agent copies — keep `gc/agent/` (full), retire `mw_mcp/agent/` (old `app.py`).
- Remove hardcoded `proounce.com` MCP URL default; strong default `JWT_SECRET`.
- **MCP tool coverage:** only `administration` is exposed today (one `admin_swagger.json`, ~707 tools).
  Exposing the other 11 services (generate `x-mcp`-annotated OpenAPI per service) is the next milestone.
- Add an alternate LLM provider behind `LLM_PROVIDER` only if a need arises.

---

## 7. Risks

- **Key handling:** the sealed HMAC key on the agent is a second copy — protect it (secret store, not
  env in plaintext logs). mTLS avoids this if required.
- **Token TTL:** `X-INT-TKN` is 300s — fine per-request; long agentic loops must not outlive it for
  downstream calls (the agent re-presents the same token per request, so keep tool calls within the
  request lifecycle).
- **Cross-server latency** agent↔MCP↔gateway — co-locate where possible.
