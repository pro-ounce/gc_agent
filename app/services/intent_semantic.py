"""
services/intent_semantic.py  ── ⚠️ SHELVED (not wired in), kept for a future revisit
────────────────────────────────────────────────────────────────────────────────────
Option B, semantic-first: classify a query's INTENT by nearest-neighbour over labelled exemplar
phrasings, embedded with the same nomic-embed-text model tool-RAG uses.

SHELVED 2026-09-12 after calibration (eval/semantic_eval.py) measured it WORSE than the
deterministic router on the shared corpus: 32/39 with **7 confident-wrong at every τ**, vs the
deterministic router's **39/39, 0 confident-wrong**. Root cause: the route discriminators are
SLOTS and VERBS (which app, which user, "who" vs "my", assign vs list), which are lexical — and
embedding similarity BLURS exactly those. E.g. `roles in FORMULATION` matched a `my_access_in_app`
exemplar at cosine 1.000 because "roles in an app" ≈ "my roles in an app" in meaning; the word
"my" (and the username, and the verb) is what decides the route, and that is not semantic.

Decision: the deterministic router stays PRIMARY (precision-first + eval-gated), UNKNOWNs escalate
straight to the LLM. This module is left in place, unused, so the idea can be revisited later —
but only as a slot-AWARE design (deterministic verb/slot signals must win), and validated against
the 0-confident-wrong gate on a corpus grown from real query logs. Do NOT wire this into the live
pipeline as-is. See docs/ROUTER-REDESIGN.md § Phase 2 outcome.
"""
from __future__ import annotations

import math
from typing import Iterable

from ..commons.config import cfg
from ..commons.logger import get_logger
from .embeddings import embed

log = get_logger(__name__)

# Abstain below this top-neighbour cosine similarity → escalate to the LLM. Calibrated on the
# corpus; overridable via config without a code change.
TAU = float(getattr(cfg, "INTENT_SEMANTIC_TAU", 0.62) or 0.62)
_TOPK = 5

# ── labelled exemplars: route (a route() handler name) → representative phrasings ─────────────
# "other" is a sentinel → maps to "" (escalate). Keep exemplars natural and varied; this is the
# knowledge the classifier runs on, so ADD phrasings here when a query mis-classifies (the
# semantic equivalent of adding a regex — but robust to paraphrase).
EXEMPLARS: dict[str, list[str]] = {
    "list_apps": [
        "list the applications", "show all applications", "what applications are there",
        "which apps exist", "application catalogue", "what's in the platform",
        "show me every application", "how many applications are there",
    ],
    "app_roles": [
        "roles in Formulation", "what roles does Allocation Planner have",
        "list the roles for Cost Model", "roles available in Execution",
        "what roles exist in Budget", "show the roles in Revenue Planner",
    ],
    "roles_catalog": [
        "list all roles", "every role in the system", "all roles across applications",
        "the full role catalogue",
    ],
    "my_access": [
        "my access", "what can I access", "what applications do I have", "apps I have",
        "what do I have access to", "which applications am I in", "show my access",
    ],
    "my_access_in_app": [
        "my roles in Formulation", "what roles do I have in Cost Model",
        "do I have super admin role in Execution", "am I an admin in Budget",
        "my access in Allocation", "do I have the planning approver role for Formulation",
        "which roles do I hold in Revenue Planner",
    ],
    "my_roles_all": [
        "my roles", "what roles do I have", "list my roles", "all my roles",
    ],
    "named_access": [
        "what can GCADMIN do", "access for JSMITH", "GCADMIN's roles",
        "what applications does SAUSER have", "what is GCADMIN",
        "roles assigned to JSMITH", "what roles and access type does GCADMIN have",
        "what can that user do",
    ],
    "app_users": [
        "who can access Formulation", "which users have Cost Model",
        "who has access to Execution", "users in Budget", "who uses Allocation",
        "list the users in Revenue Planner",
    ],
    "users_list": [
        "list all users", "show all users", "how many users are there",
        "all accounts", "every user in the system",
    ],
    "fund_groups": [
        "list fund groups", "what fund groups exist", "show fund groups", "the fund groups",
    ],
    "organizations": [
        "show organizations", "list organizations", "what offices exist", "the organizations",
    ],
    "divisions": [
        "list divisions", "show all divisions", "what divisions are there",
    ],
    "my_offices": [
        "my offices", "where can I work", "which offices am I in",
        "where can I work as Budget Facilitator", "my divisions",
    ],
    "about_app": [
        "tell me about Formulation", "what is Cost Model", "describe Execution",
        "what does Budget do", "give me an overview of Revenue Planner",
    ],
    "org_level": [
        "list sub-orgs", "list program offices", "show program offices", "sub organizations",
    ],
    "fiscal_years": [
        "list fiscal years", "what fiscal years are there", "show the fiscal years",
    ],
    # actions — must LEAVE the read router (→ skills/flows)
    "skill_or_flow": [
        "create user", "add a user", "onboard a new person", "register a new account",
        "assign Super Admin to GCADMIN", "grant access to JSMITH",
        "remove the Budget Approver role from SAUSER", "revoke access for JSMITH",
        "generate a formulation baseline", "make Formulation my default application",
        "set up a data call",
    ],
    # off-topic / chit-chat — should escalate (maps to "")
    "other": [
        "why is the sky purple", "what's the weather today", "tell me a joke",
        "help me plan my day", "how are you", "what time is it",
    ],
}


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class _Index:
    """Lazily-embedded exemplar index. Built once, cached in-process."""

    def __init__(self) -> None:
        self._vecs: list[tuple[str, list[float]]] = []   # (route_label, vector)
        self._ready = False

    async def ensure(self) -> bool:
        if self._ready:
            return bool(self._vecs)
        pairs: list[tuple[str, list[float]]] = []
        for label, phrases in EXEMPLARS.items():
            for p in phrases:
                v = await embed(p)
                if v:
                    pairs.append((label, v))
        self._vecs = pairs
        self._ready = True
        log.bind(func="intent_semantic", exemplars=len(pairs)).info(
            f"semantic intent index built ({len(pairs)} exemplars)")
        return bool(pairs)


_index = _Index()


def _route_of(label: str) -> str:
    return "" if label == "other" else label


async def score(query: str) -> tuple[str, float]:
    """Raw prediction WITHOUT the abstain threshold: (winner_label, top_similarity). winner_label
    is the raw exemplar key (incl. "other"). Used for τ-calibration; `classify` applies TAU."""
    q = (query or "").strip()
    if not q or not await _index.ensure():
        return ("", 0.0)
    qv = await embed(q)
    if not qv:
        return ("", 0.0)
    sims = sorted(((_cos(qv, v), label) for label, v in _index._vecs),
                  key=lambda t: t[0], reverse=True)
    top = sims[:_TOPK]
    votes: dict[str, float] = {}
    for sim, label in top:                        # k-NN weighted vote → a noisy neighbour can't decide
        votes[label] = votes.get(label, 0.0) + sim
    return (max(votes, key=votes.get), top[0][0])


async def classify(query: str) -> tuple[str, float]:
    """Predict (route, confidence). Route "" means abstain → escalate to the LLM (below TAU, or a
    clear off-topic 'other'). Confidence is the top-neighbour cosine similarity."""
    label, top_sim = await score(query)
    if not label or top_sim < TAU:
        return ("", round(top_sim, 3))
    return (_route_of(label), round(top_sim, 3))


async def classify_batch(queries: Iterable[str]) -> list[tuple[str, float]]:
    return [await classify(q) for q in queries]
