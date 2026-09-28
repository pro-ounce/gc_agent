"""
eval/skill_routing_eval.py
──────────────────────────
Gate for the SKILL trigger matcher (skills.match → skills.match_semantic), the layer that
pins a backing tool once the deterministic intent router has abstained. It exists to lock in
one safety invariant the router-level evals don't cover:

    A READ phrase must never pin a MUTATION skill.

A read query that semantically drifts onto a write skill (e.g. "list applications" →
assign_access) would pin that action tool and derail the turn into an onboarding flow. The
deterministic router shields the known read intents today, but the skill matcher is the last
line of defence and must be robust on its own — precision over recall for mutations.

Because match_semantic needs the embedding backend, run it on the box (like semantic_eval):

    APP_SKIP_INIT=1 PYTHONPATH=/apps/gc_agent ./bin/python eval/skill_routing_eval.py
    APP_SKIP_INIT=1 PYTHONPATH=/apps/gc_agent ./bin/python eval/skill_routing_eval.py --json

The eval reproduces the production pin decision (keyword match is authoritative; otherwise the
best semantic label is pinned once its score clears the threshold) but lets the MUTATION-skill
threshold move independently, and sweeps it to find the lowest value with ZERO read→mutation
leaks that still keeps the action paraphrases. That swept value is the recommended fix for
skills.match_semantic. Exit code is non-zero while any read→mutation leak survives at the
recommended threshold — so the regression can't ship again.

Add a case here the moment a skill mis-fire is found.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("APP_SKIP_INIT", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import skills as S            # noqa: E402
from app.services import skill_store            # noqa: E402
from app.services import intent as _intent      # noqa: E402
from app.mcp.tool_registry import is_mutation   # noqa: E402

# Current production threshold for a semantic skill trigger (skills.match_semantic default).
BASE_TAU = 0.66

# (phrase, expected_skill_or_None, kind)  — kind ∈ {"action","read"}.
#   action: the matcher SHOULD pin `expected` (recall).
#   read:   the matcher must NOT pin a mutation skill; `expected` is the acceptable read skill
#           (or None). Pinning None or a read skill passes; pinning any mutation skill is a LEAK.
CASES: list[tuple[str, str | None, str]] = [
    # ── action paraphrases that must still trigger (keyword or semantic) ──
    ("create a new user", "create_user", "action"),
    ("onboard a new person named Alex", "create_user", "action"),
    ("assign an application and role to GCADMIN", "assign_access", "action"),
    ("give jsmith access to Formulation", "assign_access", "action"),
    ("grant this user access to an app", "assign_access", "action"),
    ("remove a role from jsmith", "remove_access", "action"),
    ("remove the whole application from jsmith", "remove_application", "action"),
    ("set jsmith default application", "edit_access", "action"),
    ("disable jsmith's role", "edit_access", "action"),
    ("set up a data call", "create_data_call", "action"),
    ("create a data collection", "create_data_call", "action"),
    # ── read phrases that must NOT pin a mutation skill (the invariant) ──
    ("list applications", None, "read"),
    ("show me the applications", None, "read"),
    ("what applications are there", None, "read"),
    ("which planners exist", None, "read"),
    ("roles in Formulation", None, "read"),
    ("who can access Formulation", None, "read"),
    ("what can GCADMIN access", None, "read"),
    ("about Formulation", None, "read"),
    # read phrases whose natural skill IS a read skill — pinning it is fine, a mutation is not
    ("look up user GCADMIN", "user_lookup", "read"),
    ("account details for jsmith", "user_lookup", "read"),
    ("generate a user access report", "generate_report", "read"),
    ("my access", None, "read"),
]


def _groups() -> dict[str, list[str]]:
    """The EXACT exemplar set skills.match_semantic builds (summary + first 6 keywords)."""
    return {s.name: [s.summary or s.name, *list(s.keywords[:6])]
            for s in S.SKILLS if (s.summary or s.keywords)}


def _pin(kw: str | None, best: str | None, score: float, tau_mut: float, tau_read: float,
         mut: dict[str, bool]) -> str | None:
    """Reproduce production: keyword match wins; else pin the best semantic label once its
    score clears the per-kind threshold (mutation skills get their own, stricter bar)."""
    if kw:
        return kw
    if not best:
        return None
    eff = tau_mut if mut.get(best) else tau_read
    return best if score >= eff else None


async def main() -> int:
    as_json = "--json" in sys.argv
    try:
        skill_store.load_custom_skills()
    except Exception:  # noqa: BLE001 — custom skills are best-effort in the eval
        pass
    groups = _groups()
    mut = {s.name: is_mutation(s.tool) for s in S.SKILLS}
    cache: dict = {}

    # Score every case once (embeddings are the cost). Keyword match is threshold-independent.
    scored = []
    for phrase, expected, kind in CASES:
        kw = S.match(phrase)
        best, score = await _intent.classify_among(phrase, groups, 0.0, cache)
        scored.append((phrase, expected, kind, kw.name if kw else None, best, score))

    def grade(tau_mut: float):
        recall_ok = recall_tot = leaks = read_tot = 0
        detail = []
        for phrase, expected, kind, kw, best, score in scored:
            pinned = _pin(kw, best, score, tau_mut, BASE_TAU, mut)
            if kind == "action":
                recall_tot += 1
                ok = (pinned == expected)
                recall_ok += 1 if ok else 0
                detail.append(("action", "OK " if ok else "MISS", phrase, expected, pinned, score))
            else:
                read_tot += 1
                leak = bool(pinned) and mut.get(pinned, False)
                leaks += 1 if leak else 0
                detail.append(("read", "LEAK" if leak else "OK ", phrase, expected, pinned, score))
        return recall_ok, recall_tot, leaks, read_tot, detail

    # Sweep the mutation-skill threshold; find the lowest τ with ZERO leaks and best recall.
    sweep = []
    best_choice = None
    for i in range(int((0.90 - BASE_TAU) / 0.01) + 1):
        tau = round(BASE_TAU + 0.01 * i, 2)
        r_ok, r_tot, leaks, _rt, _d = grade(tau)
        sweep.append((tau, r_ok, r_tot, leaks))
        if leaks == 0 and best_choice is None:
            best_choice = (tau, r_ok, r_tot)

    base_ok, base_tot, base_leaks, read_tot, base_detail = grade(BASE_TAU)
    rec_tau = best_choice[0] if best_choice else None
    rec_ok, rec_tot, rec_leaks, _rt, rec_detail = grade(rec_tau) if rec_tau else (base_ok, base_tot, base_leaks, read_tot, base_detail)

    # PRODUCTION grade — the real gate. Runs the ACTUAL matcher (S.match → S.match_semantic,
    # which carries the deployed mutation-threshold guard), so the eval fails if production
    # regresses, not just if no safe threshold exists.
    prod_recall_ok = prod_recall_tot = prod_leaks = 0
    prod_detail = []
    for phrase, expected, kind in CASES:
        m = S.match(phrase) or await S.match_semantic(phrase)
        pinned = m.name if m else None
        if kind == "action":
            prod_recall_tot += 1
            ok = (pinned == expected)
            prod_recall_ok += 1 if ok else 0
            if not ok:
                prod_detail.append(("action", "MISS", phrase, expected, pinned))
        else:
            leak = bool(pinned) and mut.get(pinned, False)
            prod_leaks += 1 if leak else 0
            if leak:
                prod_detail.append(("read", "LEAK", phrase, expected, pinned))

    if as_json:
        print(json.dumps({
            "base_tau": BASE_TAU,
            "base": {"action_recall": f"{base_ok}/{base_tot}", "read_leaks": base_leaks},
            "recommended_mutation_tau": rec_tau,
            "recommended": {"action_recall": f"{rec_ok}/{rec_tot}", "read_leaks": rec_leaks},
            "production": {"action_recall": f"{prod_recall_ok}/{prod_recall_tot}",
                           "read_leaks": prod_leaks},
            "sweep": [{"tau": t, "recall": f"{a}/{b}", "leaks": l} for (t, a, b, l) in sweep],
        }, indent=2))
        return 0 if prod_leaks == 0 else 1

    print(f"skill routing eval — {len(CASES)} cases "
          f"({read_tot} read / {base_tot} action)\n")
    print(f"CURRENT (τ_mut = τ_read = {BASE_TAU}):  "
          f"action recall {base_ok}/{base_tot} · read→mutation LEAKS {base_leaks}")
    if base_leaks:
        print("  ✗ leaks at current threshold:")
        for kind, mark, phrase, exp, pinned, score in base_detail:
            if mark == "LEAK":
                print(f"      {phrase!r:40} pinned={pinned} (score {score:.3f})")
    print("\nmutation-threshold sweep (τ_read fixed at %.2f):" % BASE_TAU)
    for tau, a, b, l in sweep:
        flag = "  <- 0 leaks" if l == 0 else ""
        print(f"  τ_mut={tau:.2f}  action_recall={a}/{b}  read_leaks={l}{flag}")
    print()
    if rec_tau is not None:
        print(f"RECOMMENDED τ_mut={rec_tau:.2f}  (action recall {rec_ok}/{rec_tot}, 0 leaks)")
    else:
        print("No τ_mut in range reaches 0 leaks — exemplars overlap; tighten assign_access "
              "exemplars (drop generic 'application/access' nouns) as well.")
    # PRODUCTION result — the actual deployed matcher. This is the gate.
    print("\n" + "=" * 60)
    print(f"PRODUCTION (deployed matcher):  action recall {prod_recall_ok}/{prod_recall_tot} "
          f"· read→mutation LEAKS {prod_leaks}")
    for kind, mark, phrase, exp, pinned in prod_detail:
        print(f"  {mark:4} [{kind:6}] {phrase!r:42} want={exp!r:16} pinned={pinned!r}")
    print("  " + ("✓ invariant holds — no read pins a mutation skill" if prod_leaks == 0
                  else "✗ INVARIANT VIOLATED — a read pinned a mutation skill"))
    # Gate: fail while the DEPLOYED matcher lets any read pin a mutation skill.
    return 0 if prod_leaks == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
