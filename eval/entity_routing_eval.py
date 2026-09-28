"""
eval/entity_routing_eval.py
───────────────────────────
Gate for the entity onboarding front door (flows._detect_entity → flows.maybe_start). It
locks in the governance rule: "onboard" / "create" / "add" is overloaded across platform
entities, so an ambiguous request must ASK which entity rather than firing a wrong-entity
mutation, and each clear entity must reach its OWN create flow.

Invariants enforced (exit non-zero on any violation):
  • a clear entity request opens that entity's create flow (user → create_user; application/
    role/menu/privilege → entity_create with that entity);
  • an ambiguous onboard ("onboard", "onboard a new one") opens the DISAMBIGUATION prompt,
    never a create;
  • a non-create request (assign/grant/read) and another known task (data call/skill/…) are
    NOT hijacked by the front door (maybe_start returns None → normal routing);
  • no create/onboard phrase is ever routed into a mutation flow for the WRONG entity.

Routing is regex + keyword only (no embeddings, no backend), so this runs anywhere:

    APP_SKIP_INIT=1 PYTHONPATH=/apps/gc_agent ./bin/python eval/entity_routing_eval.py
    APP_SKIP_INIT=1 PYTHONPATH=/apps/gc_agent ./bin/python eval/entity_routing_eval.py --json
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("APP_SKIP_INIT", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import flows  # noqa: E402


class _Session:
    def __init__(self):
        self.metadata: dict = {}


# (phrase, expected) — expected is one of:
#   "create_user"                → the user intake flow
#   "entity:<name>"              → entity_create flow, that entity, collecting
#   "disambiguate"               → entity_create flow, asking which entity
#   "none"                       → NOT hijacked (maybe_start returns None)
CASES: list[tuple[str, str]] = [
    # clear entities
    ("create a role", "entity:role"),
    ("add a new role", "entity:role"),
    ("onboard a role", "entity:role"),
    ("create an application role", "entity:role"),
    ("onboard an application", "entity:application"),
    ("create a new application", "entity:application"),
    ("register a module", "entity:application"),
    ("add a menu", "entity:menu"),
    ("create a menu", "entity:menu"),
    ("onboard a privilege", "entity:privilege"),
    ("create a permission", "entity:privilege"),
    # user (existing intake)
    ("onboard a user", "create_user"),
    ("create a user", "create_user"),
    ("onboard someone", "create_user"),
    ("onboard a new person", "create_user"),
    # ambiguous → must ask
    ("onboard", "disambiguate"),
    ("onboard a new one", "disambiguate"),
    # must NOT be hijacked by the ENTITY front door — each has its own handler
    ("create a data call", "none"),          # → create_data_call skill (normal routing)
    ("set up a data call", "none"),
    ("create a skill", "create_skill"),      # skill-authoring flow front-runs
    ("assign a role to GCADMIN", "none"),    # → assign_access (normal routing)
    ("grant GCADMIN access to Formulation", "workflow"),  # → grant_access workflow
    ("who can access Formulation", "none"),
    ("list applications", "none"),
    # a fully-detailed create-user (has an email) stays on the normal skill path
    ("create user JDOE jane@agency.gov", "none"),
]

# Entities whose flow is a mutation — used to assert no wrong-entity leak.
_MUT_ENTITIES = {"application", "role", "menu", "privilege"}


def classify(phrase: str) -> tuple[str, str | None]:
    """Return (result, entity) for a phrase via the real maybe_start routing."""
    s = _Session()
    fr = flows.maybe_start(s, phrase)
    flow = s.metadata.get("flow")
    if fr is None and not flow:
        return "none", None
    name = (flow or {}).get("name")
    if name == "create_user":
        return "create_user", None
    if name == "entity_create":
        stage = flow.get("stage")
        if stage == "disambiguate":
            return "disambiguate", None
        return "entity", flow.get("entity")
    return name or "none", None


def main() -> int:
    as_json = "--json" in sys.argv
    rows = []
    fails = 0
    leaks = 0
    for phrase, want in CASES:
        res, ent = classify(phrase)
        got = f"entity:{ent}" if res == "entity" else res
        ok = (got == want)
        # wrong-entity leak: a create/onboard phrase landed on a mutation entity that isn't
        # the one asked for (only meaningful when the expectation names an entity).
        leak = False
        if want.startswith("entity:") and res == "entity" and ent in _MUT_ENTITIES and got != want:
            leak = True
            leaks += 1
        if not ok:
            fails += 1
        rows.append((phrase, want, got, ok, leak))

    if as_json:
        print(json.dumps({
            "total": len(CASES), "fails": fails, "wrong_entity_leaks": leaks,
            "rows": [{"phrase": p, "want": w, "got": g, "ok": ok} for (p, w, g, ok, _l) in rows],
        }, indent=2))
        return 0 if fails == 0 else 1

    print(f"entity routing eval — {len(CASES)} cases\n")
    for phrase, want, got, ok, leak in rows:
        mark = "OK  " if ok else ("LEAK" if leak else "FAIL")
        print(f"  {mark} {phrase!r:40} want={want:20} got={got}")
    print(f"\nsummary: {len(CASES)-fails}/{len(CASES)} correct · {fails} fail · "
          f"{leaks} wrong-entity leak")
    print("  " + ("✓ front door holds — no wrong-entity mutation, ambiguity is asked"
                  if fails == 0 else "✗ routing regressed"))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
