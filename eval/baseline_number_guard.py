"""
eval/baseline_number_guard.py
─────────────────────────────
Regression test for the HARD GUARD against hallucinated baseline numbers
(`chat_service._guard_baseline_numbers`).

Baseline figures (amounts, request/office/line-item counts) are only trustworthy when they come
from the deterministic baseline flows (list_baselines / baseline_details → FlowResult blocks,
fetched live). The LLM prose exit path is reached ONLY when no flow handled the turn, so any
baseline numbers there are invented. The guard must REPLACE such an answer with a safe deflection,
and must NOT touch grounded answers, plain baseline counts, or non-baseline numbers.

Run: PYTHONPATH=. python eval/baseline_number_guard.py
"""
import os

os.environ.setdefault("STORE_BACKEND", "memory")
os.environ.setdefault("APP_SKIP_INIT", "1")


def run() -> list[tuple[str, bool, str]]:
    from app.services.chat_service import _guard_baseline_numbers, _BASELINE_GUARD_MSG

    r: list[tuple[str, bool, str]] = []

    def check(name, cond, detail=""):
        r.append((name, bool(cond), str(detail)))

    # ── MUST GUARD: ungrounded prose stating baseline figures ──────────────────
    guarded = [
        ("real hallucination (offices + $0)",
         'FY 2027 Appropriated Testing Baseline\nSpans 33 program office(s), but no requests '
         'carried forward — every line is $0.'),
        ("fabricated per-office breakdown",
         'The baseline has CDO with 6 requests totaling $92,100.03 and OC with 4 requests.'),
        ("fabricated grand total",
         'The combined funding across the FY2027 baselines is about $1,250,000.'),
        ("request count only",
         'That baseline has 128 requests carried forward from the source year.'),
        ("hyphenated line-items count",
         'The baseline currently holds 42 line-items across its program offices.'),
    ]
    for name, text in guarded:
        out = _guard_baseline_numbers(text, grounded=False)
        check(f"GUARD · {name}", out == _BASELINE_GUARD_MSG, repr(out)[:60])

    # ── MUST NOT GUARD: grounded, or no fabricated figures ─────────────────────
    passthrough = [
        ("grounded answer (tool succeeded)",
         'The baseline has 10 requests totaling $162,388.13.', True),
        ("plain count of baselines (not figures)",
         'You have 4 baselines in Appropriated.', False),
        ("non-baseline numbers",
         'You have 10 users across 3 applications, 2 of them active.', False),
        ("baseline, no numeric figures",
         'That baseline is still in Draft and has not been published yet.', False),
        ("baseline fiscal year only (no $/count)",
         'The FY2027 baseline is published to program offices.', False),
        ("empty text", "", False),
    ]
    for name, text, grounded in passthrough:
        out = _guard_baseline_numbers(text, grounded=grounded)
        check(f"PASS  · {name}", out is None, repr(out)[:60])

    # ── Guard message is a safe DEFLECTION, carries no fabricated figures ───────
    check("guard message contains no '$' amount", "$" not in _BASELINE_GUARD_MSG)
    check("guard message routes to the real fetch",
          "line-item details" in _BASELINE_GUARD_MSG and "list all baselines" in _BASELINE_GUARD_MSG)

    return r


def main() -> int:
    results = run()
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"   [{detail}]" if (detail and not ok) else ""))
    print(f"\nsummary: {passed}/{len(results)} baseline-number-guard checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
