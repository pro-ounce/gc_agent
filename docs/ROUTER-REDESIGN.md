# Intent Router Redesign — Build Plan

**Status:** in progress, 2026-09-12. Phases 0–1 shipped. **Phase 2 (semantic-first) was tried,
measured worse, and SHELVED** — see § Phase 2 outcome. The deterministic router stays primary.

## Phase 2 outcome — semantic-first shelved (2026-09-12)

We built the semantic classifier (`app/services/intent_semantic.py`) and calibrated it against the
same corpus as the regex router (`eval/semantic_eval.py`, τ-sweep). Result:

| | Deterministic (Phases 0–1) | Pure semantic |
|---|---|---|
| Correct | **39/39** | 32/39 |
| Confident-wrong | **0** | **7 (at every τ)** |

Every semantic miss is a case where the discriminator is a **slot or a verb**, not overall meaning:
`list my roles in X`→my_roles_all (missed the app), `roles in X`→my_access_in_app at cosine **1.000**
("roles in X" ≈ "my roles in X"), `who can access X`→my_access_in_app (missed "who"), `SAUSER
access`→my_access (didn't see the username), `assign/grant/remove …`→named_access (missed the verb).
**Embedding similarity blurs exactly the distinctions that decide the route** — and those signals
(verb, username, app-scope, self-vs-named) are lexical, which the deterministic extractor already
gets right.

**Decision:** the deterministic router (precision-first + eval-gated) stays **primary**; its
UNKNOWNs escalate straight to the LLM (grounded), *not* to a blurry semantic guess. The classifier
module is **shelved** (left in place, unused, bannered) to revisit later — but only as a
**slot-aware** design where deterministic verb/slot signals win, validated on the 0-confident-wrong
gate over a corpus grown from **real query logs**. The eval-first discipline did its job: we
measured before committing and did not ship a regression.

**Revisit-later note (user):** we went to semantics and came out; look at this again later.

---

*(Original plan below; Phase 2 as written is superseded by the outcome above.)*

**Original direction, 2026-09-12.** Supersedes the interim "Option A" (regex slots).

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

**Phase 0 — Eval harness first (the gate). ✅ SHIPPED (`eval/router_eval.py`, 39/39, 0 confident-wrong).**
- Grow `eval/router_eval.py` into a real corpus: every failure we hit (`create user`→action,
  `do I have <role> for <app>`→`my_access_in_app`), representative reads, and **should-escalate**
  and **should-be-action** cases with explicit expected outcomes.
- Add metrics: per-intent precision/recall, **abstention rate**, and a hard **wrong-answer count**
  (confident + wrong) that must be **0** to pass.
- Wire it as a pre-deploy check (and CI). *No routing change ships red.*

**Phase 1 — Hard action gate. ✅ SHIPPED.**
- create/add/register/onboard + assign/grant/revoke/… are mutation actions → `route()` returns
  `skill_or_flow` and `ecosystem_qa._MUTATION_CUE` bails early. Fixed `create user`→listed-users.

**Phase 2 — Semantic classifier. ⚠️ BUILT, MEASURED WORSE, SHELVED.** (see § Phase 2 outcome above)
- Built + calibrated; 7 confident-wrong vs the deterministic router's 0. Discriminators are
  lexical (verb/slot), which embeddings blur. Module kept in place, unused, for a later
  slot-aware revisit. NOT wired in.

**Phase 3 (revised) — Deterministic stays primary; grow it safely.**
- No semantic wire-in. Keep the deterministic router as the intent path; UNKNOWN/low-confidence
  escalates straight to the LLM+tools (grounding guard already there).
- Improve coverage by **adding eval cases from real query logs** and extending the deterministic
  cues to pass them — every change gated by 0 confident-wrong. This is the safe, measured lever.

**Phase 4 — (future) revisit semantics as a slot-aware fallback.**
- Only if real logs show the deterministic router mis-routing/punting on paraphrases it can't
  parse. Design constraint: deterministic verb/slot signals must WIN; semantics only breaks ties
  within a family. Validate on the 0-confident-wrong gate.

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
