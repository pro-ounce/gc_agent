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
extract_intent, route, build_app_index = _mod.extract_intent, _mod.route, _mod.build_app_index

# The REAL application catalogue (name, code, short-code). The resolver keys off name, code,
# code sub-tokens, short-code and curated aliases — all → the canonical code the DB uses. Display
# name and code are frequently unrelated (Systems Planner→ADMINISTRATION, Allocation Planner→CRP).
CATALOGUE = [
    {"name": "Formulation Planner", "code": "FORMULATION", "short": "FOR"},
    {"name": "P&I Funds Planner", "code": "P_AND_I_FUNDS", "short": "PIF"},
    {"name": "Revenue Planner", "code": "REVENUE_PLANNER", "short": "REV"},
    {"name": "Student Planner", "code": "LMS_DOCUMENTATION", "short": "LMS"},
    {"name": "Support Manager", "code": "SUPPORT_MANAGER", "short": "SUP"},
    {"name": "Systems Planner", "code": "ADMINISTRATION", "short": "ADM"},
    {"name": "Smart HuB", "code": "SMART_HUB", "short": "SMA"},
    {"name": "Forecast Planner", "code": "FORECAST_PLANNER", "short": "FRCP"},
    {"name": "Allocation Planner", "code": "CRP", "short": "CRP"},
    {"name": "TBM Planner", "code": "TBMP", "short": "TBM"},
    {"name": "Execution Planner", "code": "EXECUTION", "short": "EXE"},
    {"name": "Integrations Planner", "code": "ARC", "short": "ARC"},
    {"name": "P&I Execution Planner", "code": "P_AND_I_EXECUTION", "short": "PIFE"},
    {"name": "CFO Analytics", "code": "CFO_ANALYTICS", "short": "CFO"},
    {"name": "Franchise Formulation Planner", "code": "FRANCHISE_FUNDS_PLANNER", "short": "FFP"},
    {"name": "People Planner", "code": "PEOPLE_PLANNER", "short": "PPP"},
    {"name": "Congressional Reports", "code": "CONGRESSIONAL_REPORTS", "short": "CR"},
    {"name": "Budget Analytics", "code": "BUDGET_ANALYTICS", "short": "BA"},
    {"name": "TBM", "code": "TBM", "short": "TBM"},
    {"name": "Reports Manager", "code": "REPORTING", "short": "REP"},
    {"name": "Cost Model", "code": "COST_MODEL", "short": "CM"},
]
APPS = build_app_index(CATALOGUE)

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
    ("my access for smart hub", "my_access_in_app", {"subject": "self", "app": "SMART_HUB"}),
    ("my access in formulation", "my_access_in_app", {"subject": "self", "app": "FORMULATION"}),
    ("what can GCADMIN do", "named_access", {"subject": "user", "user": "GCADMIN"}),
    # app-in-query still routes to named_access (dispatch then bounds the profile to that app)
    ("what can JSMITH do in Formulation", "named_access", {"subject": "user", "user": "JSMITH", "app": "FORMULATION"}),
    ("what applications does JSMITH have", "named_access", {"subject": "user"}),
    ("SAUSER access", "named_access", {"subject": "user", "user": "SAUSER"}),
    # lowercase / lookup-phrasing usernames (the "look up user gcadmin" miss, 2026-09-23)
    ("look up user gcadmin", "named_access", {"subject": "user", "user": "gcadmin"}),
    ("who is gcadmin", "named_access", {"subject": "user", "user": "gcadmin"}),
    ("look up jsmith", "named_access", {"subject": "user"}),
    ("what is GCADMIN", "named_access", {"subject": "user", "user": "GCADMIN"}),
    ("what roles and access type does GCADMIN have", "named_access",
     {"subject": "user", "user": "GCADMIN"}),
    ("list the applications", "list_apps", {"subject": "all"}),
    ("show all applications", "list_apps", {"subject": "all"}),

    # ── roles ──
    ("roles in FORMULATION", "app_roles", {"entity": "role", "app": "FORMULATION"}),
    ("what roles does Allocation Planner have", "app_roles", {"app": "CRP"}),
    # distinctive partial app name (the "Roles in Smart" balloon bug, 2026-09-22) →
    # resolve Smart Hub, not a list of ALL 149 roles.
    ("Roles in Smart", "app_roles", {"entity": "role", "app": "SMART_HUB"}),
    ("roles in Smart Hub", "app_roles", {"app": "SMART_HUB"}),
    ("about Smart", "about_app", {"app": "SMART_HUB"}),
    ("roles in Revenue", "app_roles", {"app": "REVENUE_PLANNER"}),
    # alias/code/short-code resolution → canonical code (Allocation=CRP, docs=LMS, FFP=Franchise)
    ("roles in Allocation", "app_roles", {"app": "CRP"}),
    ("roles in CRP", "app_roles", {"app": "CRP"}),
    ("roles in Execution", "app_roles", {"app": "EXECUTION"}),
    ("roles in docs", "app_roles", {"app": "LMS_DOCUMENTATION"}),
    ("roles in lms", "app_roles", {"app": "LMS_DOCUMENTATION"}),
    ("who can access integrations", "app_users", {"app": "ARC"}),
    ("roles in FFP", "app_roles", {"app": "FRANCHISE_FUNDS_PLANNER"}),
    ("roles in franchise", "app_roles", {"app": "FRANCHISE_FUNDS_PLANNER"}),
    ("my roles", "my_roles_all", {"entity": "role", "subject": "self"}),
    ("list all roles", "roles_catalog", {"entity": "role", "subject": "all"}),

    # ── users / who-has-access ──
    ("who can access FORMULATION", "app_users", {"action": "who", "app": "FORMULATION"}),
    ("which users have Cost Model", "app_users", {"app": "COST_MODEL"}),
    ("list all users", "users_list", {"entity": "user", "subject": "all"}),

    # ── the rest of the ecosystem (rollout targets) ──
    ("list fund groups", "fund_groups", {"entity": "fund_group"}),
    ("all fund groups", "fund_groups", {"entity": "fund_group", "subject": "all"}),
    ("my fund groups", "my_fund_groups", {"entity": "fund_group", "subject": "self"}),
    ("what fund groups do I have", "my_fund_groups", {"entity": "fund_group", "subject": "self"}),
    ("fund groups assigned to me", "my_fund_groups", {"entity": "fund_group", "subject": "self"}),
    # named OTHER user's fund groups → guarded route, never the master list
    ("fund groups for GCADMIN", "user_fund_groups", {"entity": "fund_group", "subject": "user", "user": "GCADMIN"}),
    ("GCADMIN's fund groups", "user_fund_groups", {"entity": "fund_group", "subject": "user", "user": "GCADMIN"}),
    ("what fund groups does JSMITH have", "user_fund_groups", {"entity": "fund_group", "subject": "user"}),
    ("fund groups assigned to jsmith", "user_fund_groups", {"entity": "fund_group", "subject": "user", "user": "jsmith"}),
    ("show organizations", "organizations", {"entity": "organization"}),
    ("my offices", "my_offices", {"entity": "organization", "subject": "self"}),
    ("where can I work as Budget Facilitator", "my_offices", {"subject": "self"}),
    # named OTHER user's offices → guarded, app-aware route, never the master org list
    ("offices for GCADMIN", "user_offices", {"entity": "organization", "subject": "user", "user": "GCADMIN"}),
    ("GCADMIN's offices", "user_offices", {"entity": "organization", "subject": "user", "user": "GCADMIN"}),
    ("what offices does JSMITH have", "user_offices", {"entity": "organization", "subject": "user"}),
    ("offices assigned to jsmith", "user_offices", {"entity": "organization", "subject": "user", "user": "jsmith"}),
    ("list divisions", "divisions", {"entity": "division"}),
    ("list sub-orgs", "org_level", {"entity": "sub_org"}),
    ("list program offices", "org_level", {"entity": "program_office"}),
    ("list fiscal years", "fiscal_years", {"entity": "fiscal_year"}),
    ("tell me about Formulation", "about_app", {"action": "describe", "app": "FORMULATION"}),

    # ── mutations escalate out of the read router ──
    ("remove the Budget Approver role from GCADMIN", "skill_or_flow", {"action": "remove"}),
    ("generate a formulation baseline", "skill_or_flow", {"action": "generate"}),
    # create/add a user is a MUTATION — must NOT be answered as a user listing (2026-09-12 backlash)
    ("create user", "skill_or_flow", {"action": "create"}),
    ("create a new user", "skill_or_flow", {"action": "create"}),
    ("add a user", "skill_or_flow", {"action": "create"}),
    ("register a new user", "skill_or_flow", {"action": "create"}),
    ("onboard a new person", "skill_or_flow", {"action": "create"}),
    ("grant access to GCADMIN", "skill_or_flow", {"action": "assign"}),

    # ── self-access about ONE role → the caller's roles, NOT a list of all app roles (2026-09-12) ──
    ("do I have super admin role for formulation", "my_access_in_app",
     {"entity": "role", "subject": "self", "app": "FORMULATION"}),
    ("do I have the planning approver role in cost model", "my_access_in_app",
     {"subject": "self", "app": "COST_MODEL"}),

    # ── long-tail → UNKNOWN → LLM fallback (Option B) ──
    ("why is the sky purple", "", {}),
    ("what's the weather today", "", {}),
]


