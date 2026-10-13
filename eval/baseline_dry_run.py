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


# Scripted user turns through the whole intake. Title is the FIRST step (→ header name); description
# is a LATER step (→ header subtitle).
_SCRIPT = [
    ("FY26 Program Baseline", "baseline title (free text) — step 1, header name"),
    ("2025", "source fiscal year"),
    ("2026", "target fiscal year"),
    ("Q1", "quarter"),
    ("Baseline Core", "rule group — pick one (multi)"),
    ("done", "finish rule-group selection"),
    ("General Fund", "fund group — single pick (auto-advances)"),
    ("select all", "program offices — all"),
    ("all", "request types — all"),
    ("all", "acquisition vehicles — all"),
    ("Baseline for FY26 planning", "description (free text) — header subtitle"),
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
    rail_ok = fr.progress is not None and fr.progress.get("title") == "Baseline Generation"
    # The flow opens on the FIRST question — now the Title (→ header name). The fiscal-year picker chips
    # arrive on the NEXT result (after the title is answered), so capture them at i == 0 in the loop.
    if verbose:
        print("\nAGENT:", (fr.message or "").replace("\n", " "))
        print(_chips(fr))

    first_fy_chips: list = []
    first_fy_noun = None
    pending = None
    for i, (msg, note) in enumerate(_SCRIPT):
        fr = await flows.handle(s, msg, {})
        rail_ok = rail_ok and fr.progress is not None   # every step stays in the FlowWindow
        if i == 0:  # title answer → the source-FY prompt (fiscal-year picker chips)
            first_fy_chips = [c.get("label") for c in (fr.suggestions or []) if isinstance(c, dict)]
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

    # ── Review-stage "adjust" (allow the changes back): a tapped row / "change the X" re-opens
    #    ONE field, then drops straight back to review with the new value (no re-walk). ──
    q_idx = next(i for i, st in enumerate(flows.BASELINE_STEPS) if st.field == "quarter")
    fy_idx = next(i for i, st in enumerate(flows.BASELINE_STEPS) if st.field == "fiscalYear")
    check("adjust: 'change the quarter' resolves to the quarter step",
          flows._match_baseline_field("change the quarter") == q_idx)
    check("adjust: 'target FY' beats the 'target' Y/N flag (longest alias wins)",
          flows._match_baseline_field("edit the target fy") == fy_idx)
    check("adjust: structured 'edit:quarter' resolves to the quarter step",
          flows._match_baseline_field("edit:quarter") == q_idx)
    _rail_steps = (flows._baseline_progress(s.metadata["flow"]).get("steps") or [])
    check("adjust: every review rail row carries a 'field' key (for the tappable edit)",
          bool(_rail_steps) and all("field" in st for st in _rail_steps),
          [st.get("label") for st in _rail_steps if "field" not in st])
    fr_edit = await flows.handle(s, "edit:quarter", {})
    flow_now = s.metadata.get("flow") or {}
    check("adjust: a tapped review row re-opens that step (confirm → collect)",
          flow_now.get("stage") == "collect" and flow_now.get("idx") == q_idx
          and flow_now.get("editing") is True,
          f"stage={flow_now.get('stage')} idx={flow_now.get('idx')} editing={flow_now.get('editing')}")
    check("adjust: the re-opened step leaves the review/confirm state",
          fr_edit.progress is not None and fr_edit.progress.get("confirming") is False)
    fr_back = await flows.handle(s, "Q3", {})
    flow_back = s.metadata.get("flow") or {}
    check("adjust: answering returns to review (pending re-armed, editing cleared)",
          getattr(fr_back, "pending", None) is not None
          and flow_back.get("stage") == "confirm" and not flow_back.get("editing"),
          f"stage={flow_back.get('stage')} pending={bool(getattr(fr_back,'pending',None))}")
    check("adjust: the edited value took (quarter → Q3, not the original Q1)",
          (getattr(fr_back, "pending", {}) or {}).get("tool_args", {}).get("quarter") == "Q3",
          (getattr(fr_back, "pending", {}) or {}).get("tool_args", {}).get("quarter"))

    # ── Terminal collapse: declining at review carries a cancelled rail so the widget folds the
    #    whole run into ONE card instead of leaving the answered steps as loose bubbles. ──
    tp = flows.terminal_progress(s.metadata.get("flow"), cancelled=True)
    check("terminal rail: complete + cancelled flags set, every step done",
          bool(tp) and tp.get("complete") is True and tp.get("cancelled") is True
          and all(st.get("state") == "done" for st in tp.get("steps", [])),
          None)
    # Completion collapses the SAME way (one card), just cancelled=False → the GREEN "completed"
    # card. This is exactly what chat_service stashes as `_flow_complete` before the flow is popped.
    _title = (s.metadata.get("flow", {}).get("labels") or {}).get("baselineTitle")
    tp_done = flows.terminal_progress(s.metadata.get("flow"))   # default cancelled=False
    check("completion rail: complete=True, cancelled=False, steps done, display name + title carried",
          bool(tp_done) and tp_done.get("complete") is True and tp_done.get("cancelled") is False
          and all(st.get("state") == "done" for st in tp_done.get("steps", []))
          and tp_done.get("title") == "Baseline Generation" and tp_done.get("primary") == _title,
          f"primary={tp_done.get('primary') if tp_done else None} title={_title}")
    # The completion callback finishes the flow with a friendly, title-bearing result (run on a
    # clone so the decline checks below still have the walked flow at confirm).
    _s_done = _Session()
    _s_done.metadata["flow"] = dict(s.metadata["flow"])
    fr_done = flows.baseline_after_generate(_s_done)
    check("completion: baseline_after_generate finishes (done, flow cleared, title in message)",
          fr_done is not None and getattr(fr_done, "done", False) is True
          and _s_done.metadata.get("flow") is None and (_title or "") in (fr_done.message or ""),
          None)
    fr_dec = flows.on_decline(s)
    check("decline at review returns a terminal (complete+cancelled) rail + done",
          fr_dec is not None and getattr(fr_dec, "progress", None) is not None
          and fr_dec.progress.get("complete") is True and fr_dec.progress.get("cancelled") is True
          and getattr(fr_dec, "done", False) is True,
          None)
    check("decline clears the flow", s.metadata.get("flow") is None)

    # ── Title guard: the start phrase can't become the title (leftover-flow safety). ──
    s2 = _Session()
    await flows.maybe_start(s2, "generate a formulation baseline", {})
    await flows.handle(s2, "generate a formulation baseline", {})   # re-fired trigger
    f2 = s2.metadata.get("flow") or {}
    check("title guard: the trigger phrase is NOT stored as the title (step holds at title)",
          (f2.get("labels") or {}).get("baselineTitle") in (None, "") and f2.get("idx") == 0,
          (f2.get("labels") or {}).get("baselineTitle"))
    await flows.handle(s2, "FY27 Planning Baseline", {})
    f2b = s2.metadata.get("flow") or {}
    check("title guard: a real title IS accepted and advances past the title step",
          (f2b.get("labels") or {}).get("baselineTitle") == "FY27 Planning Baseline"
          and f2b.get("idx", 0) >= 1,
          (f2b.get("labels") or {}).get("baselineTitle"))

    # ── Duplicate-title guard (read via the getBaseLineMasterHeaders_post MCP tool, scoped to the
    #    fund group): a taken title is BLOCKED at finalize and re-opens the title step to rename. ──
    _ti = next(i for i, s in enumerate(flows.BASELINE_STEPS) if s.field == "baselineTitle")
    _BY_TOOL["getBaseLineMasterHeaders_post"] = [{"title": "Dup Title"}, {"title": "Other"}]
    _dupf = {"name": "formulation_baseline", "stage": "confirm", "buf": [],
             "data": {"baselineTitle": "Dup Title", "fundGroupId": 1},
             "labels": {"baselineTitle": "Dup Title", "fundGroupId": "General Fund"}}
    _fr_dup = await flows._baseline_finalize(_dupf, {})
    check("dup-title guard: a taken title is BLOCKED (no pending) + re-opens the title step",
          getattr(_fr_dup, "pending", None) is None and _dupf.get("editing") is True
          and _dupf.get("idx") == _ti and _dupf.get("stage") == "collect",
          f"pending={getattr(_fr_dup,'pending',None)} editing={_dupf.get('editing')} idx={_dupf.get('idx')}")
    check("dup-title guard: case-insensitive match",
          "dup title" in await flows._existing_baseline_titles({"data": {"fundGroupId": 1}}, {}))
    _BY_TOOL["getBaseLineMasterHeaders_post"] = [{"title": "Something Else"}]
    _uf = {"name": "formulation_baseline", "stage": "confirm", "buf": [],
           "data": {"baselineTitle": "Unique FY27 Baseline", "fundGroupId": 1},
           "labels": {"baselineTitle": "Unique FY27 Baseline"}}
    _fr_uq = await flows._baseline_finalize(_uf, {})
    check("dup-title guard: a unique title proceeds (pending armed)",
          getattr(_fr_uq, "pending", None) is not None
          and _fr_uq.pending.get("tool_name") == "bobBaseLineGenerate_put", None)
    _BY_TOOL.pop("getBaseLineMasterHeaders_post", None)   # restore: no list → fail-open
    check("dup-title guard: fail-open (list unavailable) still proceeds",
          getattr(await flows._baseline_finalize(dict(_uf), {}), "pending", None) is not None)

    # ── Completion message mirrors the FE: shows the backend's OWN message + the title ──
    _sc = _Session(); _sc.metadata["flow"] = {"name": "formulation_baseline",
                                              "data": {"baselineTitle": "FY27 Ops Baseline"}, "labels": {}}
    _fr_ok = flows.baseline_after_generate(_sc, {"message": "Baseline generation is in process", "data": 123})
    check("completion: shows the backend message + title (FE toast.success(res.message))",
          _fr_ok is not None and "FY27 Ops Baseline" in _fr_ok.message
          and "in process" in _fr_ok.message and getattr(_fr_ok, "done", False) is True, None)

    # ── Failure re-arm: a rejected generate keeps the flow + re-arms the confirm for retry ──
    _sf = _Session(); _sf.metadata["flow"] = {"name": "formulation_baseline", "stage": "confirm",
                                              "data": {"baselineTitle": "X", "fundGroupId": 1}, "labels": {}}
    _fr_fl = await flows.baseline_after_failed_generate(
        _sf, {"success": False, "message": "Baseline generation is already in process"}, None)
    check("failure: re-arms the confirm with the backend error, flow kept for retry",
          _fr_fl is not None and getattr(_fr_fl, "pending", None) is not None
          and "already in process" in _fr_fl.message and _sf.metadata.get("flow") is not None, None)

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
                seasonal.seasonal_hints("FORMULATION", _dt.datetime(2026, 7, 1)) == [],
                "fired off-season"))  # Formulation window is Oct-May (10-5); Jul is outside it
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

    # Baseline review-rail render contract (so the widget shows long values without cutting off):
    #  multi-select → kind=list + items[] + "<n> selected"; description → kind=text; others scalar.
    from app.services import flows as _fl
    pf = {"name": "formulation_baseline", "stage": "confirm", "idx": len(_fl.BASELINE_STEPS),
          "labels": {"baselineTitle": "FY26 Baseline", "sourceFiscalYear": "2025",
                     "businessRuleGroups": ["A", "B", "C"], "description": "x" * 40}}
    prog = _fl._baseline_progress(pf)
    rg = next((s for s in prog["steps"] if str(s["label"]).startswith("Rule group")), {})
    out.append(("baseline rail: multi-select renders as a list block (count + items)",
                rg.get("kind") == "list" and rg.get("items") == ["A", "B", "C"]
                and rg.get("value") == "3 selected", f"got {rg!r}"))
    # Title → header name, Description → header subtitle; neither is a rail row, and there is no Review row.
    _labels = {str(s.get("label")) for s in prog["steps"]}
    out.append(("baseline header: title is the name/primary (not a rail row)",
                prog.get("primary") == "FY26 Baseline" and "Title" not in _labels, f"primary={prog.get('primary')!r}"))
    out.append(("baseline header: description is the subtitle (not a rail row)",
                prog.get("subtitle") == "x" * 40 and "Description" not in _labels, f"subtitle={prog.get('subtitle')!r}"))
    out.append(("baseline rail: no 'Review & confirm' row (header confirm state)",
                "Review & confirm" not in _labels and prog.get("confirming") is True, f"labels={_labels}"))
    sfy = next((s for s in prog["steps"] if s["label"] == "Source FY"), {})
    out.append(("baseline rail: a scalar stays a 1-line scalar",
                sfy.get("kind") == "scalar" and sfy.get("value") == "2025", f"got {sfy!r}"))
    # Character limits declared on the free-text steps (title / description).
    ts = next((s for s in _fl.BASELINE_STEPS if s.field == "baselineTitle"), None)
    ds = next((s for s in _fl.BASELINE_STEPS if s.field == "description"), None)
    out.append(("baseline: title & description have character limits",
                bool(ts and ts.maxlen == 100 and ds and ds.maxlen == 255),
                f"title={getattr(ts,'maxlen',None)} desc={getattr(ds,'maxlen',None)}"))
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
