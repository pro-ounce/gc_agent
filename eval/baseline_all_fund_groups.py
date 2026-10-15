"""
eval/baseline_all_fund_groups.py
────────────────────────────────
Regression test for baseline-list SCOPING: `list_baselines` must span EVERY fund group (via
getAllFundGroups_get), including disabled ones, and must NOT restrict by fiscal year.

Guards against the real regression we hit: the list was scoped to the caller's fund groups
(getFundGroupsByUser_post → Appropriated only), so it showed 4 of 11 baselines. The fix enumerates
all fund groups; this test pins that behaviour with a stubbed tool_registry (no live MCP).

Run: PYTHONPATH=. python eval/baseline_all_fund_groups.py
"""
import asyncio
import os

os.environ.setdefault("STORE_BACKEND", "memory")
os.environ.setdefault("APP_SKIP_INIT", "1")


class _FakeRes:
    def __init__(self, data):
        self.success = True
        self.output = {"data": data}


def run() -> list[tuple[str, bool, str]]:
    from app.services import flows

    r: list[tuple[str, bool, str]] = []

    def check(name, cond, detail=""):
        r.append((name, bool(cond), str(detail)))

    calls: list[tuple[str, dict]] = []

    # Four fund groups incl a DISABLED one (enabled='N'); the caller only "has" Appropriated.
    FUND_GROUPS = [
        {"fundGroupId": 1, "name": "Appropriated", "enabled": "Y"},
        {"fundGroupId": 2, "name": "Debt", "enabled": "Y"},            # exists, no baselines
        {"fundGroupId": 3, "name": "TFF - CFM", "enabled": "Y"},
        {"fundGroupId": 4, "name": "Retired FG", "enabled": "N"},      # disabled → must still show
    ]
    BASELINES = {
        1: [{"masterId": 101, "baselineTitle": "Appr A", "fiscalYear": 2027, "statusName": "Draft"}],
        2: [],
        3: [{"masterId": 301, "baselineTitle": "CFM A", "fiscalYear": 2027,
             "statusName": "Published to Program Office(s)"}],
        4: [{"masterId": 401, "baselineTitle": "Retired A", "fiscalYear": 2060, "statusName": "Draft"}],
    }

    async def fake_execute(tool, args, headers=None):
        calls.append((tool, dict(args or {})))
        if tool == "getAllFundGroups_get":
            return _FakeRes(FUND_GROUPS)
        if tool == "getFundGroupsByUser_post":
            return _FakeRes([{"fundGroupId": 1, "name": "Appropriated"}])  # caller-scoped fallback
        if tool == "getBaseLineMasterHeaders_1_post":
            return _FakeRes(BASELINES.get(args.get("fundGroupId"), []))
        return _FakeRes([])

    orig = flows.tool_registry.execute
    flows.tool_registry.execute = fake_execute  # type: ignore[assignment]
    try:
        fr = asyncio.run(flows.list_baselines(None, {}))
    finally:
        flows.tool_registry.execute = orig  # type: ignore[assignment]

    tool_names = [c[0] for c in calls]
    mh = [c for c in calls if c[0] == "getBaseLineMasterHeaders_1_post"]
    fg_queried = sorted({c[1].get("fundGroupId") for c in mh})

    check("uses getAllFundGroups_get (all groups, not caller-scoped)",
          "getAllFundGroups_get" in tool_names)
    check("does NOT fall back to getFundGroupsByUser_post when global list present",
          "getFundGroupsByUser_post" not in tool_names)
    check("queries EVERY fund group incl the disabled one (1,2,3,4)",
          fg_queried == [1, 2, 3, 4], fg_queried)
    check("passes NO fiscalYear — all years", all("fiscalYear" not in c[1] for c in mh))

    block = fr.blocks[0] if fr.blocks else None
    cols = (block.columns if block else []) or []
    rows = (block.rows if block else []) or []
    fg_col = cols.index("Fund Group") if "Fund Group" in cols else -1
    fgs_in_table = {row[fg_col] for row in rows} if fg_col >= 0 else set()

    check("result is a table block", block is not None and block.type == "table")
    check("Fund Group column present (spans >1 group)", "Fund Group" in cols)
    check("includes Appropriated + TFF-CFM + the DISABLED Retired FG",
          {"Appropriated", "TFF - CFM", "Retired FG"} <= fgs_in_table, sorted(fgs_in_table))
    check("exactly the 3 baselines across groups (Debt empty excluded)",
          len(rows) == 3, len(rows))

    return r


def main() -> int:
    results = run()
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"   [{detail}]" if (detail and not ok) else ""))
    print(f"\nsummary: {passed}/{len(results)} baseline-all-fund-groups checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
