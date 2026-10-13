"""
eval/history_archive.py
───────────────────────
Durability check for the HISTORY archive: the live session carries the 24h TTL, but the
`history:*` sink is written with NO TTL so conversations are retained indefinitely, survive
restarts, and ride the agent-kv snapshot. Runs against the in-memory store backend.
"""
import os

os.environ.setdefault("STORE_BACKEND", "memory")
os.environ.setdefault("APP_SKIP_INIT", "1")


def run() -> list[tuple[str, bool, str]]:
    from app.services.session_service import session_service, _key, _hkey, _HISTORY_PREFIX
    from app.connections import redis_client, redis_get_json

    r: list[tuple[str, bool, str]] = []

    def check(name, cond, detail=""):
        r.append((name, bool(cond), str(detail)))

    # Fresh conversation, saved.
    s = session_service.get_or_create("agent:user1", user_id="user1")
    s.add_user("what are my fund groups?")
    s.add_assistant("You have General Fund and Debt.")
    session_service.save(s)

    check("live session persisted (session:*)", redis_get_json(_key("agent:user1")) is not None)
    check("history archive written on save (history:* with no TTL)",
          redis_get_json(_hkey("agent:user1")) is not None)
    # The live key carries a TTL; the history key does NOT (indefinite retention).
    live_exp = redis_client._store.get(_key("agent:user1"), (None, None))[1]
    hist_exp = redis_client._store.get(_hkey("agent:user1"), (None, None))[1]
    check("live session HAS an expiry (24h TTL)", live_exp is not None, live_exp)
    check("history archive has NO expiry (indefinite retention)", hist_exp is None, hist_exp)

    # "New chat" → the prior conversation is archived immutably, then the live session is cleared.
    session_service.clear_messages("agent:user1")
    live = session_service.get("agent:user1")
    check("live session cleared on new chat", live is not None and len(live.messages) == 0)

    # Start a second conversation in the same (stable) session.
    s2 = session_service.get("agent:user1")
    s2.add_user("list the current baselines")
    s2.add_assistant("Here are your baselines…")
    session_service.save(s2)

    hist = session_service.list_history("user1")
    check("history lists the current + the archived conversation", len(hist) >= 2, len(hist))
    check("an immutable archived record exists", any(h["archived"] for h in hist))
    check("archived record preserved the PRIOR conversation's question",
          any(h.get("last_question") == "what are my fund groups?" for h in hist),
          [h.get("last_question") for h in hist])
    check("current conversation is present too",
          any(h.get("last_question") == "list the current baselines" for h in hist))
    # Archived records are IMMUTABLE — not overwritten by later saves.
    arch_keys = [k for k in redis_client._store
                 if k.startswith(_HISTORY_PREFIX) and ":" in k[len(_HISTORY_PREFIX):]]
    check("archived conversation stored under its own immutable key", len(arch_keys) >= 1, arch_keys)
    check("archived key has NO expiry (kept indefinitely)",
          all(redis_client._store[k][1] is None for k in arch_keys))

    # Admin history view: each item carries a drill-down key + a category; detail returns messages.
    check("history items carry a drill-down key", all("key" in h for h in hist), None)
    check("history items carry a category", all("category" in h for h in hist), None)
    check("categorization: the baselines conversation is 'Baseline'",
          any(h.get("category") == "Baseline" for h in hist), [h.get("category") for h in hist])
    detail = session_service.get_history_detail(hist[0]["key"])
    check("get_history_detail returns the full transcript for a key",
          isinstance(detail, dict) and "messages" in detail)
    check("get_history_detail rejects a non-history key",
          session_service.get_history_detail("session:agent:user1") is None)

    return r


def main() -> int:
    results = run()
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"   [{detail}]" if (detail and not ok) else ""))
    print(f"\nsummary: {passed}/{len(results)} history-archive checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
