"""
services/skill_store.py
───────────────────────
Persistence + live registration for USER-CREATED skills (authored at runtime via the
`create_skill` guided flow). A custom skill is the simple slice of the Skill dataclass —
keywords + backing MCP tool + required fields + hint + summary — enough to trigger and
ground a single-tool action. (Chains, lookups, validators stay code-authored.)

Stored in BOTH places so either can be the source of truth:
  • OpenSearch KV (via the redis-compatible store) — survives restarts and git deploys.
  • A flat JSON file (gitignored) — human-inspectable and hand-editable.
On load the file OVERRIDES the KV for a given name, so a hand-edit to the file wins.
"""
from __future__ import annotations

import json
import os
from typing import Any

from ..commons.logger import get_logger
from ..connections import redis_client, redis_get_json, redis_set_json, redis_delete
from . import skills as _skills
from .skills import Skill

log = get_logger(__name__)

_KV_PREFIX = "gcskill:"
# Flat file: relative to the app's working dir (/apps/gc_agent under supervisor). Gitignored
# so the pull-deploy hard-reset never clobbers it. Override with CUSTOM_SKILLS_FILE.
_FILE = os.environ.get("CUSTOM_SKILLS_FILE", "custom_skills.json")


def _to_dict(s: Skill) -> dict[str, Any]:
    return {
        "name": s.name, "keywords": list(s.keywords), "tool": s.tool,
        "required": list(s.required), "defaults": dict(s.defaults),
        "hint": s.hint, "summary": s.summary, "custom": True,
    }


def _from_dict(d: dict[str, Any]) -> Skill:
    return Skill(
        name=str(d["name"]).strip(),
        keywords=tuple(str(k).strip().lower() for k in d.get("keywords", []) if str(k).strip()),
        tool=str(d["tool"]).strip(),
        required=tuple(str(r).strip() for r in d.get("required", []) if str(r).strip()),
        defaults=dict(d.get("defaults", {}) or {}),
        hint=str(d.get("hint", "") or ""),
        summary=str(d.get("summary", "") or ""),
    )


def register(skill: Skill) -> None:
    """Add or replace a skill in the live SKILLS list (dedupe by name)."""
    _skills.SKILLS[:] = [s for s in _skills.SKILLS if s.name != skill.name]
    _skills.SKILLS.append(skill)


def _read_file() -> list[dict[str, Any]]:
    try:
        if os.path.exists(_FILE):
            with open(_FILE, encoding="utf-8") as f:
                data = json.load(f)
                return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
    except Exception as exc:  # noqa: BLE001 — a bad file must never break startup
        log.warning(f"custom skills file read failed: {exc}")
    return []


def _write_file(items: list[dict[str, Any]]) -> None:
    try:
        with open(_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"custom skills file write failed: {exc}")


def load_custom_skills() -> int:
    """Load persisted custom skills (KV first, then file overrides) and register them live."""
    merged: dict[str, dict[str, Any]] = {}
    try:
        for raw in (redis_client.keys(f"{_KV_PREFIX}*") or []):
            key = raw.decode() if isinstance(raw, bytes) else raw
            d = redis_get_json(key)
            if isinstance(d, dict) and d.get("name") and d.get("tool"):
                merged[d["name"]] = d
    except Exception as exc:  # noqa: BLE001
        log.warning(f"custom skills KV load failed: {exc}")
    for d in _read_file():                       # file wins over KV (hand-edits authoritative)
        if d.get("name") and d.get("tool"):
            merged[d["name"]] = d
    n = 0
    for d in merged.values():
        try:
            register(_from_dict(d))
            n += 1
        except Exception as exc:  # noqa: BLE001
            log.warning(f"skip malformed custom skill: {exc}")
    if n:
        log.bind(func="load_custom_skills", count=n).info(f"loaded {n} custom skill(s)")
    return n


def save_custom_skill(d: dict[str, Any]) -> Skill:
    """Persist a custom skill to KV + file and register it live. Returns the Skill."""
    skill = _from_dict(d)
    dd = _to_dict(skill)
    try:
        redis_set_json(f"{_KV_PREFIX}{skill.name}", dd)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"custom skill KV save failed: {exc}")
    items = [x for x in _read_file() if x.get("name") != skill.name]
    items.append(dd)
    _write_file(items)
    register(skill)
    log.bind(func="save_custom_skill", skill=skill.name, tool=skill.tool).info("custom skill saved")
    return skill


