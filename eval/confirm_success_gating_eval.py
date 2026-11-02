"""
eval/confirm_success_gating_eval.py
───────────────────────────────────
Gate for the FALSE-SUCCESS fix (chat_service.confirm_stream / confirm). A confirmed WRITE can come
back HTTP-200 with a business-error ENVELOPE ({success:false}); the agent must NOT read transport
success as the write landing and then claim "created / assigned / ready".

The confirm paths now gate the completion flow on an envelope-aware `accepted` flag:
    accepted = result.success and not _envelope_error(_coerce(result.output))
This eval locks the `accepted` signal: a business-error envelope → NOT accepted (so start_onboarding /
after_assign never run), a transport failure → NOT accepted, a real success envelope → accepted.

    APP_SKIP_INIT=1 PYTHONPATH=/apps/gc_agent ./bin/python eval/confirm_success_gating_eval.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("APP_SKIP_INIT", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.ui_blocks import _coerce, _envelope_error  # noqa: E402


class _Res:
    def __init__(self, success, output=None, error=None):
        self.success = success
        self.output = output
        self.error = error


def _accepted(res: "_Res") -> bool:
    """Mirror the exact gate used in chat_service confirm paths."""
    env = _coerce(res.output) if res.success else None
    return res.success and not _envelope_error(env)


# Real envelopes seen in the agent history (create user / assign application).
CREATE_USER_BAD_SQL = {"success": "false",
                       "message": "CallableStatementCallback; bad SQL grammar [{call USER_MAINTENANCE_PKG.USER_VALIDATIONS(...)}]"}
ASSIGN_400 = _Res(False, output=None, error='400 on POST ".../api/admin/user/applicationAndRole"')
CREATE_OK = {"success": "true", "statusCode": "200", "message": "Successfully created",
             "data": {"id": "9001", "userName": "JDOE"}}
ASSIGN_OK = {"success": "true", "statusCode": "200", "message": "Assigned", "data": {}}
ENVELOPE_404 = {"success": "true", "statusCode": "404", "message": "User not found"}


def run():
    r = []

    def check(n, c):
        r.append((n, bool(c)))

    # Business-error envelope on HTTP 200 → NOT accepted (the core false-success case).
    check("createUser {success:false, bad SQL} is NOT accepted",
          not _accepted(_Res(True, output=CREATE_USER_BAD_SQL)))
    # Transport failure (HTTP 400) → NOT accepted.
    check("assign HTTP 400 is NOT accepted", not _accepted(ASSIGN_400))
    # statusCode >= 400 inside a 'success:true' envelope → NOT accepted.
    check("envelope statusCode 404 is NOT accepted",
          not _accepted(_Res(True, output=ENVELOPE_404)))
    # Genuine success → accepted (so the flow DOES continue; no regression).
    check("real createUser success IS accepted", _accepted(_Res(True, output=CREATE_OK)))
    check("real assign success IS accepted", _accepted(_Res(True, output=ASSIGN_OK)))

    return r


def main() -> int:
    results = run()
    passed = sum(1 for _, ok in results if ok)
    for n, ok in results:
        print(f"  {'OK  ' if ok else 'FAIL'} {n}")
    print(f"\nsummary: {passed}/{len(results)} confirm-gating checks passed")
    print("  ✓ false-success gate holds" if passed == len(results) else "  ✗ false-success regression")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
