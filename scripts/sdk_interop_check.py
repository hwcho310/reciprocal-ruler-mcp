#!/usr/bin/env python3
"""Optional interop check: connect to this server with the official MCP Python SDK client.

Proves the stdio server works with a real client implementation, not only with the hand-rolled
frames in tests/. Needs the SDK and the network (the free trial tools are called; no payment):

    pip install mcp && python3 scripts/sdk_interop_check.py
"""

import asyncio
import os
import sys
import uuid
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

SRC = Path(__file__).resolve().parents[1] / "src"


async def main() -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env.pop("RULER_API_BASE", None)
    env["RULER_TRIAL_CLIENT_ID"] = "sdk-interop-" + uuid.uuid4().hex[:8]
    params = StdioServerParameters(command=sys.executable, args=["-m", "reciprocal_ruler_mcp"], env=env)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            info = getattr(init, "server_info", None) or getattr(init, "serverInfo")
            name = getattr(info, "name", "")
            version = getattr(info, "version", "")
            protocol = getattr(init, "protocol_version", None) or getattr(init, "protocolVersion", None)
            print("server:", name, version, "protocol:", protocol)
            tools = await session.list_tools()
            print("tools:", [tool.name for tool in tools.tools])
            for tool in tools.tools:
                schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
                print(f"  - {tool.name}: required={schema.get('required')}")

            result = await session.call_tool("ruler_trial_reconcile", {
                "expected_amount_krw": 220000,
                "actual_settled_krw": 180000,
                "holdback_krw": 22000,
            })
            text = result.content[0].text if result.content else ""
            structured_content = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
            print("trial call isError:", result.is_error, "| structured:", str(structured_content)[:160])
            print("text head:", text[:200].replace("\n", " "))

            quote = await session.call_tool("ruler_quote_lock", {
                "buyer_version": "1.0", "supplier_version": "1.0",
                "buyer_id": "sdk-buyer", "supplier_id": "sdk-supplier",
                "terms": {"price_krw": 220000},
            })
            structured = getattr(quote, "structured_content", None) or getattr(quote, "structuredContent", None) or {}
            print("quote lock isError:", quote.is_error, "| status:", structured.get("status"),
                  "| amount:", (structured.get("payment_summary") or {}).get("amount_usdc"))

            if name != "reciprocal-ruler" or len(tools.tools) != 3:
                print("INTEROP FAILED")
                return 1
            if result.is_error or (structured.get("status") != "PAYMENT_REQUIRED"):
                print("INTEROP FAILED: unexpected tool results")
                return 1
    print("SDK INTEROP OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
