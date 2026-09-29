"""
eval/remove_role_eval.py
────────────────────────
Gate for the guided REMOVE-ROLE flow (flows._detect_remove_role → start_remove_role → _remove_role).
Locks in the governance rules for deleting a role DEFINITION:

  • Detection fires only for deleting a role definition — NOT for un-assigning a role from a user
    or removing access (those belong to the access skills). Precision over recall.
  • Before any delete, the flow SURFACES dependents: the user→role assignments a hard delete
    cascades (named + counted) and any privileges, and notes the attached menu is preserved.
  • A SEEDED role is refused (the platform protects seeded data) — the flow ends, no mutation.
  • The delete is confirm-gated (pending deleteApplicationRoleById_delete) and lives in the
    access-mutation set, so the admin authority gate applies.

Backend is stubbed (deterministic, no network):

    APP_SKIP_INIT=1 PYTHONPATH=/apps/gc_agent ./bin/python eval/remove_role_eval.py
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


_APPS = [{"applicationId": 3, "applicationName": "Formulation Planner",
          "applicationCode": "FORMULATION", "enabled": "Y"}]
_ROLES = [
    {"applicationRoleId": 2300, "applicationId": 3, "roleName": "QA Reviewer", "role": "QA_REVIEWER",
     "enabled": "Y", "isSeeded": "No", "menuName": "Budget Facilitator", "menuCode": "BUDGET_FACILITATOR",
     "roleDescription": "QA review-only role", "isAdmin": "N"},
    {"applicationRoleId": 10, "applicationId": 3, "roleName": "Budget Admin", "role": "BUDGET_ADMIN",
     "enabled": "Y", "isSeeded": "Yes"},
]
_UAR = [
    {"applicationRoleId": "2300", "userName": "JDOE", "firstName": "Jane", "lastName": "Doe", "enabled": "Y"},
    {"applicationRoleId": "2300", "userName": "BSMITH", "firstName": "Bob", "lastName": "Smith", "enabled": "Y"},
    {"applicationRoleId": "999", "userName": "OTHER", "firstName": "Not", "lastName": "Related", "enabled": "Y"},
]


async def _fake(tool, args=None, headers=None):
    if tool == "getAllApplications_get":
        return _Res(_APPS)
    if tool == "getApplicationRolesByAppId_get":
        return _Res(_ROLES)
    if tool == "getAllUserApplicationRoles_post":
        return _Res(_UAR)
    if tool == "getAllAppRolePrivileges_post":
        return _Res([])
    return _Res([])


class _Session:
    def __init__(self):
        self.metadata: dict = {}


async def run() -> list[tuple[str, bool, str]]:
    tool_registry.execute = _fake  # type: ignore[assignment]
    r: list[tuple[str, bool, str]] = []

    def check(name, cond, detail=""):
        r.append((name, bool(cond), detail))

    # detection
    check("detect delete role def", flows._detect_remove_role("delete the QA_REVIEWER role"))
    check("detect remove a role", flows._detect_remove_role("remove a role in Formulation"))
    check("no fire: un-assign from user", not flows._detect_remove_role("remove the QA role from JDOE"))
    check("no fire: remove access", not flows._detect_remove_role("remove access to Formulation"))
    check("no fire: create a role", not flows._detect_remove_role("create a role"))

    # start via maybe_start
    s = _Session()
    fr = flows.maybe_start(s, "delete a role in Formulation")
    check("maybe_start arms remove_role", (s.metadata.get("flow") or {}).get("name") == "remove_role")
    check("start shows rail", bool(fr.progress) and fr.progress.get("title") == "Remove application role")

    fr = await flows.handle(s, "Formulation Planner", {})
    check("app → role picker stage", s.metadata["flow"]["stage"] == "pick_role")
    check("role chips offered", any(c["label"] == "QA Reviewer" for c in (fr.suggestions or [])))

    fr = await flows.handle(s, "QA Reviewer", {})
    check("preview pending is delete", bool(fr.pending) and fr.pending["tool_name"] == "deleteApplicationRoleById_delete")
    check("preview targets role 2300", (fr.pending or {}).get("tool_args", {}).get("applicationRoleId") == "2300")
    check("preview lists assigned users", "2 user(s)" in (fr.message or "") and "Jane Doe" in fr.message and "Bob Smith" in fr.message)
    check("preview keeps attached menu", "Budget Facilitator" in (fr.message or "") and "stays" in (fr.message or ""))
    check("delete is an access mutation (gated)", flows.is_access_mutation("deleteApplicationRoleById_delete"))

    # seeded role refused
    s2 = _Session()
    flows.maybe_start(s2, "delete a role in Formulation")
    await flows.handle(s2, "Formulation Planner", {})
    fr = await flows.handle(s2, "Budget Admin", {})
    check("seeded role refused", "seeded" in (fr.message or "").lower() and fr.done)
    check("seeded refusal ends flow", s2.metadata.get("flow") is None)

    return r


def main() -> int:
    results = asyncio.run(run())
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    for name, ok, detail in results:
        tag = "OK  " if ok else "FAIL"
        extra = "" if ok else f"   ← {detail!r}"
        print(f"  {tag} {name}{extra}")
    print(f"\nsummary: {passed}/{total} remove-role checks passed")
    print("  ✓ remove-role flow holds — deps surfaced, seeded protected, delete gated"
          if passed == total else "  ✗ remove-role regression")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
