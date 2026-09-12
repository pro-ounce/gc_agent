# Intent Router Redesign — Build Plan

**Status:** agreed direction, 2026-09-12. Supersedes the interim "Option A" (regex slots).

## The problem

The current router (`app/services/intent_schema.py` — labelled in-code *"Option A of the router
redesign"*) decides intent from **regex** verb/noun cues and is tuned for **recall**: it answers
whenever it can pattern-match a noun, even when it misread the intent. Observed failures:

| Query | Should | Actually did |
|---|---|---|
| `create user` | start the create-user flow | listed 416 users |
| `do I have super admin role for formulation` | yes/no on the caller's role | listed all 13 app roles |
| `roles in formulation` | list app roles ✓ | (correct) |

A fast, confident, **wrong** answer is worse than no fast answer — it violates the "response is
never wrong" bar. And because changes ship without an eval covering the real query mix, we find
these live. That is the whole backlash, in one sentence: **recall over precision, no eval gate.**

## The principle

**Precision over recall: answer only what we're sure of; escalate the rest.** Escalating to the LLM
is a *success*, not a fallback embarrassment.

## Architecture — separate the three tangled jobs

Today the regex does all three at once. Split them:

| Job | Today | Target |
|---|---|---|
| **Intent label** — what does the user *want*? (the brittle part) | regex cues | **semantic/embedding classifier** → intent + confidence; **abstain → LLM** |
| **Slots** — which app / user / role? | regex + catalogue match | **deterministic** resolution vs the live catalogue (kept — reliable) |
| **Handlers** — fetch + render grounded data | deterministic | **kept** — this is the correct, valuable part |

**Safety net (both non-negotiable):** the **grounding guard** (already shipped — an answer must be
backed by a successful tool result, else it's caveated/escalated) + an **eval suite as a deploy
gate**.

**Hard action gate:** any mutation verb (create/add/register/assign/grant/revoke/edit/update/
remove/onboard/…) is **never** answered by the read path → straight to skills/flows.

**Harness-first, LLM for the tail:** the semantic classifier runs in the harness (no GPU) and
answers the confident common cases; only low-confidence/abstain queries hit the LLM. Not
straight-to-LLM every turn — that keeps GPU on every easy question and still needs grounding+eval
anyway. How much goes to the LLM is a **measurable dial** the eval reports, not a guess.

## Target pipeline

```
turn
 └─ active flow? ─ yes → flows.handle
    no
     ├─ HARD ACTION GATE (mutation verb) ────────────→ skills / flows
     ├─ semantic intent classifier (harness, no GPU)
     │     confident (≥ τ) → deterministic slot resolve → grounded handler
     │     abstain  (< τ) ─────────────────────────────→ LLM + tools + grounding guard
     └─ (regex kept only as an optional thin fast-path for the golden set, or retired)
```

## The intent set (fixed enum the classifier targets)

Seed from the routes that already exist in `route()` + the gaps we found:

`list_apps · app_roles · roles_catalog · my_access · my_access_in_app · my_roles_all ·
named_access · app_users · users_list · fund_groups · organizations · divisions · my_offices ·
about_app` — plus the **action** labels that must route OUT: `create_user · assign_role ·
grant_access · data_call · baseline · edit_assignment` — plus **`other`** (→ LLM).

## Phased build

**Phase 0 — Eval harness first (the gate).** *Do this before touching routing.*
- Grow `eval/router_eval.py` into a real corpus: every failure we hit (`create user`→action,
  `do I have <role> for <app>`→`my_access_in_app`), representative reads, and **should-escalate**
  and **should-be-action** cases with explicit expected outcomes.
- Add metrics: per-intent precision/recall, **abstention rate**, and a hard **wrong-answer count**
  (confident + wrong) that must be **0** to pass.
- Wire it as a pre-deploy check (and CI). *No routing change ships red.*

**Phase 1 — Hard action gate (quick correctness win).**
- Mutation-verb guard returns None from the read path (fixes `create user` immediately).
- Land with Phase 0 cases covering it.

**Phase 2 — Semantic classifier (Option B core).**
- Curate labelled exemplars per intent (10–30 each), embed via `services/embeddings.py`, index
  (reuse the tool_index kNN pattern). Classify a query by nearest exemplars → intent + a
  distance-based confidence.
- Calibrate the abstain threshold τ on the eval corpus (maximise precision at 0 wrong-answers;
  report the resulting abstention→LLM rate).

**Phase 3 — Wire intent = classifier, slots = deterministic.**
- Replace the *intent decision* with the classifier; keep `_match_app` / named-user / role
  resolution against the live catalogue for slots. Handlers unchanged.
- Confident → handler; abstain → LLM+tools (grounding guard already on that path).

**Phase 4 — Shrink/retire the regex.**
- Keep regex only as a thin fast-path for a tiny golden set if it measurably helps latency;
  otherwise delete `extract_intent`'s cue tables. `route()`'s intent→handler map stays (it's the
  dispatch, not the brittle part).

**Phase 5 — Rollout & measure.**
- Shadow-run the classifier against live turns (log predicted vs actual) before switching.
- Watch the admin **harness-vs-inference** ratio + latency; confirm the abstention rate matches the
  eval projection.

## Success criteria

- **0** confident-wrong answers on the eval corpus (the gate).
- Every mutation intent routes to a flow/skill, never a read listing.
- Abstention→LLM rate within the agreed budget (measured, tuned via τ).
- No regression in p50/p95 latency for the common read questions (they stay on the harness).

## Non-goals / kept as-is

- The grounded **handlers** and **slot resolution** against the catalogue — reliable, unchanged.
- The **grounding guard** and **verify-after-write** — already shipped this cycle.
- Guided **flows** (grant_access, create_user, …) — the action path is unchanged; we only stop the
  read router from stealing action intents.
