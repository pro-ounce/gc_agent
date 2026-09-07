"""
services/ecosystem.py
─────────────────────
A cohesive, cached model of the GovConnect 360 (Compass) ecosystem so the agent can reason
about it as a whole — the applications that exist, the roles inside each, and the business
workflows they run — instead of only firing isolated tools.

Two backend reads give almost everything (both cheap, cached here with a short TTL):
  • getAllApplications_get        → the application catalogue (id, name, code)
  • getAllApplicationRoles_get    → every application-role, carrying human descriptions
                                    (roleDescription, applicationDesc/Info/Url) we ground on.

On top of the LIVE data we lay a small CURATED overlay (`_WORKFLOWS`) for the handful of
workflows where the backend text is thin — what the process is, its stages, and the concrete
things the agent can DO there (entry points). Live is the base; curated only enriches.

Everything is defensive: any backend hiccup degrades to an empty/partial model, never raises,
so a bad fetch can't break chat, grounding, or the widget bootstrap.
"""
from __future__ import annotations

import base64
import time
from typing import Any

from ..commons.logger import get_logger
from ..mcp.tool_registry import tool_registry

log = get_logger(__name__)

_TTL = 600.0  # seconds; the catalogue changes rarely, refetch at most every 10 min
_cache: dict[str, tuple[float, Any]] = {}


# ── curated workflow overlay (enriches live descriptions for key applications) ──
# keyed by applicationCode. `entry` items are things the agent can actually DO (they map to
# guided flows / skills) so "about <app>" can point the user straight at an action.
_WORKFLOWS: dict[str, dict[str, Any]] = {
    "FORMULATION": {
        "workflow": (
            "Budget **formulation** — authoring budget requests and moving them up an approval "
            "ladder. An author builds the request hierarchy (Budget Period master → headers → "
            "details → request → lines → distributions), submits it, and it is approved in "
            "turn by the sub-organization, the organization, the BOB, and finally the IOP."
        ),
        "stages": ["Author", "Submit", "Sub-org approval", "Org approval", "BOB", "IOP"],
        "entry": [("Set up a data call", "set up a data call")],
    },
    "EXECUTION": {
        "workflow": (
            "Budget **execution** — managing approved funds through the year: distributing "
            "allotments, tracking obligations and expenditures against the formulated plan."
        ),
        "stages": ["Allotment", "Commitment", "Obligation", "Expenditure"],
    },
    "CRP": {
        "workflow": (
            "**Allocation planning** — allocating funds across organizations with a "
            "facilitator/approver ladder (Budget Facilitator → Budget Approver → Executive Approver)."
        ),
    },
    "ADMINISTRATION": {
        "workflow": (
            "Platform **administration** — the people-and-access backbone: users, applications, "
            "roles and who can do what. This is where onboarding and access changes happen."
        ),
        "entry": [("Create a user", "create a user"),
                  ("Assign an application", "assign an application")],
    },
    "REPORTING": {
        "workflow": "**Reporting** — cross-application reports and exports over the platform's data.",
        "entry": [("User access report", "generate a user access report")],
    },
}


def _fresh(key: str) -> Any | None:
    hit = _cache.get(key)
    if hit and (time.monotonic() - hit[0]) < _TTL:
        return hit[1]
    return None


def _store(key: str, value: Any) -> Any:
    _cache[key] = (time.monotonic(), value)
    return value


def _rows(output: Any) -> list[dict[str, Any]]:
    """Pull the list payload out of a GC envelope (or a raw list)."""
    data = output.get("data") if isinstance(output, dict) else output
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


async def _fetch(tool: str, headers: dict[str, str] | None) -> list[dict[str, Any]]:
    try:
        res = await tool_registry.execute(tool, {}, headers)
        return _rows(res.output) if getattr(res, "success", False) else []
    except Exception as exc:  # noqa: BLE001 — the ecosystem model must never break callers
        log.bind(func="ecosystem").warning(f"{tool} failed: {exc}")
        return []