import re as _re

# Boilerplate appended to every MCP tool description — noise for a human choosing a tool.
_DESC_BOILERPLATE = _re.compile(
    r"\s*(use this tool only|only use this tool|use this only)\b.*$", _re.I | _re.S)


def _clean_desc(desc: str) -> str:
    """Strip the standard grounding boilerplate and tidy punctuation so the real, full
    description shows — a functional consultant shouldn't have to guess from the tool name."""
    d = (desc or "").strip()
    d = _DESC_BOILERPLATE.sub("", d)
    d = _re.sub(r"\.{2,}", ".", d).strip()          # "type.." → "type."
    return d.rstrip(".").strip()


def _params_of(tool: Any, injected: frozenset[str]) -> list[str]:
    sch = getattr(tool, "input_schema", None) or {}
    if isinstance(sch, dict) and isinstance(sch.get("required"), list):
        return [str(r) for r in sch["required"] if str(r).lower() not in injected]
    return [p.name for p in getattr(tool, "parameters", [])
            if getattr(p, "required", False) and p.name.lower() not in injected]


async def suggest_tools(query: str, k: int = 6) -> list[dict[str, Any]]:
    """Candidate MCP tools for a natural-language purpose (tool-RAG) with FULL cleaned
    descriptions + the fields each needs, so the create-skill picker is self-explanatory."""
    from ..mcp.tool_registry import tool_registry, _INJECTED_PARAMS
    from ..mcp.tool_index import tool_index
    names = await tool_index.search(query, k) or []
    by = {t.name: t for t in await tool_registry.get_tools()}
    out: list[dict[str, Any]] = []
    for n in names:
        t = by.get(n)
        if t:
            out.append({"name": n, "desc": _clean_desc(getattr(t, "description", "") or ""),
                        "required": _params_of(t, _INJECTED_PARAMS)})
    return out


async def tool_detail(tool_name: str) -> dict[str, Any] | None:
    """Full detail for ONE tool (cleaned description + required + all params), so the user can
    ask for more before choosing."""
    from ..mcp.tool_registry import tool_registry, _INJECTED_PARAMS
    t = next((x for x in await tool_registry.get_tools() if x.name == tool_name), None)
    if not t:
        return None
    allp = [p.name for p in getattr(t, "parameters", []) if p.name.lower() not in _INJECTED_PARAMS]
    return {"name": tool_name, "desc": _clean_desc(getattr(t, "description", "") or ""),
            "required": _params_of(t, _INJECTED_PARAMS), "params": allp}


async def tool_required_fields(tool_name: str) -> list[str]:
    """The tool's required parameters (minus auth-injected ones) → the new skill's `required`."""
    from ..mcp.tool_registry import tool_registry, _INJECTED_PARAMS
    t = next((x for x in await tool_registry.get_tools() if x.name == tool_name), None)
    if not t:
        return []
    sch = getattr(t, "input_schema", None) or {}
    if isinstance(sch, dict) and isinstance(sch.get("required"), list):
        return [str(r) for r in sch["required"] if str(r).lower() not in _INJECTED_PARAMS]
    return [p.name for p in getattr(t, "parameters", [])
            if getattr(p, "required", False) and p.name.lower() not in _INJECTED_PARAMS]


async def find_tool(name_or_query: str) -> str | None:
    """Resolve a typed tool name to the exact catalog name (exact, else case-insensitive)."""
    from ..mcp.tool_registry import tool_registry
    q = str(name_or_query).strip()
    names = [t.name for t in await tool_registry.get_tools()]
    if q in names:
        return q
    return next((n for n in names if n.lower() == q.lower()), None)


def delete_custom_skill(name: str) -> bool:
    """Remove a custom skill from KV, file, and the live list."""
    try:
        redis_delete(f"{_KV_PREFIX}{name}")
    except Exception:  # noqa: BLE001
        pass
    _write_file([x for x in _read_file() if x.get("name") != name])
    before = len(_skills.SKILLS)
    _skills.SKILLS[:] = [s for s in _skills.SKILLS if s.name != name]
    return len(_skills.SKILLS) < before
