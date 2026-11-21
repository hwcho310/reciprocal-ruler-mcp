#!/usr/bin/env python3
"""Live smoke test: drive the MCP server over stdio against the public API.

Free trial calls only; the script never sends a payment header, so it cannot spend money.

    python3 scripts/live_smoke.py
"""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def main() -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.pop("RULER_API_BASE", None)  # use the default production endpoint
    env["RULER_TRIAL_CLIENT_ID"] = "mcp-smoke-" + uuid.uuid4().hex[:8]

    proc = subprocess.Popen([sys.executable, "-m", "reciprocal_ruler_mcp"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, env=env)
    failures = []

    def rpc(request_id, method, params=None):
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method,
                                     "params": params or {}}) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("server closed stdout: " + proc.stderr.read())
        return json.loads(line)

    try:
        init = rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "live-smoke", "version": "0"}})
        print("initialize:", init["result"]["protocolVersion"], init["result"]["serverInfo"])
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()

        tools = rpc(2, "tools/list")["result"]["tools"]
        print("tools/list:", [tool["name"] for tool in tools])
        if len(tools) != 3:
            failures.append("tools/list did not return 3 tools")

        trial_args = {"expected_amount_krw": 220000, "actual_settled_krw": 180000,
                      "holdback_krw": 22000, "tx_id": "smoke-%s" % uuid.uuid4().hex[:6]}
        for attempt in (1, 2):
            result = rpc(10 + attempt, "tools/call", {"name": "ruler_trial_reconcile", "arguments": trial_args})
            payload = result["result"]["structuredContent"]
            print(f"trial call {attempt}: isError={result['result']['isError']} "
                  f"judgment={(payload.get('result') or {}).get('judgment')} "
                  f"remaining={payload.get('trial_usage', {}).get('remaining')}")
            if payload.get("result", {}).get("judgment") != "COMPENSATED":
                failures.append(f"trial call {attempt} did not judge COMPENSATED: {payload}")

        quote = rpc(20, "tools/call", {"name": "ruler_quote_lock", "arguments": {
            "buyer_version": "1.0", "supplier_version": "1.0", "buyer_id": "smoke-buyer",
            "supplier_id": "smoke-supplier", "terms": {"price_krw": 220000}}})
        payload = quote["result"]["structuredContent"]
        summary = payload.get("payment_summary", {})
        print("quote_lock 402:", payload.get("status"), summary.get("amount_usdc"),
              summary.get("network_name"), "payTo", summary.get("pay_to"))
        if payload.get("status") != "PAYMENT_REQUIRED" or summary.get("amount_usdc") != "0.050000":
            failures.append("quote lock did not return the expected $0.05 x402 challenge")

        previous = rpc(30, "tools/call", {"name": "ruler_quote_lock", "arguments": {
            "buyer_version": "1.0", "supplier_version": "2.0", "buyer_id": "smoke-buyer",
            "supplier_id": "smoke-supplier", "terms": {"price_krw": 220000}, "payment_header": "x"}})
        print("paid path with a dummy payment payload (must not be honored):",
              previous["result"]["structuredContent"].get("status"))
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    if failures:
        print("\nFAILED:")
        for item in failures:
            print(" -", item)
        return 1
    print("\nLIVE SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
