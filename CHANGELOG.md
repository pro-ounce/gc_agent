# Changelog

All notable changes to the GC Agent (`gc_agent`, the COMPASS chatbot backend) are recorded here.

- Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
  [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`).
- The running version is `APP_VERSION` (`app/commons/config.py`), surfaced at `GET /health`
  (`version`) and in the `build_info` metric. Each release here is tagged `vX.Y.Z` in git.
- Deploy is `origin/main` → `bash /apps/gc_agent/deploy/pull-deploy.sh main` (resets to `origin/main`,
  pre-snapshots, restarts the `ai-agent-service` supervisor process on :17024).
- **History note:** `0.1.0`–`0.11.0` reconstruct the pre-release development milestones from git
  history (the running build reported `1.0.0` throughout). `1.0.0` marks the production baseline that
  has been serving sandbox since 2026-10-27; `1.0.1` is the first formally versioned patch.

## [1.0.1] — 2026-11-02

Correctness fixes found by reviewing the live conversation history (12 conversations / 554 user
turns) through the loopback admin history API. No new features; no config/API changes beyond the
version bump. Backend (MW/DB) defects noted below are **out of scope** and tracked separately.

### Fixed

- **Agent no longer reports false success on a rejected mutation.**
  *What went wrong:* the confirm paths (`chat_service.confirm_stream` and the sync `confirm`) gated
  completion on the **transport** `result.success` only. A GC business error returns HTTP 200 with a
  `{success:false}` envelope, so a failed `createUser` (backend `USER_MAINTENANCE_PKG.USER_VALIDATIONS`
  SQL error) and a failed role assignment (HTTP 400 on `/applicationAndRole`) still ran
  `start_onboarding` / `after_assign` and told the user *"🎉 account is ready"* / *"✅ assigned"* —
  claiming success on writes that never landed.
  *Fix:* gate completion and the skill chain on an **envelope-aware**
  `accepted = result.success and not _envelope_error(_coerce(output))` in both confirm paths.

- **Questions typed mid-flow are no longer captured as form field values.**
  *What went wrong:* `flows._should_break_out` returned `False` early for any `_FLOW_QUERY_RE` match,
  making the question break-out unreachable. A question during the create-user intake
  (*"does the current user have permission…"*, *"i am asking do we have the permission…"*) was stored
  as a name/email — in one case creating a user literally named *"i am asking do we have the
  permission to change this user"*.
  *Fix:* `is_query` now only gates **new-flow-starting** intents; a clear read/question still breaks
  out of slot capture. Added `i am asking` / `i'm asking` to `_QUESTION_RE`.

- **Definitional baseline questions are answered, not mislisted.**
  *What went wrong:* `_LIST_BASELINE_RE` includes *what*, so *"What is a budget baseline, in two
  sentences?"* matched the list read and replied *"Your 13 FORMULATION baselines:"*.
  *Fix:* a tight `_BASELINE_DEF_RE` vetoes the list/details reads for definitional phrasings; real
  list requests still list.

### Tests

- Added `eval/confirm_success_gating_eval.py` (5/5).
- Extended `eval/flow_breakout_eval.py` (now 11/11); also repaired a stale harness that never
  `await`ed the now-async `start_entity_create` / `maybe_start` (the gate had been silently 0/6).
- Regression: `router_eval` 69/69 (0 confident-wrong); `skill_routing_eval`, `remove_role_eval` pass.

### Known issues / not included

- **Backend (MW/DB), out of scope:** `USER_MAINTENANCE_PKG.USER_VALIDATIONS` bad-SQL-grammar envelope
  and `POST /api/admin/user/applicationAndRole` HTTP 400 — users shown as "created/assigned" in the
  history likely did not persist. Owner: MW/DB team.
- **Deferred (agent):** a grounded *"who is using this agent"* self-identity answer needs a new
  caller-resolution route verifiable only against live MW. `eval/entity_routing_eval.py` has the same
  stale missing-`await` harness bug (not fixed here).

## [1.0.0] — 2026-10-27

First formally versioned **production baseline** — the consolidated state that has been serving the
sandbox (COMPASS widget → `ai-agent-service` :17024). Everything in `0.1.0`–`0.11.0` rolls up here.
Capabilities at 1.0.0: GPU-served (Ollama) streaming chat with hybrid tool-RAG over the MCP tool
catalogue; grounded Q&A over the GC ecosystem; guided mutation flows (create user / role / app / menu
/ privilege, assign/remove/edit access, change role, data call, FORMULATION baseline generation) with
confirm gates, admin-authority gates and verify-after-write; a model-based intent router (flag-gated)
with an eval gate; per-app durable conversation history; full FedRAMP-style audit + risk scoring with
on-disk PII redaction; and an enterprise admin console (metrics, activity, audit, storage, history,
calendar, skills/workflows authoring, API reference, health).

## [0.11.0] — 2026-10-26

- Tool-calling model-selection eval harness wired into the router gate; live Ollama model list in the
  admin Model dropdown (embeddings excluded).
- Mutation flows re-pointed at current MCP tool names (`add*`/`save*` → `create*`).
- `user_apps` read skill; **change-role** flow (replace a user's role in an app, with multi-role
  disambiguation).
- Per-app conversation id (`chatbot:<user>:<app>`) + per-app / cross-app history scoping; history
  preserved on session expiry (not only on "new chat").
- Ops: standard snapshot-recovery script; widget bootstrap returns the current app's real name + icon;
  build & deploy reference (CLAUDE.md).

## [0.10.0] — 2026-10-16

- **Security:** redact chat content, emails and all PII from on-disk log sinks (fields, messages and
  the persisted record); stop logging tool-result content.
- **Headers:** forward full context + client env on every tool call; base64-normalize
  `X-Selected-App` / `X-Selected-Role` before forwarding to MCP.
- **Durability:** indefinitely-retained append-only conversation history (restart-safe); rotating
  local audit file + durable snapshotted OpenSearch sink; admin Storage & Durability + History tabs;
  per-day history separation + rich-block restoration; user-facing history endpoints.
- **Baseline flow maturity:** noun-labelled pickers, year-grid, multi-select, char limits + full-view
  review rail, line-item drill-down, status-code decoding, all-fund-groups/all-years scoping, and a
  hard guard against hallucinated baseline numbers.
- Seasonal workflow hints (admin-tunable **Calendar**, seeded from the gc360 activation calendar);
  guided-flow 100%-trigger hardening; capabilities reply as an actionable menu block.

## [0.9.0] — 2026-10-05

- **Intent router Phase 3:** model-based intent classifier wired as primary (flag-gated), with a
  catalogue-driven app resolver (name/code/short-code/alias → canonical code) and fast-path reads.
- Scoped-read guards + closed named-user fund-group/office leaks; app-aware named access; tools scoped
  to the current app's module.
- **Flows:** entity-onboarding front door (disambiguate before mutating) + onboarding authority gate;
  role→menu prerequisite chain; entity-create guardrails; guided remove-role flow with dependency
  preview; self-healing new-intent break-out; FlowWindow picker UX (full option lists, selectable
  flags, confirm rail).
- **Admin:** enterprise UX overhaul, Guide tab, Health tab (tiered liveliness + allow-listed restart),
  Automations vs Guided-workflows naming; client-facing terminology ("module" → "application").

## [0.8.0] — 2026-09-12

- Guided **FORMULATION baseline-generation** flow.
- Intent **slot schema + offline router eval** (Option A) made primary in `ecosystem_qa`, LLM as
  fallback; remaining ecosystem handlers (users, roles catalogue, fund groups, fiscal years) and
  organization hierarchy; role × access-type user profiles.
- Declarative **node-graph workflow engine** (grant-access), JSON-authored, with an admin Workflows
  tab, cancel/walk-back editing and run audit.
- **Verify-after-write + truthful grounding guard** (response integrity); router redesign Phase 0–2
  (action gate + eval gate; semantic classifier built, measured worse, shelved).

## [0.7.0] — 2026-09-08

- Live **Metrics** dashboard + **Activity** feed (GPU/CPU, charts, ticker); harness-vs-inference
  instrumentation; performance health panel; per-turn workflow timeline (live + replay).
- **Audit & governance (FedRAMP):** full audit trail (actor/source/app/role per turn), client
  fingerprinting, Tier-1 rule-based risk scorer, Audit tab; metrics persisted across restarts.
- `create_data_call` skill + guided data-call flow (create → attendees → reminder).
- **Ecosystem awareness:** catalogue + grounding + Q&A, license-aware, per-app balloons, named-user
  access, who-can-access; `remove_access` / `remove_application` / `edit_access` skills; role
  awareness (selected role factored into grounding); admin Docs tab.

## [0.6.0] — 2026-08-30

- Natural-language answers (synthesis on + card) with GC360-exclusive grounding on by default; keep
  chat + embed models warm (no cold start); push the model to actually CALL action tools.
- Assign application + role to a user by name; offer only the action tool when a mutation skill is
  active (slim name-typed schema); guided onboarding + create-user intake.
- **Runtime skill authoring** (`create_skill` flow + dual persistence, richer tool picker); fail-safe
  cancel + introspection meta-skills; context-aware per-module suggestions; output detail levels
  (concise/standard/detailed); deterministic user-lookup skill; air-gap enforced on every entry point.

## [0.5.0] — 2026-08-25

- **Skill framework** + `create_user` action skill with a verified field set; forbid asking for
  non-required fields; deterministic required-field gate (ask instead of inventing).
- Admin-scoped master-data (lookup) resolution (name → code, token-prefix matching); dependency-chain
  skills with progressive status and human status labels (never expose method names).
- Deterministic `applicationId` resolution (Approach A) and block-rendered results that skip LLM
  synthesis (Approach B); platform endpoints `/{agent}/me`, `/resume`, `/questions`.

## [0.4.0] — 2026-08-24

- **Admin console:** runtime config UI (tune params without restart), backup console (snapshots,
  schedule, stats + CLI), Metrics + Logs tabs; base-path aware behind an Apache proxy.
- **Deploy:** UDS/TCP runner under supervisor, backup-freshness health, deploy-snapshot hook,
  `pull-deploy.sh` one-command deploy, auto-deploy-on-push timer; factory/UDS entry points.
- **Structured UIBlocks:** render tool results as cards/tables (carried through streaming); render
  error/empty GC envelopes as notices; compact tables.

## [0.3.0] — 2026-08-20

- Consume MCP **risk metadata** for the confirmation gate; `AGENT_STRICT_GROUNDING` flag to gate
  embellishment; stop tool-call JSON / fabricated responses leaking to the chat.
- **Auth:** verify the forwarded user JWT (gateway sends the user token); accept the vouching bearer;
  self-mint a discovery token so startup authenticates to MCP.
- **Observability:** end-to-end per-turn metrics, in-flight `turn_step` logs, prompt + retrieved-tools
  logging, `/actuator/info` build/runtime/features; CIDR support in the actuator IP allow-list;
  persist the `/mcp/tools` catalogue in OpenSearch and load it on startup.

## [0.2.0] — 2026-07-29

- GPU chatbot: **streaming** responses, **hybrid tool-RAG** over the MCP catalogue, and
  **mutation guardrails** (confirm before any write).

## [0.1.0] — 2026-05-05

- Project genesis: standalone enterprise **FastAPI MCP agent** scaffold; open-source only
  (Ollama-only, all closed/paid AI deps removed); MCP client wired to the Spring Boot JSON-RPC API;
  chat UI + ASGI entry point; Python 3.12 baseline.
