"""
eval/flow_breakout_eval.py
──────────────────────────
Gate for the self-healing new-intent BREAK-OUT (flows._should_break_out / flows.handle). A
long-lived session keeps its flow state, so an abandoned flow must not swallow a clearly-new
intent. Invariants:

  • A clear new flow-intent that isn't the active flow's continuation breaks out (handle returns
    reroute + clears the flow) so it re-routes fresh instead of being eaten or hitting the LLM.
  • A legitimate FIELD value (a role name, an ambiguous 'new …' with no entity noun) does NOT
    break out — the flow continues.
  • The SAME intent as the active flow does NOT break out (no needless restart).

    APP_SKIP_INIT=1 PYTHONPATH=/apps/gc_agent ./bin/python eval/flow_breakout_eval.py
"""
from __future__ import annotations

import asyncio
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


_APPS = [{"applicationId": 3, "applicationName": "Formulation Planner", "applicationCode": "FORMULATION", "enabled": "Y"}]
_ROLES = [{"applicationRoleId": 10, "applicationId": 3, "roleName": "Budget Admin", "role": "BUDGET_ADMIN", "enabled": "Y", "isSeeded": "No"}]


async def _fake(t, a=None, h=None):
    if t == "getAllApplications_get":
        return _Res(_APPS)
    if t == "getApplicationRolesByAppId_get":
        return _Res(_ROLES)
    return _Res([])


class _S:
    def __init__(self):
        self.metadata: dict = {}


async def _create_role_at_name(s):
    flows.start_entity_create(s, "role")
    await flows.handle(s, "Formulation Planner", {})   # answer app → now collecting roleName


async def run():
    tool_registry.execute = _fake  # type: ignore[assignment]
    r = []

    def check(n, c, d=""):
        r.append((n, bool(c), d))

    s = _S(); await _create_role_at_name(s)
    fr = await flows.handle(s, "remove the QA_REVIEWER role in Formulation", {})
    check("create-role + 'remove a role' breaks out",
          fr is not None and fr.reroute and s.metadata.get("flow") is None)

    s = _S(); await _create_role_at_name(s)
    fr = await flows.handle(s, "Budget Reviewer", {})
    check("field value does NOT break out",
          (s.metadata.get("flow") or {}).get("name") == "entity_create" and not (fr and fr.reroute))

    s = _S(); flows.maybe_start(s, "remove a role in Formulation")
    fr = await flows.handle(s, "remove a role in Formulation", {})
    check("same intent does NOT break out",
          (s.metadata.get("flow") or {}).get("name") == "remove_role" and not (fr and fr.reroute))

    s = _S(); await _create_role_at_name(s)
    fr = await flows.handle(s, "create a menu in Formulation", {})
    check("different-entity create breaks out",
          fr is not None and fr.reroute and s.metadata.get("flow") is None)

    s = _S(); await _create_role_at_name(s)
    fr = await flows.handle(s, "create a role", {})
    check("same-entity create does NOT break out",
          (s.metadata.get("flow") or {}).get("name") == "entity_create")

    s = _S(); await _create_role_at_name(s)
    await flows.handle(s, "Budget Reviewer", {})
    fr = await flows.handle(s, "New reviewer for budgets", {})
    check("ambiguous 'new …' (no entity noun) does NOT break out",
          (s.metadata.get("flow") or {}).get("name") == "entity_create")

    return r


def main() -> int:
    results = asyncio.run(run())
    passed = sum(1 for _, ok, _ in results if ok)
    for n, ok, d in results:
        print(f"  {'OK  ' if ok else 'FAIL'} {n}")
    print(f"\nsummary: {passed}/{len(results)} break-out checks passed")
    print("  ✓ self-healing break-out holds" if passed == len(results) else "  ✗ break-out regression")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