# ── applications ────────────────────────────────────────────────────────────────
async def applications(headers: dict[str, str] | None) -> list[dict[str, Any]]:
    """The application catalogue: [{id, name, code, desc, info, url}], enriched with the
    description fields carried on the application-roles rows (the apps endpoint omits them)."""
    cached = _fresh("apps")
    if cached is not None:
        return cached
    apps = await _fetch("getAllApplications_get", headers)
    roles = await app_roles(headers)
    # index descriptions by applicationId from the roles rows (they carry app metadata)
    meta: dict[str, dict[str, str]] = {}
    for r in roles:
        aid = str(r.get("applicationId") or "")
        if aid and aid not in meta:
            meta[aid] = {
                "desc": str(r.get("applicationDesc") or "").strip(),
                "info": str(r.get("applicationInfo") or "").strip(),
                "url": str(r.get("applicationUrl") or "").strip(),
            }
    out: list[dict[str, Any]] = []
    for a in apps:
        aid = str(a.get("applicationId") or "")
        m = meta.get(aid, {})
        out.append({
            "id": aid,
            "name": str(a.get("applicationName") or "").strip(),
            "code": str(a.get("applicationCode") or "").strip(),
            "desc": m.get("desc", ""),
            "info": m.get("info", ""),
            "url": m.get("url", ""),
        })
    out.sort(key=lambda x: x["name"].lower())
    return _store("apps", out)


async def app_roles(headers: dict[str, str] | None) -> list[dict[str, Any]]:
    """Every application-role (158-ish), each with role + application metadata."""
    cached = _fresh("roles")
    if cached is not None:
        return cached
    return _store("roles", await _fetch("getAllApplicationRoles_get", headers))


# ── resolution + description ──────────────────────────────────────────────────────
def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


async def resolve_app(name_or_code: str, headers: dict[str, str] | None) -> dict[str, Any] | None:
    """Find an application by (fuzzy) name or code — 'formulation', 'FORMULATION',
    'formulation planner' all resolve to the same app."""
    q = _norm(name_or_code)
    if not q:
        return None
    apps = await applications(headers)
    # exact code / name, then contains
    for a in apps:
        if _norm(a["code"]) == q or _norm(a["name"]) == q:
            return a
    for a in apps:
        if q in _norm(a["name"]) or q in _norm(a["code"]) or _norm(a["name"]).startswith(q):
            return a
    return None


async def roles_for(app: dict[str, Any], headers: dict[str, str] | None) -> list[dict[str, Any]]:
    """The roles defined for one application: [{name, desc, admin}] (de-duplicated by name)."""
    aid = str(app.get("id") or "")
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in await app_roles(headers):
        if str(r.get("applicationId") or "") != aid:
            continue
        name = str(r.get("roleName") or r.get("role") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({
            "name": name,
            "desc": str(r.get("roleDescription") or "").strip(),
            "admin": str(r.get("isAdmin") or "").upper() in ("Y", "TRUE", "1"),
        })
    out.sort(key=lambda x: (not x["admin"], x["name"].lower()))  # admins first
    return out


def workflow_for(code: str) -> dict[str, Any] | None:
    """The curated workflow overlay for an application code, if any."""
    return _WORKFLOWS.get((code or "").upper())


# ── grounding digest (compact, injected into the system prompt) ────────────────────
def selected_app_code(headers: dict[str, str] | None) -> str:
    """Decode the current application from the X-Selected-App header (base64 of the code)."""
    if not headers:
        return ""
    raw = headers.get("X-Selected-App") or headers.get("x-selected-app") or ""
    if not raw:
        return ""
    try:
        return base64.b64decode(raw).decode("utf-8", "ignore").strip()
    except Exception:  # noqa: BLE001
        return raw.strip()


async def grounding_digest(headers: dict[str, str] | None, limit: int = 30) -> str:
    """A compact, current snapshot of the ecosystem for the system prompt: the real
    applications (so the model never invents one), plus the currently-selected application
    and its roles when the widget provides one. Kept short to protect the context window."""
    apps = await applications(headers)
    if not apps:
        return ""
    listing = ", ".join(f"{a['name']} [{a['code']}]" for a in apps[:limit])
    lines = [
        "GC360 ECOSYSTEM (ground every answer in these real applications — never invent one):",
        f"Applications ({len(apps)}): {listing}.",
    ]
    code = selected_app_code(headers)
    if code:
        app = await resolve_app(code, headers)
        if app:
            roles = await roles_for(app, headers)
            rnames = ", ".join(r["name"] for r in roles[:12]) or "—"
            desc = app["desc"] or app["info"]
            wf = workflow_for(app["code"])
            line = f"Current application: {app['name']} [{app['code']}]"
            if desc:
                line += f" — {desc.rstrip('.')}"
            lines.append(line + ".")
            lines.append(f"Its roles: {rnames}.")
            if wf and wf.get("workflow"):
                lines.append("Workflow: " + wf["workflow"].replace("**", ""))
    lines.append(
        "For questions about applications, roles, who has access, or a user's access, use the "
        "platform's read tools and answer from the returned data."
    )
    return "\n" + "\n".join(lines) + "\n"
