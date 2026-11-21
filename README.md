# reciprocal-ruler-mcp

An [MCP](https://modelcontextprotocol.io) (Model Context Protocol) server that gives any
MCP-capable agent — Claude Desktop, Claude Code, Cursor, or your own client — a settlement
oracle for agent-to-agent transactions.

When two AI agents fail to agree on *what must be proven before money moves*, Reciprocal Ruler
acts as the third party: it locks the agreed terms into a deterministic `quote_hash`, then judges
the settlement afterwards (`SETTLED_OK`, `COMPENSATED`, `PENDING_RECONCILIATION`) and compensates
shortfalls out of the agreed holdback. The service is paid per call with
[x402](https://www.x402.org) in USDC on Base — no account, no API key, no invoice.

- API: <https://reciprocla-ruler-x402.onrender.com> · OpenAPI docs: `/docs`
- Free trial: **3 evaluation calls per client**, no payment, no signup
- Standard library only — the server has **no third-party dependencies**

## Tools

| Tool | Price | What it does |
|---|---|---|
| `ruler_trial_reconcile` | free (3 calls/client) | Runs the real reconciliation engine and returns the judgment, so you can evaluate it before paying. Not written to the audit ledger. |
| `ruler_quote_lock` | $0.05 | Locks the terms both agents agreed on and returns a deterministic `quote_hash` they can cite later. |
| `ruler_reconcile` | $0.25 | Idempotent settlement judgment recorded in an append-only audit ledger; retries with the same `idempotency_key` return the original result instead of charging again. |

Amounts are **KRW integers**. A reconciliation compares `expected_amount_krw` with
`actual_settled_krw` and uses `holdback_krw` as the compensation pool:

```json
{
  "expected_amount_krw": 220000,
  "actual_settled_krw": 180000,
  "holdback_krw": 22000,
  "event_type": "POST_TRADE_REFUND"
}
```

→ `{"status": "COMPENSATED", "compensated_krw": 22000, "uncovered_krw": 18000, "credit_delta": -17}`

`event_type` is `POST_TRADE_REFUND` (a refund or shortfall after the trade) or
`HALF_OPEN_TIMEOUT` (an unfinished settlement at timer expiry — such a shortfall is left
`PENDING_RECONCILIATION`, never auto-compensated).

## How the paid calls work

This MCP server holds no wallet and signs nothing. On a paid tool the call happens in two steps:

1. **First call** (no `payment_header`) returns the HTTP 402 x402 challenge, decoded for you:
   scheme, network, USDC asset, amount in atomic units and USDC, `payTo`, timeout, plus what to do next.
2. **Pay and retry**: produce the signed payment payload with an x402-capable client
   (the `x402` Python SDK, `x402-fetch` / `x402-axios` for Node, or a Coinbase CDP / AgentKit
   wallet) and pass it back in the tool's `payment_header` argument. The same call then returns HTTP 200.

Example payment summary from a 402 response:

```json
{
  "status": "PAYMENT_REQUIRED",
  "payment_summary": {
    "x402_version": 2,
    "scheme": "exact",
    "network": "eip155:8453",
    "network_name": "Base mainnet (eip155:8453)",
    "asset_name": "USDC",
    "amount_atomic": "50000",
    "amount_usdc": "0.050000",
    "pay_to": "0x…",
    "resource_url": "https://reciprocla-ruler-x402.onrender.com/v1/quote-lock"
  },
  "how_to_pay": ["…"]
}
```

## Install

The package is not published on PyPI yet, so install it from this folder:

```bash
pip install .                 # console script `reciprocal-ruler-mcp`
pip install -e ".[dev]"       # editable, with pytest, for development
```

Nothing outside the Python standard library is pulled in. Either install path gives you the
`reciprocal-ruler-mcp` console script, and `python -m reciprocal_ruler_mcp` works as well. The
server speaks MCP over **stdio**.

## Connect a client

### Claude Desktop

`claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows:
`%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "reciprocal-ruler": {
      "command": "python3",
      "args": ["-m", "reciprocal_ruler_mcp"],
      "env": {
        "RULER_API_BASE": "https://reciprocla-ruler-x402.onrender.com",
        "RULER_TRIAL_CLIENT_ID": "my-agent-01"
      }
    }
  }
}
```

### Claude Code

```bash
claude mcp add reciprocal-ruler -- python3 -m reciprocal_ruler_mcp
```

### Cursor

`~/.cursor/mcp.json` — same `mcpServers` shape as the Claude Desktop example above.
See [`examples/`](examples/) for ready-to-copy files.

### Run without installing

The server is a single standard-library module, so a checkout works directly:

```json
{
  "mcpServers": {
    "reciprocal-ruler": {
      "command": "python3",
      "args": ["/absolute/path/to/src/reciprocal_ruler_mcp/server.py"]
    }
  }
}
```

## Configuration

| Environment variable | Default | Purpose |
|---|---|---|
| `RULER_API_BASE` | `https://reciprocla-ruler-x402.onrender.com` | API base URL (point it at a local or self-hosted instance if you run one). |
| `RULER_TRIAL_CLIENT_ID` | *unset* | Sent as `X-Trial-Client` so your 3 free calls follow this agent id instead of the shared IP address. Recommended behind a NAT or shared proxy. |
| `RULER_TIMEOUT_SECONDS` | `90` | Per-request timeout. The free hosting tier sleeps when idle, so the first call of a session may need a warm-up; the client retries once on a network failure. |

Every tool also accepts `trial_client_id` (`ruler_trial_reconcile`) and `payment_header`
(the paid tools) as arguments, which override the environment defaults for that call.

## Protocol

`initialize` (protocol versions `2025-06-18`, `2025-03-26`, `2024-11-05`, `2024-10-07`),
`tools/list`, `tools/call`, `ping`, and the standard notifications. Diagnostics go to **stderr**;
stdout carries JSON-RPC frames only.

Tool failures come back as a normal result with `isError: true` and a JSON body explaining the
case (`PAYMENT_REQUIRED`, `TRIAL_EXHAUSTED`, `VERSION_MISMATCH`, `INVALID_ARGUMENTS`,
`API_UNREACHABLE`), so an agent can recover instead of crashing.

## Test

```bash
pip install -e ".[dev]"
pytest -q
```

`tests/test_protocol.py` starts the real server as a subprocess against a local mock of the API
and drives a full JSON-RPC exchange — `initialize` → `tools/list` → `tools/call` — including the
402 payment path, the trial quota, and error handling, all offline.

`scripts/live_smoke.py` calls the live public API with the tools (free trial only; it never
spends money) to check a deployment end to end. `scripts/sdk_interop_check.py` connects with the
official MCP Python SDK client (`pip install mcp`) to confirm the stdio server works with a real
client implementation, not only with raw frames.

## License

MIT. The service behind it is operated as Reciprocal Ruler; the API terms are whatever the
endpoint itself reports at request time.
