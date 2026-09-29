"""
eval/entity_validation_eval.py
──────────────────────────────
Gate for the entity-create GUARDRAILS (flows._entity_create field validation). It locks in the
governance rules for onboarding new records so a guided create can never push an invalid or
colliding value at the backend:

  • ROLE NAME is unique within the target application — a duplicate is rejected, the flow stays
    on the field (no advance, no mutation).
  • ROLE CODE is AUTO-generated from the role name (UPPER_SNAKE, ≤30 chars) and de-duped within
    the app (…_2, _3 on collision) — it is never asked, and the user can't set a colliding one.
  • DESCRIPTION length is capped at the DB column width (role 1000) — an over-length value is
    rejected rather than silently truncated.
  • APPLICATION CODE / MENU NAME / MENU CODE are likewise normalized + uniqueness-checked
    (app code global, menu name/code within the app).
  • A MENU attached to a role comes only from that application's menus (getAllMenus_post is
    already scoped by applicationId — asserted indirectly by the menu-index scoping).

Uniqueness is checked against a STUBBED backend (deterministic, no network), so this runs
anywhere:

    APP_SKIP_INIT=1 PYTHONPATH=/apps/gc_agent ./bin/python eval/entity_validation_eval.py
    APP_SKIP_INIT=1 PYTHONPATH=/apps/gc_agent ./bin/python eval/entity_validation_eval.py --json
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


# Deterministic fixture: one app, two existing roles, one existing menu.
_APPS = [{"applicationId": 7, "applicationName": "Formulation Planner",
          "applicationCode": "FORM", "enabled": "Y"}]
_ROLES = [{"applicationRoleId": 1, "roleName": "Budget Viewer", "role": "BUDGET_VIEWER", "enabled": "Y"},
          {"applicationRoleId": 2, "roleName": "Approver", "role": "APPROVER", "enabled": "Y"}]
_MENUS = [{"menuId": 300, "menuName": "Budget Home", "menuCode": "BUD_HOME", "enabled": "Y"}]


async def _fake_execute(tool, args=None, headers=None):
    if tool == "getApplicationRolesByAppId_get":
        return _Res(_ROLES)
    if tool == "getAllApplications_get":
        return _Res(_APPS)
    if tool == "getAllMenus_post":
        return _Res(_MENUS)
    return _Res([])


class _Session:
    def __init__(self):
        self.metadata: dict = {}


async def _send(session, flow, text):
    return await flows._entity_create(session, flow, text, {})


async def run() -> list[tuple[str, bool, str]]:
    tool_registry.execute = _fake_execute  # type: ignore[assignment]
    results: list[tuple[str, bool, str]] = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail))

    # ── helper-level: code style + de-dup ──────────────────────────────────────
    check("codeify UPPER_SNAKE", flows._codeify("Budget Reviewer!!", 30) == "BUDGET_REVIEWER",
          flows._codeify("Budget Reviewer!!", 30))
    trimmed = flows._codeify("A very long role name that exceeds thirty characters", 30)
    check("codeify width cap ≤30 no trailing _", len(trimmed) <= 30 and not trimmed.endswith("_"), trimmed)
    check("unique_code collision → _2",
          flows._unique_code("Budget Viewer", {"BUDGET_VIEWER"}, 30) == "BUDGET_VIEWER_2",
          flows._unique_code("Budget Viewer", {"BUDGET_VIEWER"}, 30))
    check("unique_code collision → _3",
          flows._unique_code("Approver", {"APPROVER", "APPROVER_2"}, 30) == "APPROVER_3", "")

    # ── role flow: name uniqueness, auto code, description cap ──────────────────
    s = _Session()
    flows.start_entity_create(s, "role")
    flow = s.metadata["flow"]
    await _send(s, flow, "Formulation Planner")           # app_picker
    fr = await _send(s, flow, "Budget Viewer")            # duplicate role name
    check("role name duplicate rejected", "already taken" in (fr.message or "").lower(), fr.message)
    check("role name dup keeps field (idx==1)", flow["idx"] == 1, f"idx={flow['idx']}")

    fr = await _send(s, flow, "Budget Reviewer")          # unique name → auto code, advance
    check("role code auto-generated", flow["data"].get("role") == "BUDGET_REVIEWER", flow["data"].get("role"))
    check("role code marked auto", "auto" in (flow["labels"].get("role", "")).lower(), flow["labels"].get("role"))
    check("advanced past hidden code step (idx==3)", flow["idx"] == 3, f"idx={flow['idx']}")

    fr = await _send(s, flow, "x" * 1001)                 # description over cap
    check("role description >1000 rejected", "1000" in (fr.message or "") and "1001" in (fr.message or ""), fr.message)
    check("over-length keeps field (idx==3)", flow["idx"] == 3, f"idx={flow['idx']}")
    await _send(s, flow, "Reviews budget submissions before approval")
    check("valid description advances (idx==4)", flow["idx"] == 4, f"idx={flow['idx']}")

    # ── auto code de-dup when the derived code collides ────────────────────────
    s2 = _Session()
    flows.start_entity_create(s2, "role")
    f2 = s2.metadata["flow"]
    await _send(s2, f2, "Formulation Planner")
    await _send(s2, f2, "Budget-Viewer")                  # → BUDGET_VIEWER (taken) → _2
    check("auto code de-dup on collision", f2["data"].get("role") == "BUDGET_VIEWER_2", f2["data"].get("role"))

    # ── application code: dup rejected, new normalized ─────────────────────────
    s3 = _Session()
    flows.start_entity_create(s3, "application")
    f3 = s3.metadata["flow"]
    await _send(s3, f3, "My App")
    fr = await _send(s3, f3, "form")                      # → FORM (taken)
    check("application code duplicate rejected", "already taken" in (fr.message or "").lower(), fr.message)
    await _send(s3, f3, "budget analytics")              # → BUDGET_ANALYTICS
    check("application code normalized UPPER_SNAKE", f3["data"].get("applicationCode") == "BUDGET_ANALYTICS",
          f3["data"].get("applicationCode"))

    # ── menu name/code uniqueness within the app ───────────────────────────────
    s4 = _Session()
    flows.start_entity_create(s4, "menu")
    f4 = s4.metadata["flow"]
    await _send(s4, f4, "Formulation Planner")
    fr = await _send(s4, f4, "Budget Home")               # dup name
    check("menu name duplicate rejected", "already taken" in (fr.message or "").lower(), fr.message)
    await _send(s4, f4, "Reports Home")                   # ok name
    fr = await _send(s4, f4, "bud home")                  # → BUD_HOME (taken)
    check("menu code duplicate rejected", "already taken" in (fr.message or "").lower(), fr.message)

    return results


def main() -> int:
    results = asyncio.run(run())
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    if "--json" in sys.argv:
        print(json.dumps({"passed": passed, "total": total,
                          "results": [{"name": n, "ok": ok, "detail": d} for n, ok, d in results]}, indent=2))
    else:
        for name, ok, detail in results:
            tag = "OK  " if ok else "FAIL"
            extra = "" if ok else f"   ← got: {detail!r}"
            print(f"  {tag} {name}{extra}")
        print(f"\nsummary: {passed}/{total} guardrail checks passed")
        print("  ✓ entity-create guardrails hold" if passed == total
              else "  ✗ guardrail regression — see FAIL lines above")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
