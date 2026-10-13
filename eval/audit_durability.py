"""
eval/audit_durability.py
────────────────────────
Durability check for the AUDIT sink: the local file is ROTATED (never trim-dropped), and every
metric-relevant record is queued for the durable OpenSearch audit index. Runs against the
in-memory store backend (so the OpenSearch mirror is safely skipped, not errored).
"""
import os
import tempfile

os.environ.setdefault("STORE_BACKEND", "memory")
os.environ.setdefault("APP_SKIP_INIT", "1")
os.environ["METRICS_LOG_FILE"] = os.path.join(tempfile.mkdtemp(), "metrics_log.jsonl")


def run() -> list[tuple[str, bool, str]]:
    import glob
    from app.commons import logger as L
    from app.connections import os_client

    r: list[tuple[str, bool, str]] = []

    def check(name, cond, detail=""):
        r.append((name, bool(cond), str(detail)))

    L._METRICS_MAXLINES = 10         # tiny threshold so rotation triggers in the test
    L._AUDIT_Q.clear()
    if os.path.exists(L._METRICS_FILE):
        os.remove(L._METRICS_FILE)

    for i in range(30):
        L._persist_record({"ts": f"2026-01-01 00:00:{i:02d}", "level": "INFO",
                           "fields": {"request_id": "req", "n": i}})

    with open(L._METRICS_FILE, encoding="utf-8") as f:
        live = f.readlines()
    check("all audit records persisted to the local file", len(live) == 30, len(live))
    check("every record queued for the durable OpenSearch sink", len(L._AUDIT_Q) == 30, len(L._AUDIT_Q))

    L._trim_metrics_file()           # live is 30 > 2*10 → ROTATE (not drop)
    rots = glob.glob(f"{L._METRICS_FILE}.*")
    check("rotation rolled the file to an archive (never dropped)", len(rots) == 1, rots)

    total = 0
    for p in [L._METRICS_FILE, *rots]:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                total += sum(1 for _ in f)
    check("no audit records lost across rotation (all 30 preserved)", total == 30, total)

    check("memory backend → OpenSearch mirror safely skipped (no raw client)", os_client() is None)

    L._LOG_RING.clear()
    n = L.load_persisted_metrics(limit=100)   # file gone after rotation → OS fallback (None → 0)
    check("rehydrate runs cleanly (no crash) after rotation", isinstance(n, int), n)

    # Prune cap: with many rotations, only the last _METRICS_ROTATIONS are kept locally (OS has all).
    L._METRICS_ROTATIONS = 3
    for _ in range(6):
        with open(L._METRICS_FILE, "w", encoding="utf-8") as f:
            f.writelines(["x\n"] * 30)
        L._trim_metrics_file()
    kept = glob.glob(f"{L._METRICS_FILE}.*")
    check("local rotations are capped (bounded cache; OS keeps everything)", len(kept) <= 3, len(kept))

    return r


def main() -> int:
    results = run()
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"   [{detail}]" if (detail and not ok) else ""))
    print(f"\nsummary: {passed}/{len(results)} audit-durability checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
