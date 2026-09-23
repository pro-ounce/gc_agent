"""
eval/classifier_eval.py
───────────────────────
Benchmark the MODEL-BASED intent classifier against the same corpus as the regex router. Unlike
router_eval.py (pure stdlib), this needs the model, so run it on the box:

    APP_SKIP_INIT=1 PYTHONPATH=/apps/gc_agent ./bin/python eval/classifier_eval.py

Compares ROUTE only (slots are the deterministic resolver's job). Sweeps the abstain threshold τ
to find the value with max coverage at ZERO confident-wrong — the same gate the deterministic
router uses — and lists the misses so the prompt/enum can be tuned.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_root))

_re = importlib.util.spec_from_file_location("router_eval", _root / "eval" / "router_eval.py")
_rm = importlib.util.module_from_spec(_re); _re.loader.exec_module(_rm)
CASES = _rm.CASES

from app.services.intent_classifier import score          # noqa: E402
from app.services import ecosystem as eco                  # noqa: E402
from app.rbac.jwt_handler import mint_service_token        # noqa: E402


async def main() -> int:
    H = {"Authorization": "Bearer " + (mint_service_token() or "")}
    catalogue = await eco.applications(H)
    print(f"classifier eval — {len(CASES)} cases, {len(catalogue)} apps in catalogue (model-based)\n")

    scored = []
    for i, (q, want, _slots) in enumerate(CASES, 1):
        route, conf = await score(q, catalogue)
        scored.append((q, want, "" if route == "other" else route, conf))
        print(f"  [{i:2}/{len(CASES)}] {q[:44]:44} -> {route or '(none)':16} ({conf})")

    print("\n== τ sweep (route-only; correct includes a correct abstain on an '' case) ==")
    best = None
    for tau in [round(0.50 + 0.05 * i, 2) for i in range(10)]:
        correct = wrong = abstained = 0
        for _q, want, route, conf in scored:
            pred = "" if (not route or conf < tau) else route
            if pred == want:
                correct += 1
            elif pred == "":
                correct += 1 if want == "" else 0
                abstained += 0 if want == "" else 1
            else:
                wrong += 1
        flag = "   <- 0-wrong" if wrong == 0 else ""
        print(f"  τ={tau:.2f}  correct={correct:2}/{len(scored)}  confident-wrong={wrong:2}  abstain={abstained:2}{flag}")
        if wrong == 0 and (best is None or correct > best[1]):
            best = (tau, correct, wrong, abstained)

    print()
    print(f"RECOMMENDED τ={best[0]:.2f}  (correct={best[1]}, wrong=0, abstain={best[3]})" if best
          else "No τ reached 0 confident-wrong — tune prompt/enum (see misses).")
    print("\n-- misses @ τ=0.60 --")
    for q, want, route, conf in scored:
        pred = "" if (not route or conf < 0.60) else route
        if pred != want and not (pred == "" and want == ""):
            kind = "ABSTAIN" if pred == "" else "WRONG"
            print(f"  {kind:7} {q!r:50} want={want!r:16} got={route!r:16} conf={conf}")
    return 0 if best else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
