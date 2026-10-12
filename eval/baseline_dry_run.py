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

_BY_TOOL = {
    "getAllFiscalYears_post": _FISCAL_YEARS,
    "getAllValidationGroups_post": _RULE_GROUPS,
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
    pending = None
    for i, (msg, note) in enumerate(_SCRIPT):
        fr = await flows.handle(s, msg, {})
        rail_ok = rail_ok and fr.progress is not None   # every step stays in the FlowWindow
        if i == 0:  # the source-FY prompt right after 'start'
            first_fy_chips = [c.get("label") for c in (fr.suggestions or []) if isinstance(c, dict)]
        if verbose:
            print(f"\nUSER:  {msg!r}   ({note})")
            print("AGENT:", (fr.message or "").replace("\n", " "))
            print(_chips(fr))
        if getattr(fr, "pending", None):
            pending = fr.pending

    check("source-FY chips are year numbers", "2026" in first_fy_chips and "2025" in first_fy_chips,
          first_fy_chips)
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
    check("healthCheck sent explicitly", args.get("healthCheck") == "N", args.get("healthCheck"))
    check("applicationCode FORMULATION", args.get("applicationCode") == "FORMULATION",
          args.get("applicationCode"))

    return r


def main() -> int:
    results = asyncio.run(run(verbose=True))
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
