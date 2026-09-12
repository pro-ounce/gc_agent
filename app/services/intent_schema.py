"""
services/intent_schema.py
─────────────────────────
Option A of the router redesign: parse a query ONCE into a structured Intent
(action · entity · subject · scope) and dispatch on that, instead of an ordered
list of regex cues. Scope can't be silently dropped (it's a field), and a new
phrasing is one extraction tweak, not another ordered cue competing for priority.

Two accuracy hooks live here so we can tell whether a route is trustworthy:
  • Intent.confidence — how well the deterministic extractor resolved the query.
  • route() returns "" (UNKNOWN) when no rule matches.
Either signal → the caller escalates to the LLM router (Option B) as a fallback.

This module is PURE (no I/O) so it is fully unit-/eval-testable offline; the live
router passes the current application catalogue in for app-name resolution.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ── controlled vocabularies ───────────────────────────────────────────────────
ACTIONS = ("list", "count", "describe", "who", "assign", "remove", "edit", "generate")
# Organizations are a self-referential tree: org → sub-org → program office → division.
ENTITIES = ("application", "role", "user", "fund_group", "organization", "sub_org",
            "program_office", "division", "fiscal_year", "data_call", "baseline")
SUBJECTS = ("self", "user", "all")   # who the query is about


@dataclass
class Intent:
    action: str = "list"          # what to do (default read = list)
    entity: str = ""              # what it's about (application / role / user / …)
    subject: str = "all"          # self · user (a named user) · all
    user: str = ""                # the named user, when subject == "user"
    app: str = ""                 # application code/name, when scoped to one app
    role: str = ""                # role name, when scoped to one role
    filters: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0       # 0..1 — deterministic extractor's self-assessment
    raw: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"action": self.action, "entity": self.entity, "subject": self.subject,
                "user": self.user, "app": self.app, "role": self.role,
                "filters": self.filters, "confidence": round(self.confidence, 2)}


# ── cue patterns (used to FILL slots, never to pick a handler directly) ───────
_ACTION_CUES: list[tuple[str, str]] = [
    ("assign", r"\b(assign|grant|allocate|provision|give)\b"),
    ("remove", r"\b(remove|revoke|unassign|de-?assign|take away|strip)\b"),
    ("edit", r"\b(make|set|mark)\b[^.?]{0,40}\b(default|favou?rite)\b|\b(enable|disable)\b"),
    ("generate", r"\b(generate|run|create|build)\b[^.?]{0,24}\bbaseline\b"),
    # create/add a user or account → a MUTATION; must leave the read router. Ordered AFTER
    # generate so "create ... baseline" still routes to the baseline flow, not here.
    ("create", r"\b(create|add|register|onboard)\b|\bnew (user|account|person)\b"),
    ("who", r"\bwho (can|has|have|is|are)\b|\bwhich users?\b"),
    ("count", r"\bhow many\b|\bcount of\b|\bnumber of\b"),
    ("describe", r"\b(about|describe|what'?s|what is|tell me about|explain|overview of)\b"),
]
# Entity cues, most specific first (multi-word beats single).
_ENTITY_CUES: list[tuple[str, str]] = [
    ("baseline", r"\bbaselines?\b"),
    ("data_call", r"\bdata calls?\b"),
    ("fiscal_year", r"\bfiscal years?\b|\bbfy\b"),
    ("fund_group", r"\bfund ?groups?\b"),
    # org hierarchy (most specific first): sub-org · program office · division · organization
    ("sub_org", r"\bsub[ -]?(?:orgs?|organi[sz]ations?)\b"),
    ("program_office", r"\bprogram[ -]?offices?\b|\bprog\.? ?office\b"),
    ("division", r"\bdivisions?\b"),
    ("organization", r"\borgani[sz]ations?\b|\borgs?\b|\boffices?\b|\bwhere can i (?:work|act)\b"),
    ("role", r"\broles?\b"),
    ("user", r"\busers?\b|\baccounts?\b"),
    ("application", r"\bapplications?\b|\bapps?\b|\baccess\b"),
]
_SELF = re.compile(r"\b(my|mine|i|me)\b|\bwhat can i\b|\bdo i have\b", re.I)
_STOP_USER = {"i", "you", "we", "my", "me", "us", "the", "a", "an", "this",
              "that", "user", "everyone", "all"}
# Named-user patterns (same set the legacy extractor learned, kept in one place).
_USER_PATS = [
    re.compile(r"what can ([A-Za-z0-9._@-]+) (?:do|access)\b", re.I),
    re.compile(r"what (?:applications?|apps?|access|roles?) (?:does|do|has|have) ([A-Za-z0-9._@-]+)", re.I),
    re.compile(r"what (?:does|do|has|have) ([A-Za-z0-9._@-]+)\b", re.I),
    re.compile(r"\baccess (?:for|of) ([A-Za-z0-9._@-]+)", re.I),
    re.compile(r"\b(?:applications?|apps?|access|roles?) (?:assigned|granted) to (?:the )?([A-Za-z0-9._@-]+)", re.I),
    re.compile(r"\bassigned to (?:the )?([A-Za-z0-9._@-]+)", re.I),
    re.compile(r"\b([A-Za-z0-9._@-]+)'s (?:access|applications?|roles?)\b", re.I),
    re.compile(r"\b(?:for|of) (?:user )?([A-Za-z0-9._@-]+)\b", re.I),
]


def _norm(s: Any) -> str:
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


def _match_app(msg: str, apps: dict[str, str]) -> str:
    """Resolve a mentioned application to its code. `apps` maps normalised name/code → code.
    Whole-word match so 'budgeting' never matches the Budget app."""
    words = set(re.findall(r"[a-z0-9]+", msg.lower()))
    # try the longest known labels first (multi-word names)
    for label in sorted(apps, key=len, reverse=True):
        toks = label.split()
        if all(t in words for t in toks) and toks:
            return apps[label]
    return ""


def _named_user(msg: str) -> str:
    for pat in _USER_PATS:
        m = pat.search(msg)
        if m:
            tok = m.group(1).strip().strip(".'")
            if tok and tok.lower() not in _STOP_USER and len(tok) >= 2:
                return tok
    return ""


def extract_intent(query: str, apps: dict[str, str] | None = None) -> Intent:
    """Deterministically parse a query into an Intent. `apps` is a {normalised label → code}
    map of the live catalogue (names AND codes); pass {} in pure tests that don't need
    application resolution. Never raises — an unresolved query returns a low-confidence Intent."""
    msg = (query or "").strip()
    apps = apps or {}
    it = Intent(raw=msg)

    low = msg.lower()
    # action (first cue wins; default stays "list")
    for name, pat in _ACTION_CUES:
        if re.search(pat, low, re.I):
            it.action = name
            break

    # entity (most specific first). "who ..." implies we're listing USERS.
    for name, pat in _ENTITY_CUES:
        if re.search(pat, low, re.I):
            it.entity = name
            break
    if it.action == "who":
        it.entity = "user"
    if it.action == "generate":
        it.entity = "baseline"

    # subject: a named user beats "my"; else self if first-person; else all.
    app_norms = {_norm(k) for k in apps} | {_norm(v) for v in apps.values()}
    user = _named_user(msg)
    # A weak "for/of <token>" capture must not mistake an APPLICATION for a user — e.g. "do I
    # have super admin role FOR FORMULATION" is app scope + self, not user 'formulation'. Dropping
    # it lets the first-person cue win → the caller's own roles, not a list of all app roles.
    if user and _norm(user) in app_norms:
        user = ""
    if user:
        it.subject, it.user = "user", user
    elif _SELF.search(msg):
        it.subject = "self"
    else:
        it.subject = "all"

    # Bare-username fallback: an ALL-CAPS token that isn't a known application code/name or a
    # stopword is very likely a username ("SAUSER access", "what is GCADMIN", "SAUSER access
    # type"). The named-access handler validates it against real users and falls through if it
    # isn't one, so a liberal guess here is safe. Only when nothing more specific claimed it.
    if it.subject == "all":
        for tok in re.findall(r"\b[A-Z][A-Z0-9]{3,}\b", msg):
            if tok.lower() not in _STOP_USER and _norm(tok) not in app_norms:
                it.subject, it.user, user = "user", tok, tok
                break

    # A bare access question about someone ("what can GCADMIN do", "my access") is about
    # their APPLICATIONS when no other entity noun was named.
    if not it.entity and it.subject in ("self", "user") \
            and it.action not in ("assign", "remove", "edit", "generate"):
        it.entity = "application"

    # app + role scope. Strip a trailing "as <role>" clause first, so a role name that
    # happens to contain an app word ("as Budget Facilitator" → "Budget") can't be mistaken
    # for the application — an explicit "in <app>" earlier in the query still matches.
    _app_msg = re.sub(r"\bas (?:an? |the )?[A-Za-z][\w /&.-]*$", "", msg, flags=re.I)
    it.app = _match_app(_app_msg, apps)
    if re.search(r"\b(all|every|catalog(ue)?|unlicensed|disabled)\b", low):
        it.filters["show_all"] = True

    # confidence: did we resolve the parts this action/entity needs?
    it.confidence = _confidence(it)
    return it


def _confidence(it: Intent) -> float:
    """A cheap self-assessment of the extraction. Low = escalate to the LLM router (B)."""
    score = 0.0
    if it.entity:
        score += 0.5                       # we know WHAT it's about
    if it.action:
        score += 0.2                       # and a verb (defaults to list)
    # scope that the phrasing clearly asked for is present
    if it.subject == "user" and it.user:
        score += 0.2
    elif it.subject in ("self", "all"):
        score += 0.15
    if it.app:
        score += 0.15
    return min(1.0, round(score, 2))


# ── dispatch: Intent → ecosystem_qa handler name (or a non-read escalation) ───
def route(it: Intent) -> str:
    """Map an Intent to a handler name. Returns "" (UNKNOWN) when nothing fits — the caller
    then falls back to the LLM router (Option B). Mutations route out to the skill/flow path."""
    a, e, s = it.action, it.entity, it.subject

    if a in ("assign", "remove", "edit", "generate", "create"):
        return "skill_or_flow"             # not a read — handled by skills/flows, not here

    if a == "who" or (e == "user" and it.app):
        return "app_users"
    if a == "describe" and e in ("application", ""):
        if s == "user":
            return "named_access"          # "what is <user>" → their role × access-type profile
        return "about_app" if it.app else ""

    if e == "application":
        if s == "self":
            return "my_access"
        if s == "user":
            return "named_access"
        return "list_apps"
    if e == "role":
        if s == "self":
            return "my_access_in_app" if it.app else "my_roles_all"
        if s == "user" and not it.app:
            return "named_access"          # "<user>'s roles" → their role × access-type profile
        return "app_roles" if it.app else "roles_catalog"
    if e == "user":
        return "users_in_app" if it.app else "users_list"
    if e == "fund_group":
        return "fund_groups"
    if e == "organization":
        return "my_offices" if s == "self" else "organizations"
    if e == "division":
        return "divisions"
    if e in ("sub_org", "program_office"):
        # tree-relative levels — fetched under a fund-group/app context, not a flat master list
        return "org_level"
    if e == "fiscal_year":
        return "fiscal_years"

    return ""                              # UNKNOWN → escalate to Option B
