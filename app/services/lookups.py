"""
services/lookups.py
───────────────────
Admin-scoped MASTER-DATA resolution. User/role/app management values (account type,
category, status, …) are lookups owned by the ADMINISTRATION application. Users refer to
them by name ("Local", "Service account"); the backend wants the code (L, S). This maps a
name-or-code to the valid lookupValueCode, using the lookup under the administration app.

Deliberately decoupled from tool_registry: the caller passes its `execute` coroutine
(so the JSON-RPC/envelope normalization is shared and there is no import cycle).
"""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from ..commons.config import cfg
from ..commons.logger import get_logger

log = get_logger(__name__)

# lookup_code → (loaded_at, {alias_lower: value_code})
_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_TTL = 600.0  # seconds

ExecFn = Callable[..., Awaitable[Any]]


async def _aliases(lookup_code: str, execute: ExecFn, request_headers: dict | None) -> dict[str, str]:
    """Cached {code|label (lower) → value_code} for a lookup under the admin app."""
    now = time.monotonic()
    hit = _CACHE.get(lookup_code)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    amap: dict[str, str] = {}
    try:
        res = await execute(
            "getLookupValueByLookupAndApplicationId_get",
            {"lookup": lookup_code, "applicationId": cfg.LOOKUP_ADMIN_APP_ID},
            request_headers,
        )
        data = res.output.get("data") if getattr(res, "success", False) and isinstance(res.output, dict) else None
        for v in (data or []):
            if not isinstance(v, dict):
                continue
            code = v.get("lookupValueCode")
            if not code:
                continue
            amap[str(code).strip().lower()] = code
            label = v.get("lookupValue")
            if label:
                amap[str(label).strip().lower()] = code
    except Exception as exc:  # noqa: BLE001 — resolution is best-effort
        log.bind(func="lookups", lookup=lookup_code).warning(f"lookup load failed: {exc}")
    _CACHE[lookup_code] = (now, amap)
    return amap


async def resolve(lookup_code: str, value: Any, execute: ExecFn, request_headers: dict | None) -> str | None:
    """Return the valid lookupValueCode for `value` (a code or a name/label), else None."""
    if value in (None, ""):
        return None
    amap = await _aliases(lookup_code, execute, request_headers)
    key = str(value).strip().lower()
    if key in amap:                       # exact code or exact label
        return amap[key]
    for alias, code in amap.items():      # loose: user's word inside the label ("local" → "local account")
        if key and key in alias:
            return code
    return None
