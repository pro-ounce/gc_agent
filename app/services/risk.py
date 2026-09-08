"""
services/risk.py
────────────────
Tier-1 rule-based risk scorer over the audit trail — deterministic and EXPLAINABLE (auditors
need reasons, not a black box). Given the recent turns (each carrying actor, client_ip, origin,
user-agent, app, role, question, tools, errors, ts), it scores each turn 0-100 and lists the
signals that fired. Cross-turn signals (velocity, impossible-travel) are computed per user over
the ordered stream.

Signals (weights tunable):
  • off-hours access            — outside business hours / on a weekend
  • high velocity               — a burst of requests from one user in a short window
  • impossible travel           — the user's source IP hops between requests too close in time
                                  (IP-hop proxy; no offline geo DB — labelled as such)
  • privilege escalation        — an access change granting an admin/super role, esp. to self
  • off-domain origin           — Origin/Referer isn't the platform's own domain
  • non-browser client          — curl/python/etc. hitting the agent
  • errors                      — the turn errored (probing / failure signal)

Not an ML model — it's the explainable first line; graduate to statistical baselining / an
unsupervised model once there's logged history.
"""
from __future__ import annotations

import datetime as _dt

# ── tunables ────────────────────────────────────────────────────────────────────
BIZ_START, BIZ_END = 7, 19          # local business hours [07:00, 19:00)
VELOCITY_WINDOW_S = 60
VELOCITY_MAX = 10                   # >10 requests/min from one user → burst
TRAVEL_WINDOW_S = 900               # an IP hop within 15 min is suspicious
EXPECTED_ORIGIN = "proounce"
_PRIV_TOOLS = {"addUserApplicationAndRole_post", "addUserApplicationRole_post",
               "updateUserApplicationRoles_put"}
_ADMIN_WORDS = ("super admin", "super_admin", "superadmin", "administrator", "admin")
_SCRIPT_UAS = ("curl", "python-", "httpx", "wget", "go-http", "java/")

_W = {"off_hours": 15, "velocity": 25, "travel": 30, "priv_self_admin": 40,
      "priv_self": 30, "priv_admin": 25, "priv_change": 10,
      "off_domain": 25, "non_browser": 15, "errors": 5}


def _parse_ts(s: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.strptime(str(s), "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _level(score: int) -> str:
    return "high" if score >= 60 else "medium" if score >= 30 else "low"


def score_turns(turns: list[dict]) -> list[dict]:
    """Annotate each turn in `turns` with risk_score / risk_level / risk_reasons (in place).
    Accepts turns in any order (newest-first from _recent_turns); scores a time-ordered pass
    so per-user velocity + IP-hop state is correct. Returns the same list."""
    ordered = sorted([t for t in turns if t.get("ts")], key=lambda t: t["ts"])
    state: dict[str, dict] = {}
    for t in ordered:
        reasons: list[str] = []
        score = 0
        user = str(t.get("user_name") or t.get("user_id") or "?")
        ts = _parse_ts(t.get("ts"))
        ip = str(t.get("client_ip") or "")
        st = state.setdefault(user, {"times": [], "last_ip": "", "last_ts": None})

        if ts and (ts.hour < BIZ_START or ts.hour >= BIZ_END or ts.weekday() >= 5):
            score += _W["off_hours"]
            reasons.append(f"off-hours access ({ts.strftime('%a %H:%M')})")

        if ts:
            st["times"] = [x for x in st["times"] if (ts - x).total_seconds() <= VELOCITY_WINDOW_S]
            st["times"].append(ts)
            if len(st["times"]) > VELOCITY_MAX:
                score += _W["velocity"]
                reasons.append(f"high velocity ({len(st['times'])} req/min for {user})")

        if (ip and st["last_ip"] and ip != st["last_ip"] and ts and st["last_ts"]
                and (ts - st["last_ts"]).total_seconds() <= TRAVEL_WINDOW_S):
            gap = int((ts - st["last_ts"]).total_seconds())
            score += _W["travel"]
            reasons.append(f"impossible travel: IP {st['last_ip']} → {ip} in {gap}s")
        if ip:
            st["last_ip"] = ip
        if ts:
            st["last_ts"] = ts

        tools = t.get("tools") or []
        q = str(t.get("question") or "").lower()
        is_priv = any(x in _PRIV_TOOLS for x in tools) or ("assign" in q and "role" in q)
        if is_priv:
            admin_role = any(w in q for w in _ADMIN_WORDS)
            self_target = bool(user) and user != "?" and user.lower() in q
            if self_target and admin_role:
                score += _W["priv_self_admin"]
                reasons.append("privilege escalation: self-assigning an admin role")
            elif self_target:
                score += _W["priv_self"]
                reasons.append("self role assignment")
            elif admin_role:
                score += _W["priv_admin"]
                reasons.append("admin-role assignment")
            else:
                score += _W["priv_change"]
                reasons.append("access change")

        origin = str(t.get("origin") or "").lower()
        if origin and EXPECTED_ORIGIN not in origin:
            score += _W["off_domain"]
            reasons.append("off-domain origin")
        ua = str(t.get("user_agent") or "").lower()
        if ua and any(s in ua for s in _SCRIPT_UAS):
            score += _W["non_browser"]
            reasons.append("non-browser client")
        if t.get("errors"):
            score += _W["errors"]
            reasons.append("errors in turn")

        t["risk_score"] = min(100, score)
        t["risk_level"] = _level(t["risk_score"])
        t["risk_reasons"] = reasons
    # turns that had no ts never entered `ordered` — default them to low/no-signal.
    for t in turns:
        t.setdefault("risk_score", 0)
        t.setdefault("risk_level", "low")
        t.setdefault("risk_reasons", [])
    return turns
