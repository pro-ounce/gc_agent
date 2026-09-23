"""
services/intent_classifier.py
─────────────────────────────
Model-based intent classifier (router redesign — Option B, primary). ONE structured call to the
local model maps a query to a fixed route + a calibrated confidence. Unlike the regex extractor,
it GENERALIZES to unseen phrasings ("look up user gcadmin", "who is gcadmin", "gcadmin's stuff")
without a rule per case.

Separation of concerns: the classifier picks only the INTENT (which route). The concrete SLOTS
(which app/user/role) stay deterministic — `intent_schema.build_app_index` + the named-user
resolver — so the model never has to get a canonical code exactly right. Below `TAU` (or route
"other") the caller ABSTAINS → escalate. Fail-open: any model/parse error returns ("", 0.0) so
the pipeline falls through exactly as today.

NOT wired into the live pipeline yet — build + benchmark first (eval/classifier_eval.py) at the
0-confident-wrong bar, same discipline that shelved the embedding classifier.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..commons.logger import get_logger
from .llm_service import llm

log = get_logger(__name__)

# Abstain below this confidence → escalate to the full LLM+tools path. Calibrated on the corpus.
TAU = 0.6

# The fixed route vocabulary the model must choose from — one line each. These are the exact
# handler names `intent_schema.route()` emits (plus "other" → escalate).
ROUTES: dict[str, str] = {
    "list_apps": "list the applications / what apps exist / the catalogue",
    "app_roles": "list the roles that exist in ONE named application",
    "roles_catalog": "list ALL roles across EVERY application",
    "my_access": "the CALLER's own applications / what can I access",
    "my_access_in_app": "the CALLER's own roles in ONE named app, incl. 'do I have <role> in <app>'",
    "my_roles_all": "the CALLER's own roles across all applications",
    "named_access": "a NAMED OTHER user's access/roles: 'what can X do', \"X's roles\", 'look up user X', 'what is X'",
    "app_users": "WHO can access / which users are in ONE named application",
    "users_list": "list ALL users in the system",
    "fund_groups": "list fund groups",
    "organizations": "list organizations / offices",
    "divisions": "list divisions",
    "my_offices": "the CALLER's own offices / 'where can I work'",
    "about_app": "describe / 'about' / 'what is' ONE named application",
    "org_level": "list sub-orgs or program offices",
    "fiscal_years": "list fiscal years",
    "skill_or_flow": "an ACTION/mutation — create/add/register/onboard/assign/grant/revoke/remove/edit a user or role, or a guided setup flow",
    "other": "anything else, unclear, or chit-chat → escalate",
}

_ROUTE_KEYS = set(ROUTES)


def _system_prompt(catalogue: list[dict[str, Any]]) -> str:
    apps = ", ".join(f"{a.get('name')} ({a.get('code')})" for a in (catalogue or [])[:40] if a.get("code"))
    routes = "\n".join(f"- {k}: {v}" for k, v in ROUTES.items())
    return (
        "You are an INTENT CLASSIFIER for a federal budget-system assistant. Read the user's "
        "message and choose the ONE route that best matches what they want.\n"
        'Reply with ONLY a compact JSON object, no prose: {"route":"<route key>","confidence":<0.0-1.0>}.\n\n'
        "Routes:\n" + routes + "\n\n"
        "Applications (display name → code):\n" + apps + "\n\n"
        "Rules:\n"
        "- Any ACTION on a user/role (create, add, register, onboard, assign, grant, revoke, "
        "remove, edit) → skill_or_flow, regardless of detail.\n"
        "- First person ('my', 'I', 'do I have', 'where can I') → the my_* routes (the caller).\n"
        "- A named other person → named_access. 'roles in <app>' (no person) → app_roles.\n"
        "- If you are unsure or it doesn't fit, use \"other\" with a low confidence.\n"
        "- confidence = how sure you are of the route (calibrated: only ≥0.6 when clearly right)."
    )


_JSON = re.compile(r"\{[^{}]*\}", re.S)


def _parse(text: str) -> tuple[str, float]:
    m = _JSON.search(text or "")
    if not m:
        return ("", 0.0)
    obj = json.loads(m.group(0))
    route = str(obj.get("route", "")).strip()
    try:
        conf = float(obj.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if route not in _ROUTE_KEYS:
        return ("", round(conf, 2))
    return (route, round(max(0.0, min(1.0, conf)), 2))


async def classify(query: str, catalogue: list[dict[str, Any]]) -> tuple[str, float]:
    """Predict (route, confidence). route "" means abstain → escalate (below TAU, unknown, or the
    model's own "other"). Fail-open: model/parse failure → ("", 0.0)."""
    q = (query or "").strip()
    if not q:
        return ("", 0.0)
    try:
        resp = await llm().complete([{"role": "user", "content": q}], [], _system_prompt(catalogue))
        route, conf = _parse(resp.text or "")
    except Exception as exc:  # noqa: BLE001 — never break a turn on the classifier
        log.warning(f"intent classify failed: {exc}")
        return ("", 0.0)
    if not route or route == "other" or conf < TAU:
        return ("", conf)
    return (route, conf)


async def score(query: str, catalogue: list[dict[str, Any]]) -> tuple[str, float]:
    """Raw prediction WITHOUT the abstain threshold — (route_or_other, confidence) — for
    τ-calibration in the benchmark. `classify` applies TAU."""
    q = (query or "").strip()
    if not q:
        return ("", 0.0)
    try:
        resp = await llm().complete([{"role": "user", "content": q}], [], _system_prompt(catalogue))
        return _parse(resp.text or "")
    except Exception as exc:  # noqa: BLE001
        log.warning(f"intent score failed: {exc}")
        return ("", 0.0)
