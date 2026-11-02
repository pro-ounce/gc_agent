"""
eval/flow_navigation_eval.py
────────────────────────────
Gate for mid-intake NAVIGATION and the definitional-baseline VETO — the three fixes for the
2026-11 agent-history deviations:

  • P0 create-user dead-end: a guided step (esp. email validation) must NEVER trap the user.
    "start from first" restarts, "what do you have so far?" recaps + re-asks, "go back" steps
    back, and an invalid answer re-prompts WITH visible escape chips — never a bare dead-end.
  • P0 skill-authoring leak: at the tool-pick step, control words ("next"/"yes") and unknown
    input must not surface raw MCP tool ids / "type an exact tool name".
  • P1 baseline definitional misroute: conceptual "what/define/explain … baseline" questions are
    answered (fall through), NOT routed to the baseline list read.

    APP_SKIP_INIT=1 PYTHONPATH=/apps/gc_agent ./bin/python eval/flow_navigation_eval.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("APP_SKIP_INIT", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import flows  # noqa: E402
from app.mcp.tool_registry import tool_registry  # noqa: E402


class _Res:
    def __init__(self, data):
        self.success = True
        self.output = {"data": data}


async def _fake(t, a=None, h=None):
    # No user with any name exists → username step always passes.
    return _Res([])


class _S:
    def __init__(self):
        self.metadata: dict = {}
        self.pending_action_id = None


def _labels(fr) -> list[str]:
    return [c.get("label", "") for c in (getattr(fr, "suggestions", None) or [])]


async def _advance(s, msg):
    """Drive a turn exactly as chat_service does: active flow → handle, else maybe_start."""
    if flows.is_active(s):
        return await flows.handle(s, msg, {})
    return await flows.maybe_start(s, msg, {})


async def run() -> int:
    tool_registry.execute = _fake  # type: ignore[assignment]
    results: list[tuple[bool, str]] = []

    def check(cond: bool, label: str) -> None:
        results.append((bool(cond), label))

    # ── Walk create-user to the email step, the historical trap ──────────────────
    async def _at_email() -> _S:
        s = _S()
        flows.start_create(s)
        await _advance(s, "one at a time")      # mode → first
        await _advance(s, "Jane")               # first → last
        await _advance(s, "Doe")                # last → username
        await _advance(s, "JDOE")               # username → email
        assert s.metadata["flow"]["stage"] == "email", s.metadata["flow"]["stage"]
        return s

    # 1. invalid email does NOT dead-end — it re-prompts WITH escape chips, flow still at email.
    s = await _at_email()
    fr = await _advance(s, "abc")
    check(s.metadata.get("flow", {}).get("stage") == "email"
          and any("back" in l.lower() or "start over" in l.lower() for l in _labels(fr))
          and "valid email" not in (fr.message or "").lower(),
          "invalid email re-prompts with escape chips (no dead-end)")

    # 2. "start from first" RESTARTS the intake (back to the mode step, data cleared).
    s = await _at_email()
    fr = await _advance(s, "lets start from first")
    check(s.metadata.get("flow", {}).get("stage") == "mode"
          and not s.metadata.get("flow", {}).get("data"),
          "'lets start from first' restarts the intake")

    # 3. "what are the details do you have upto now" RECAPS and keeps the flow (does not break out).
    s = await _at_email()
    fr = await _advance(s, "what are the details do you have upto now")
    check(flows.is_active(s) and s.metadata["flow"]["stage"] == "email"
          and "JDOE" in (fr.message or "") and "Jane" in (fr.message or ""),
          "progress question recaps collected data and keeps the flow")

    # 4. "go back" steps to the previous question (email → username) and clears it for re-entry.
    s = await _at_email()
    fr = await _advance(s, "go back")
    check(s.metadata["flow"]["stage"] == "username"
          and "userName" not in s.metadata["flow"]["data"],
          "'go back' returns to the previous step")

    # 5. a REAL email still advances (nav never eats a valid answer).
    s = await _at_email()
    fr = await _advance(s, "jane@agency.gov")
    check(s.metadata["flow"]["stage"] == "type"
          and s.metadata["flow"]["data"].get("emailAddress") == "jane@agency.gov",
          "a valid email still advances past the step")

    # 6. a plausible NAME is never mistaken for a nav command.
    s = _S()
    flows.start_create(s)
    await _advance(s, "one at a time")
    await _advance(s, "Start")                  # first name literally "Start"
    check(s.metadata["flow"]["stage"] == "last"
          and s.metadata["flow"]["data"].get("firstName") == "Start",
          "a first name 'Start' is stored, not treated as restart")

    # ── Skill-authoring tool-pick step: no raw-internal leak on control words ─────
    s = _S()
    s.metadata["flow"] = {"name": "create_skill", "stage": "tool",
                          "data": {"summary": "x", "keywords": ["x"],
                                   "_sugg": ["getAllAuditor_get", "getNextDisplayOrde"]}}
    fr = await flows.handle(s, "NEXT", {})
    msg = (fr.message or "").lower()
    check("couldn't find a tool" not in msg and "exact tool name" not in msg
          and "next" not in msg,
          "'NEXT' at tool step does not leak raw tool-picker internals")

    # ── Baseline definitional veto: conceptual questions are NOT routed to the list ──
    async def _is_list(q: str) -> bool:
        s2 = _S()
        fr2 = await flows.maybe_start(s2, q, {})
        return fr2 is not None and "baseline" in (fr2.message or "").lower() \
            and any(w in (fr2.message or "").lower() for w in ("your ", "formulation baseline"))

    for q in ("tell me what a baseline is", "what does a baseline mean",
              "what is a budget baseline, in two sentences?", "how does a baseline work"):
        listed = await _is_list(q)
        check(not listed, f"definitional {q!r} is NOT routed to the list")

    # real list requests still list
    for q in ("what baselines do I have", "list my baselines"):
        s3 = _S()
        fr3 = await flows.maybe_start(s3, q, {})
        check(fr3 is not None, f"real list request {q!r} still routes to a read")

    ok = sum(1 for p, _ in results if p)
    for passed, label in results:
        print(f"  {'OK  ' if passed else 'FAIL'} {label}")
    print(f"\nsummary: {ok}/{len(results)} navigation + veto checks passed")
    if ok == len(results):
        print("  ✓ intake navigation + baseline veto hold")
        return 0
    print("  ✗ navigation/veto regression")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
