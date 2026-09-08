"""
services/intent.py
──────────────────
Lightweight SEMANTIC intent classifier for the harness. Regex/keyword routing is precise but
brittle — it misses paraphrases ("which planners can I get into?" won't match a "my access"
keyword). This embeds a handful of exemplar phrasings per intent once (via the already-loaded
nomic-embed model) and matches an incoming query by cosine similarity, so natural rewordings
route to the right deterministic handler without an LLM generation call.

Used as a FALLBACK after the fast regex paths in ecosystem_qa: cheap (one embed/turn, model is
resident), fail-open (any embedding hiccup → returns None → normal routing / the model).
"""
from __future__ import annotations

import asyncio
import math

from ..commons.logger import get_logger
from .embeddings import embed

log = get_logger(__name__)

# intent → natural exemplar phrasings. App-specific intents (about/roles/who) describe the
# ACT; the application entity is resolved separately (ecosystem_qa._mentioned_app / current app).
_EXEMPLARS: dict[str, list[str]] = {
    "list_apps": [
        "what applications are available", "show me all the apps", "which planners exist",
        "list the applications", "what modules does this platform have",
        "what apps can this system do", "give me the application catalogue",
    ],
    "my_access": [
        "what can I do", "what do I have access to", "my applications and roles",
        "which apps can I get into", "what access do I have", "what am I allowed to use",
        "show me my access",
    ],
    "about_app": [
        "tell me about this application", "what does this app do", "what is this planner for",
        "give me an overview of this application", "explain what this application is",
        "what's this module about",
    ],
    "roles_app": [
        "what roles are in this application", "list the roles for this app",
        "which roles does this application have", "what roles can users have here",
    ],
    "who_access": [
        "who can access this application", "which users use this app",
        "who has access to it", "list the users of this application",
        "how many people are on this app", "who is in this application",
    ],
    "user_access": [
        "what can this user do", "what does this person have access to",
        "show this user's applications and roles", "what access does this account have",
    ],
}

_vecs: dict[str, list[list[float]]] | None = None
_lock = asyncio.Lock()


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


async def _ensure() -> dict[str, list[list[float]]] | None:
    """Embed the exemplars once (cached). Returns None if embeddings are unavailable."""
    global _vecs
    if _vecs is not None:
        return _vecs or None
    async with _lock:
        if _vecs is not None:
            return _vecs or None
        built: dict[str, list[list[float]]] = {}
        for name, phrases in _EXEMPLARS.items():
            vs = []
            for p in phrases:
                v = await embed(p)
                if v:
                    vs.append(v)
            if vs:
                built[name] = vs
        _vecs = built
        if built:
            log.bind(func="intent", intents=len(built)).info("intent exemplars embedded")
        return _vecs or None


async def classify(query: str, threshold: float = 0.66) -> tuple[str | None, float]:
    """Best-matching intent for the query and its cosine score, or (None, score) when nothing
    clears `threshold`. nomic-embed similarities run high, so the bar is deliberately strict."""
    q = (query or "").strip()
    if len(q) < 3:
        return None, 0.0
    vecs = await _ensure()
    if not vecs:
        return None, 0.0
    qv = await embed(q)
    if not qv:
        return None, 0.0
    best, best_score = None, 0.0
    for name, exemplars in vecs.items():
        score = max((_cos(qv, ev) for ev in exemplars), default=0.0)
        if score > best_score:
            best, best_score = name, score
    if best_score >= threshold:
        return best, round(best_score, 3)
    return None, round(best_score, 3)


async def classify_among(query: str, groups: dict[str, list[str]], threshold: float = 0.66,
                         cache: dict[str, list[list[float]]] | None = None) -> tuple[str | None, float]:
    """Generic semantic match: pick the label whose exemplar phrases are closest to `query`.
    `groups` is {label: [exemplar phrases]}. Pass a `cache` dict (owned by the caller) to reuse
    exemplar vectors across calls; the caller clears it when its label set changes (e.g. a new
    skill was registered). Used by skill trigger matching."""
    q = (query or "").strip()
    if len(q) < 3 or not groups:
        return None, 0.0
    store = cache if cache is not None else {}
    for name, phrases in groups.items():
        if name not in store:
            vs: list[list[float]] = []
            for p in phrases:
                v = await embed(p)
                if v:
                    vs.append(v)
            store[name] = vs
    qv = await embed(q)
    if not qv:
        return None, 0.0
    best, best_score = None, 0.0
    for name, vs in store.items():
        score = max((_cos(qv, ev) for ev in vs), default=0.0)
        if score > best_score:
            best, best_score = name, score
    if best_score >= threshold:
        return best, round(best_score, 3)
    return None, round(best_score, 3)
