"""
eval/baseline_dry_run.py
────────────────────────
Offline dry-run + gate for the guided FORMULATION BASELINE flow
(flows.maybe_start → start_formulation_baseline → _baseline → bobBaseLineGenerate_put).

Walks the full 13-step intake with a deterministic, stubbed backend (no MCP / network /
OpenSearch), prints the transcript and the final confirmation payload, and locks in the
field-contract invariants that the live backend expects:

  • sourceFiscalYear / fiscalYear carry the fiscal YEAR NUMBER (e.g. 2025 / 2026), NOT the
    fiscalYearId. Confirmed against BaselineGeneratePayload (Long year) + the formulation FE
    (generate-baseline.jsx sends Number(fiscalYear - 1); FY dropdowns bind value: item.fiscalYear).
  • businessRuleGroups / acquisitionVehicles / orgIds send ids; requestTypes send codes.
  • empty optional lists ([]) mean "all" per the baseline contract.
  • the final mutation is confirm-gated (pending bobBaseLineGenerate_put — correct casing).
  • healthCheck is sent explicitly ("N").

Run (backend stubbed, deterministic):

    APP_SKIP_INIT=1 PYTHONPATH=/apps/gc_agent ./bin/python eval/baseline_dry_run.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("APP_SKIP_INIT", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import flows  # noqa: E402
from app.mcp.tool_registry import tool_registry  # noqa: E402


class _Res:
    def __init__(self, data):
        self.success = True
        self.output = {"data": data}


# Sample option rows per source tool — shapes mirror the live responses. Fiscal years carry
# BOTH fiscalYearId and the year number, so the dry-run proves the picker sends the year.
_FISCAL_YEARS = [
    {"fiscalYearId": 6, "fiscalYear": 2026, "enabled": "Y"},
    {"fiscalYearId": 5, "fiscalYear": 2025, "enabled": "Y"},
    {"fiscalYearId": 4, "fiscalYear": 2024, "enabled": "Y"},
]
_RULE_GROUPS = [
    {"validationGroupId": 11, "groupName": "Baseline Core", "enabled": "Y"},
    {"validationGroupId": 12, "groupName": "Baseline FY26", "enabled": "Y"},
]
_ORGS = [
    {"organizationId": 101, "organizationName": "Program Office Alpha"},
    {"organizationId": 102, "organizationName": "Program Office Bravo"},
]
_REQUEST_TYPES = [
    {"requestTypeCode": "NEW", "requestTypeName": "New Request"},
    {"requestTypeCode": "MOD", "requestTypeName": "Modification"},
]
_ACQ_VEHICLES = [
    {"acquisitionVehicleId": 7, "acquisitionVehicleName": "Contract Vehicle X"},
    {"acquisitionVehicleId": 8, "acquisitionVehicleName": "Contract Vehicle Y"},
]
# Field names mirror the live getFundGroupsByUser_post response: id=fundGroupId, label=name.
_FUND_GROUPS = [
    {"fundGroupId": 1, "name": "General Fund", "code": "G", "description": "General Fund"},
    {"fundGroupId": 2, "name": "Reimbursable Fund", "code": "R", "description": "Reimbursable Fund"},
]

_BY_TOOL = {
    "getAllFiscalYears_post": _FISCAL_YEARS,
    "getAllValidationGroups_post": _RULE_GROUPS,
    "getFundGroupsByUser_post": _FUND_GROUPS,
    "getOrganizationsByUser_post": _ORGS,
    "getActiveRequestTypeMappings_post": _REQUEST_TYPES,
    "getActiveAcquisitionVehicleMappingsIds_post": _ACQ_VEHICLES,
}


async def _fake(tool, args=None, headers=None):
    return _Res(_BY_TOOL.get(tool, []))


class _Session:
    def __init__(self):
        self.metadata: dict = {}


def _chips(fr) -> str:
    labels = [c.get("label") for c in (getattr(fr, "suggestions", None) or []) if isinstance(c, dict)]
    return ("   [chips] " + " · ".join(labels)) if labels else ""


# Scripted user turns through the whole intake (intro 'start' first).
_SCRIPT = [
    ("start", "user starts the flow"),
    ("2025", "source fiscal year"),
    ("2026", "target fiscal year"),
    ("Baseline Core", "rule group — pick one (multi)"),
    ("done", "finish rule-group selection"),
    ("FY26 Program Baseline", "baseline title (free text)"),
    ("Baseline for FY26 planning", "description (free text)"),
    ("General Fund", "fund group — single pick (auto-advances)"),
    ("select all", "program offices — all"),
    ("all", "request types — all"),
    ("all", "acquisition vehicles — all"),
    ("Q1", "quarter"),
    ("yes", "target applicable?"),
    ("yes", "include recommended?"),
    ("no", "include deferred?"),
    ("no", "include withdrawn?"),
]


async def run(verbose: bool = True) -> list[tuple[str, bool, str]]:
    tool_registry.execute = _fake  # type: ignore[assignment]
    r: list[tuple[str, bool, str]] = []

    def check(name, cond, detail=""):
        r.append((name, bool(cond), detail))

    s = _Session()
    fr = await flows.maybe_start(s, "generate a formulation baseline", {})
    check("maybe_start arms formulation_baseline",
          (s.metadata.get("flow") or {}).get("name") == "formulation_baseline")
    # Must carry the progress rail from the INTRO so the widget opens a FlowWindow (not loose
    # chat bubbles) — the title is what the window header shows.
    rail_ok = fr.progress is not None and fr.progress.get("title") == "Generate FORMULATION baseline"
    if verbose:
        print("\nAGENT:", (fr.message or "").replace("\n", " "))
        print(_chips(fr))

    first_fy_chips: list[str] = []
    first_fy_noun = None
    pending = None
    for i, (msg, note) in enumerate(_SCRIPT):
        fr = await flows.handle(s, msg, {})
        rail_ok = rail_ok and fr.progress is not None   # every step stays in the FlowWindow
        if i == 0:  # the source-FY prompt right after 'start'
            first_fy_chips = [c.get("label") for c in (fr.suggestions or []) if isinstance(c, dict)]
            # Picker chips carry the field noun so the widget labels the popover "fiscal years",
            # not the generic entity ("applications") the icon glyph would otherwise imply.
            first_fy_noun = next((c.get("noun") for c in (fr.suggestions or [])
                                  if isinstance(c, dict) and c.get("noun")), None)
        if verbose:
            print(f"\nUSER:  {msg!r}   ({note})")
            print("AGENT:", (fr.message or "").replace("\n", " "))
            print(_chips(fr))
        if getattr(fr, "pending", None):
            pending = fr.pending

    check("source-FY chips are year numbers", "2026" in first_fy_chips and "2025" in first_fy_chips,
          first_fy_chips)
    check("source-FY picker chips carry noun='fiscal year'", first_fy_noun == "fiscal year",
          first_fy_noun)
    check("every step renders in the FlowWindow (progress rail present)", rail_ok)
    check("reaches confirm with a pending mutation", bool(pending))

    args = (pending or {}).get("tool_args", {})
    if verbose and pending:
        print("\n" + "=" * 78)
        print("FINAL CONFIRMATION PAYLOAD —", pending.get("tool_name"))
        print("=" * 78)
        print(json.dumps(args, indent=2, default=str))

    check("tool is bobBaseLineGenerate_put (correct casing)",
          (pending or {}).get("tool_name") == "bobBaseLineGenerate_put",
          (pending or {}).get("tool_name"))
    check("sourceFiscalYear is the YEAR 2025 (not an id)", args.get("sourceFiscalYear") == 2025,
          args.get("sourceFiscalYear"))
    check("fiscalYear is the YEAR 2026 (not an id)", args.get("fiscalYear") == 2026,
          args.get("fiscalYear"))
    check("businessRuleGroups sends ids [11]", args.get("businessRuleGroups") == [11],
          args.get("businessRuleGroups"))
    check("orgIds [] == all", args.get("orgIds") == [], args.get("orgIds"))
    check("requestTypes [] == all", args.get("requestTypes") == [], args.get("requestTypes"))
    check("acquisitionVehicles [] == all", args.get("acquisitionVehicles") == [],
          args.get("acquisitionVehicles"))
    check("Y/N answers captured", args.get("isTargetApplicable") == "Y"
          and args.get("isRecommended") == "Y" and args.get("isDeferred") == "N"
          and args.get("isWithdrawn") == "N")
    check("quarter captured", args.get("quarter") == "Q1", args.get("quarter"))
    check("fundGroupId sent from the picked fund group (General Fund = 1)",
          args.get("fundGroupId") == 1, args.get("fundGroupId"))
    check("healthCheck sent explicitly", args.get("healthCheck") == "N", args.get("healthCheck"))
    check("applicationCode FORMULATION", args.get("applicationCode") == "FORMULATION",
          args.get("applicationCode"))

    return r


def _trigger_checks() -> list[tuple[str, bool, str]]:
    """Trigger-robustness battery. arm = _BASELINE_INTENT_RE ∧ ¬_FLOW_QUERY_RE — the exact predicate
    maybe_start uses. Every taught 'generate a baseline' phrasing MUST arm; every question / read /
    status / cancel about baselines MUST NOT. Keeps the trigger at 100% for recognised intents."""
    def arms(s: str) -> bool:
        return bool(flows._BASELINE_INTENT_RE.search(s)) and not bool(flows._FLOW_QUERY_RE.search(s))

    POSITIVE = [  # a clear GENERATE request → must open the flow
        "generate a formulation baseline", "create a baseline", "run baseline",
        "build a new baseline", "new baseline", "start baseline", "formulation baseline",
        "generate the FORMULATION budget baseline", "create a budget baseline for appropriated",
        "can you generate a baseline", "could you create a baseline", "generate baseline for FY2027",
        "I want to generate a baseline", "make a baseline", "set up a baseline", "prepare a baseline",
        "kick off a baseline", "produce a baseline", "regenerate the baseline", "bob baseline",
        "generate baselines", "create formulation baselines", "formulate a baseline",
        "spin up a baseline", "initiate baseline generation", "please generate a baseline",
        "go ahead and create a baseline",
    ]
    NEGATIVE = [  # a question / read / status / cancel → must NOT open the generate flow
        "what is a baseline", "show me the baseline", "delete the baseline", "list baselines",
        "view the baseline", "explain how baselines work", "how many baselines do I have",
        "open the baseline planner", "what fund groups do I have", "who can create a baseline",
        "what is baseline generation", "explain baseline generation",
        "how does baseline generation work", "cancel baseline generation",
        "generate a report, not a baseline", "why did the baseline generation fail",
        "status of baseline generation", "how do I generate a baseline", "is the baseline ready",
        "do you have a baseline",
    ]
    out: list[tuple[str, bool, str]] = []
    for s in POSITIVE:
        out.append((f"baseline ARMS: {s!r}", arms(s), "did NOT arm"))
    for s in NEGATIVE:
        out.append((f"baseline no-misfire: {s!r}", not arms(s), "WRONGLY armed"))

    # The remaining guided flows — arm = (deterministic detector) ∧ ¬_FLOW_QUERY_RE, the exact
    # predicate maybe_start uses. Every taught phrasing must arm; every question/read/status must not.
    g = flows._FLOW_QUERY_RE
    def arms_entity(s): return flows._detect_entity(s) is not None and not g.search(s)
    def arms_remove(s): return bool(flows._detect_remove_role(s)) and not g.search(s)
    def arms_skill(s):  return bool(flows._SKILL_INTENT_RE.search(s)) and not g.search(s)
    batteries = [
        ("entity-create", arms_entity,
         ["create a role", "add a new role", "create an application", "set up an application",
          "add a menu", "make a new menu", "create a role privilege", "create a user",
          "onboard a new user", "create an app role"],
         ["what roles do I have", "list applications", "show me the menus",
          "who can create a role", "how do I create a role", "delete the menu"]),
        ("remove-role", arms_remove,
         ["delete the Budget Approver role", "remove the admin role definition", "delete a role"],
         ["who has the admin role", "what is a role", "list roles"]),
        ("create-skill", arms_skill,
         ["create a skill", "add a new skill", "build a skill"],
         ["what is a skill", "list skills", "how do skills work"]),
    ]
    for label, fn, pos, neg in batteries:
        for s in pos:
            out.append((f"{label} ARMS: {s!r}", fn(s), "did NOT arm"))
        for s in neg:
            out.append((f"{label} no-misfire: {s!r}", not fn(s), "WRONGLY armed"))
    # The tool→opener map must stay consistent with the entity specs it drives.
    out.append(("entity tool-map matches _ENTITY_CREATE",
                all(flows._ENTITY_CREATE.get(e, {}).get("tool") == t
                    for t, e in flows._ENTITY_BY_TOOL.items()), "tool map drift"))

    # Seasonal hints: fire in-season for the right app, stay empty out-of-season / for other apps,
    # and EVERY hint's `send` must arm its flow (tapping a seasonal hint opens the real guided flow).
    import datetime as _dt
    from app.services import seasonal
    in_season = seasonal.seasonal_hints("FORMULATION", _dt.datetime(2026, 4, 1))
    out.append(("seasonal: formulation hints fire in-season", len(in_season) > 0, "no hints"))
    out.append(("seasonal: baseline seasonal hint arms the flow",
                any(arms(h["send"]) for h in in_season if "baseline" in h["send"].lower()),
                "baseline hint does not arm"))
    out.append(("seasonal: empty out-of-season",
                seasonal.seasonal_hints("FORMULATION", _dt.datetime(2026, 12, 1)) == [],
                "fired off-season"))
    out.append(("seasonal: empty for a different app in-season",
                seasonal.seasonal_hints("ADMINISTRATION", _dt.datetime(2026, 4, 1)) == [],
                "fired for wrong app"))

    # Data-driven store: the federal seed is non-empty and well-formed, validation drops junk and
    # keeps a good season, and every seed hint's `send` arms its flow (data-driven = still real flows).
    from app.services import seasonal_store
    seed = seasonal_store.federal_calendar()
    out.append(("seasonal store: federal seed non-empty + well-formed",
                bool(seed) and all({"name", "app", "months", "enabled", "hints"} <= set(s) for s in seed),
                "seed malformed"))
    # The baseline hint opens a maybe_start guided flow, so it must arm. ("set up a data call" is an
    # LLM-driven data-call action, not a maybe_start flow — see gc-agent-seasonal-hints — so it won't.)
    out.append(("seasonal store: seed baseline hint arms its flow",
                any(arms(h["send"]) for s in seed for h in s["hints"] if "baseline" in h["send"].lower()),
                "seed baseline hint does not arm"))
    cleaned = seasonal_store._clean_list([
        {"name": "Junk"},                                        # no months/hints → dropped
        "not-a-dict",                                             # wrong type → dropped
        {"name": "Audit", "months": "1-2", "enabled": True,
         "hints": [{"label": "Review baselines", "send": "generate a baseline"}]},
    ])
    out.append(("seasonal store: validation drops junk, keeps a valid season",
                len(cleaned) == 1 and cleaned[0]["name"] == "Audit",
                f"got {cleaned!r}"))
    return out


def main() -> int:
    results = asyncio.run(run(verbose=True))
    results += _trigger_checks()
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("\n" + "-" * 78)
    for name, ok, detail in results:
        tag = "OK  " if ok else "FAIL"
        extra = "" if ok else f"   ← {detail!r}"
        print(f"  {tag} {name}{extra}")
    print(f"\nsummary: {passed}/{total} baseline-flow checks passed")
    print("  ✓ baseline flow holds — year numbers sent, lists=all, confirm-gated"
          if passed == total else "  ✗ baseline-flow regression")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
