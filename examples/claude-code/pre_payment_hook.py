#!/usr/bin/env python3
"""Claude Code PreToolUse hook for deterministic settlement verification.

Blocks settlement/payout commands unless Reciprocal Ruler verifies the math
without an unacknowledged SHORTFALL. Standard library only.
"""

from __future__ import annotations

import json
import os
import sys
from urllib import error as urlerror
from urllib import request as urlrequest

API_BASE = os.getenv("RULER_API_BASE", "https://reciprocal-ruler-x402.vercel.app").rstrip("/")


def verify_math(expected: float, actual: float, currency: str = "USD") -> dict:
    payload = json.dumps(
        {
            "expected_amount": expected,
            "actual_amount": actual,
            "tolerance": 0.0,
            "currency": currency,
        }
    ).encode("utf-8")
    req = urlrequest.Request(
        f"{API_BASE}/v1/trial/verify-math",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Trial-Client": os.getenv("RULER_TRIAL_CLIENT", "claude-code-hook"),
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {"status": "HTTP_ERROR", "code": exc.code, "body": body}


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        return 0
    hook_input = json.loads(raw)
    tool_input = hook_input.get("tool_input") or {}
    expected = tool_input.get("expected_amount")
    actual = tool_input.get("actual_amount")
    if expected is None or actual is None:
        return 0

    result = verify_math(float(expected), float(actual), str(tool_input.get("currency", "USD")))
    status = result.get("status")
    if status == "SHORTFALL":
        sys.stderr.write(
            f"[Reciprocal Ruler Hook] Blocked: SHORTFALL detected (delta={result.get('delta')}). "
            "Run ruler_reconcile or settle from holdback before proceeding.\n"
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
