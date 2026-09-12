"""
eval/semantic_eval.py
─────────────────────
Calibration + accuracy eval for the semantic intent classifier (Option B). Unlike
router_eval.py (pure stdlib), this needs the embedding backend, so run it on the box:

    APP_SKIP_INIT=1 PYTHONPATH=/apps/gc_agent ./bin/python eval/semantic_eval.py

It classifies every query in the shared corpus (router_eval.CASES) with the RAW score, then
sweeps the abstain threshold τ to find the value that maximises coverage at ZERO confident-wrong
— the same gate the deterministic router uses. Reports, per τ:
  • correct      — predicted route == expected (or correctly abstained on an "" case)
  • confident-wrong — committed (sim ≥ τ) to a WRONG route, or answered a should-escalate case
  • abstained    — sim < τ on a case we'd like nailed (acceptable → LLM handles it)
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_root))

# The corpus is the SAME cases the regex router is graded on.
_re = importlib.util.spec_from_file_location("router_eval", _root / "eval" / "router_eval.py")
_rm = importlib.util.module_from_spec(_re); _re.loader.exec_module(_rm)
CASES = _rm.CASES

from app.services.intent_semantic import score, _route_of  # noqa: E402


async def main() -> int:
    # Score every case once (embeddings are the expensive part).
    scored = []
    for query, want, _slots in CASES:
        label, sim = await score(query)
        scored.append((query, want, label, sim))

    print(f"semantic intent eval — {len(scored)} cases\n")
    best = None
    for tau in [round(0.50 + 0.02 * i, 2) for i in range(20)]:   # 0.50 … 0.88
        correct = wrong = abstained = 0
        for query, want, label, sim in scored:
            pred = "" if (not label or sim < tau) else _route_of(label)
            if pred == want:
                correct += 1
            elif pred == "":                       # abstained
                if want == "":
                    correct += 1                   # correctly escalated
                else:
                    abstained += 1                 # coverage gap → LLM (ok)
            else:                                  # committed to a route
                wrong += 1                         # wrong route, or leaked a should-escalate
        row = (tau, correct, wrong, abstained)
        flag = "  <- 0-wrong" if wrong == 0 else ""
        print(f"  τ={tau:.2f}  correct={correct:2}/{len(scored)}  confident-wrong={wrong:2}  abstain={abstained:2}{flag}")
        if wrong == 0 and (best is None or correct > best[1]):
            best = row
    print()
    if best:
        print(f"RECOMMENDED τ={best[0]:.2f}  (correct={best[1]}, wrong=0, abstain={best[3]})")
    else:
        print("No τ reached 0 confident-wrong — exemplars need work (see misses below).")
    # Show the confident-wrong + low-sim cases at a mid τ to guide exemplar tuning.
    print("\n-- detail @ τ=0.62 --")
    for query, want, label, sim in scored:
        pred = "" if (not label or sim < 0.62) else _route_of(label)
        mark = "OK " if pred == want or (pred == "" and want == "") else ("ABST" if pred == "" else "WRONG")
        if mark != "OK ":
            print(f"  {mark:5} {query!r:52} want={want!r:16} got={label!r:16} sim={sim:.3f}")
    return 0 if best else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
