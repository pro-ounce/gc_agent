# GC Agent — TODO / Backlog

Open follow-ups, most recent first. Deployed items live in the commit history; this is what's
*not* built yet.

## Fraud / risk detection (graduate beyond Tier-1)
The current risk scorer (`app/services/risk.py`) is Tier-1: rule-based + explainable, wired into
`/admin/audit` + the Audit tab. Next tiers:

- [ ] **Tunable risk weights/thresholds in admin config** — expose the `risk._W` weights,
      business-hours window, velocity/travel limits via `runtime_config` (like the other toggles)
      so operators adjust without a deploy.
- [ ] **Statistical baselining (Tier-2)** — per-user behavior profile (typical apps, roles, hours,
      IPs, request rate) from the WORM audit trail + the platform audit-log tools
      (`getUserAuditLogs`, `getRequestAuditSummary*`); flag z-score / percentile deviations.
- [ ] **Unsupervised model (Tier-3)** — isolation forest / one-class SVM on the audit features
      (runs locally via scikit-learn, air-gapped); needs enough logged history to train.
- [ ] **True impossible-travel** — current signal is an IP-hop proxy (no offline geo DB). Add an
      offline GeoIP/ASN database to compute distance-over-time properly.
- [ ] **Anomaly alerting** — notify on repeated high-risk / off-domain / non-browser hits
      (webhook / email / Fluent Bit route), not just surface them in the Audit tab.

## Audit / logging (WORM)
- [ ] **Graduate WORM to S3 Object-Lock (compliance mode)** — offsite + enforced retention.
      `out_s3` plugin is present in the box's Fluent Bit build; needs a bucket (object-lock on),
      region, and a credential method (instance IAM role or Vault keys). Dual-ship: keep local
      append-only + add S3.
- [ ] **Local WORM rotation/retention** — the interim `/dbdata/worm/gcagent/*` files grow
      unbounded; add daily-dated append-only files (until S3 lifecycle handles it).
- [ ] **Session persistence** — the agent's OpenSearch KV is unreachable on the box
      (`ping False → in-memory`), so chat *sessions* are lost on restart (metrics/skills/balloons
      already persist to flat files). Fix the OpenSearch connection or persist sessions to a file.

## Access management (round out the CRUD)
- [ ] **"Set default application" semantics** — `edit_access make_default` sets `isDefault=Y` on the
      *named* role's row; decide if "make X my default app" should also *unset* the others (loop).
- [ ] **`updateUserApplicationRoles_put` versionNumber** — currently sent as-is (current value);
      confirm whether the backend expects an incremented version for optimistic locking.
- [ ] **Bulk / by-app role edits** if needed beyond single (user, app, role).

## UX (deferred by the user — "UX is worst, come back later")
- [ ] **Widget UX overhaul** — the whole chat widget experience.
- [ ] **Admin-section balloon editor UI** — the `/admin/balloons` API + store are built; no
      front-end editor in the smarthub/widget admin section yet.

## Done this session (for reference)
Data-call flow · MW deploy script (`/apps/deploy-mw.sh`) · ecosystem awareness + license-gating ·
per-app balloons + admin store · harness-vs-inference instrumentation · request workflow
visualization · natural-interaction (semantic routing + friendly voice) · read-vs-mutation routing
fix · assign/remove/remove-app(cascade)/edit access skills · role awareness (grounding + header) ·
full audit trail (actor/ip/browser/OS/origin/app/role) · Docs tab (inline architecture SVG) ·
metrics persistence · WORM log shipping (local append-only) · Tier-1 risk scorer.
