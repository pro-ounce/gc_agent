"""
eval/router_eval.py
───────────────────
Offline eval for the intent router (Option A). Each case is a query plus the route
(and key slots) it MUST resolve to. Runs with no backend — it exercises the pure
extractor + dispatcher, so it can gate every deploy in CI.

    python3 eval/router_eval.py            # human report
    python3 eval/router_eval.py --json     # machine summary (for CI)

Add a case here the moment a routing bug is found — that is how "fix and hope"
becomes "can't ship the regression again". The three bugs from 2026-09-09 are the
first three cases below.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Load intent_schema directly (it's pure stdlib) so the eval never imports the whole
# app package — it runs anywhere, with no service dependencies.
_p = Path(__file__).resolve().parents[1] / "app" / "services" / "intent_schema.py"
_spec = importlib.util.spec_from_file_location("intent_schema", _p)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["intent_schema"] = _mod          # dataclass needs the module registered
_spec.loader.exec_module(_mod)
extract_intent, route = _mod.extract_intent, _mod.route

# A small slice of the live catalogue: normalised label (name AND code) → code.
APPS = {
    "formulation": "FORMULATION", "formulation planner": "FORMULATION",
    "allocation": "ALLOCATION", "allocation planner": "ALLOCATION",
    "execution": "EXECUTION", "execution planner": "EXECUTION",
    "cfo analytics": "CFO", "cost model": "COSTMODEL", "budget": "BUDGET",
}

# Each case: (query, expected_route, expected_slots{})  — slots checked only if given.
CASES: list[tuple[str, str, dict]] = [
    # ── regressions (real bugs, 2026-09-09) ──
    ("list the applications assigned to GCADMIN", "named_access",
     {"entity": "application", "subject": "user", "user": "GCADMIN"}),
    ("list my roles in FORMULATION", "my_access_in_app",
     {"entity": "role", "subject": "self", "app": "FORMULATION"}),
    ("assign Super Admin role in FORMULATION to GCADMIN", "skill_or_flow",
     {"action": "assign"}),

    # ── access, scoped by subject ──
    ("my access", "my_access", {"entity": "application", "subject": "self"}),
    ("what can GCADMIN do", "named_access", {"subject": "user", "user": "GCADMIN"}),
    ("what applications does JSMITH have", "named_access", {"subject": "user"}),
    ("SAUSER access", "named_access", {"subject": "user", "user": "SAUSER"}),
    ("what is GCADMIN", "named_access", {"subject": "user", "user": "GCADMIN"}),
    ("what roles and access type does GCADMIN have", "named_access",
     {"subject": "user", "user": "GCADMIN"}),
    ("list the applications", "list_apps", {"subject": "all"}),
    ("show all applications", "list_apps", {"subject": "all"}),

    # ── roles ──
    ("roles in FORMULATION", "app_roles", {"entity": "role", "app": "FORMULATION"}),
    ("what roles does Allocation Planner have", "app_roles", {"app": "ALLOCATION"}),
    ("my roles", "my_roles_all", {"entity": "role", "subject": "self"}),
    ("list all roles", "roles_catalog", {"entity": "role", "subject": "all"}),

    # ── users / who-has-access ──
    ("who can access FORMULATION", "app_users", {"action": "who", "app": "FORMULATION"}),
    ("which users have Cost Model", "app_users", {"app": "COSTMODEL"}),
    ("list all users", "users_list", {"entity": "user", "subject": "all"}),

    # ── the rest of the ecosystem (rollout targets) ──
    ("list fund groups", "fund_groups", {"entity": "fund_group"}),
    ("show organizations", "organizations", {"entity": "organization"}),
    ("my offices", "my_offices", {"entity": "organization", "subject": "self"}),
    ("where can I work as Budget Facilitator", "my_offices", {"subject": "self"}),
    ("list divisions", "divisions", {"entity": "division"}),
    ("list sub-orgs", "org_level", {"entity": "sub_org"}),
    ("list program offices", "org_level", {"entity": "program_office"}),
    ("list fiscal years", "fiscal_years", {"entity": "fiscal_year"}),
    ("tell me about Formulation", "about_app", {"action": "describe", "app": "FORMULATION"}),

    # ── mutations escalate out of the read router ──
    ("remove the Budget Approver role from GCADMIN", "skill_or_flow", {"action": "remove"}),
    ("generate a formulation baseline", "skill_or_flow", {"action": "generate"}),

    # ── long-tail → UNKNOWN → LLM fallback (Option B) ──
    ("why is the sky purple", "", {}),
]


def run(as_json: bool = False) -> int:
    passed, failed, low_conf = 0, [], 0
    for query, want_route, want_slots in CASES:
        it = extract_intent(query, APPS)
        got = route(it)
        slots_ok = all(str(getattr(it, k, "")) == str(v) for k, v in want_slots.items())
        ok = got == want_route and slots_ok
        if it.confidence < 0.5 and want_route:      # would escalate to Option B
            low_conf += 1
        if ok:
            passed += 1
        else:
            failed.append((query, want_route, got, want_slots, it.as_dict(), slots_ok))

    total = len(CASES)
    if as_json:
        import json
        print(json.dumps({"total": total, "passed": passed, "failed": len(failed),
                          "accuracy": round(passed / total, 3), "low_confidence": low_conf}))
        return 0 if not failed else 1

    print(f"router eval — {passed}/{total} passed ({passed / total:.0%})   "
          f"low-confidence (→ LLM fallback): {low_conf}")
    for query, want, got, wslots, gslots, slots_ok in failed:
        print(f"\n  ✗ {query!r}")
        print(f"      route  want={want!r}  got={got!r}")
        if not slots_ok:
            print(f"      slots  want={wslots}  got={gslots}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run(as_json="--json" in sys.argv))