# Below this confidence the deterministic router ABSTAINS → Option B (LLM). Abstaining on a
# case we'd like it to nail is an acceptable coverage gap; committing to a WRONG route is not.
_TAU = 0.5


def run(as_json: bool = False) -> int:
    """Gate on CORRECTNESS, not coverage. The hard failure is a **confident-wrong** route — the
    router committed (confidence ≥ τ) to a route that isn't the expected one, OR answered a query
    that should have escalated (want=""). Those must be 0 to ship — they are the "backlash". An
    abstain→LLM on a golden case is reported (coverage gap for the semantic classifier) but does
    NOT fail the build, per precision-over-recall."""
    passed = confident_wrong = abstained = 0
    misses: list[tuple] = []
    for query, want, want_slots in CASES:
        it = extract_intent(query, APPS)
        got = route(it)
        slots_ok = all(str(getattr(it, k, "")) == str(v) for k, v in want_slots.items())
        abstain = it.confidence < _TAU
        if got == want and slots_ok:
            passed += 1
            continue
        if want == "":                              # should have escalated — any route is wrong
            kind = "LEAK (answered; should escalate)"
            confident_wrong += 1
        elif abstain and got in ("", want):         # low-confidence miss → LLM handles it: OK
            kind = "abstain→LLM (coverage gap)"
            abstained += 1
        else:                                       # committed to a wrong route → the dangerous one
            kind = "CONFIDENT-WRONG"
            confident_wrong += 1
        misses.append((query, want, got, want_slots, it.as_dict(), slots_ok, kind, round(it.confidence, 2)))

    total = len(CASES)
    gate_ok = confident_wrong == 0
    if as_json:
        import json
        print(json.dumps({"total": total, "passed": passed, "confident_wrong": confident_wrong,
                          "abstained": abstained, "accuracy": round(passed / total, 3),
                          "gate": "pass" if gate_ok else "FAIL"}))
        return 0 if gate_ok else 1

    print(f"router eval — {passed}/{total} exact   confident-wrong: {confident_wrong} "
          f"(gate: {'PASS' if gate_ok else 'FAIL'})   abstain→LLM: {abstained}")
    for query, want, got, wslots, gslots, slots_ok, kind, conf in misses:
        mark = "✗" if kind in ("CONFIDENT-WRONG",) or kind.startswith("LEAK") else "·"
        print(f"\n  {mark} [{kind}] {query!r}  (conf {conf})")
        print(f"      route  want={want!r}  got={got!r}")
        if not slots_ok:
            print(f"      slots  want={wslots}  got={gslots}")
    return 0 if gate_ok else 1


if __name__ == "__main__":
    sys.exit(run(as_json="--json" in sys.argv))
