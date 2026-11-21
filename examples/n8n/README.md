# Reciprocal Ruler in n8n — x402 settlement automation

A ready-to-import [n8n](https://n8n.io) workflow that calls the Reciprocal Ruler settlement
oracle the way an agent would: lock the terms both sides agreed on, then reconcile what was
actually settled and compensate the shortfall out of the holdback.

| File | What it is |
|---|---|
| [`reciprocal_ruler_n8n_workflow.json`](reciprocal_ruler_n8n_workflow.json) | The workflow — **Import from File** in n8n |
| [`reciprocal_ruler_x402_payer.py`](reciprocal_ruler_x402_payer.py) | Optional local x402 relay that pays the 402 and retries for you (stdlib only) |
| [`reciprocal_ruler_n8n_guide.md`](reciprocal_ruler_n8n_guide.md) | Full guide (in Korean): node-by-node walkthrough, data contract, payment path, troubleshooting |

API: <https://reciprocal-ruler-x402.vercel.app> · OpenAPI docs at `/docs` ·
prices `POST /v1/quote-lock` **$0.05**, `POST /v1/reconcile` **$0.25**, USDC on Base.

## Import and run (about 5 minutes)

1. In n8n: **⋯ → Import from File** → `reciprocal_ruler_n8n_workflow.json`.
2. Open `1. Sample Event (edit me)` and replace the values with your own trade.
3. Press **Execute Workflow** on the Manual Trigger.
4. Read `6. Classify Result`: `verdict` is `MATCHED`, `EXCEPTION`, or `PENDING`.

In production, disable the Manual Trigger and post the same event JSON to the workflow's
Webhook node (`POST /webhook/ruler-settlement`).

## What the workflow does

```
Manual Trigger ─┐
                ├→ 2. Normalize & Validate → 3a. POST /v1/quote-lock ($0.05)
Webhook ────────┘                                   │
                                   200 ─────────────┴──────── 402 / 409 / 422
                                    │                              │
                          4. Attach quote_hash                     │
                                    │                              │
                      5a. POST /v1/reconcile ($0.25)               │
                                    │                              │
                           200 ─────┴──── 402 / 422 / 5xx          │
                            │                    │                 │
                   6. Classify Result      5c. 402/Error ──────────┤
                            │                                       │
                    7. MATCHED? ─── exception ──────────────────────┤
                            │                                       ↓
                   8a. Audit Record                    10. Slack / 10b. Email alerts
                            │                              (placeholders, disabled by default)
                   8b. Audit Log Append
                      (placeholder, disabled by default)
```

- `2. Normalize & Validate` builds the two request bodies and pre-checks required fields and
  the rules version, so the obvious `409`s never leave n8n.
- `3a` / `5a` are the paid calls. They run with **neverError + fullResponse**, so a `402` does
  not stop the workflow — the status code and headers flow on to be interpreted.
- `4. Attach quote_hash` carries the deterministic hash of the locked terms into the reconcile
  call; `6. Classify Result` maps the answer to a single `verdict`.
- `8a` / `8b` write an audit record (and optionally append it to your own log); `10` / `10b`
  are placeholder Slack and email alerts.

## Paying the x402 call

`3a` and `5a` return HTTP 402 with a payment challenge (scheme, network, USDC amount, `payTo`,
timeout). Two ways through it:

- **Relay (recommended, no n8n code):** run `python3 reciprocal_ruler_x402_payer.py`, set
  `EVM_PRIVATE_KEY` for a small dedicated wallet, then point the two HTTP nodes at the relay
  (`http://<relay-host>:8787/v1/quote-lock`, `…/v1/reconcile`). The relay signs the x402
  payment, retries the request and returns the upstream response unchanged.
- **In-workflow:** build the `PAYMENT-SIGNATURE` header yourself (USDC EIP-3009
  `transferWithAuthorization`, base64 JSON) in a Code node and pass it on the retry.

Node-by-node details, the request/response data contract, and error cases are in the guide.

## Data contract

The reconcile call compares what both sides said would arrive with what did:

```json
{
  "expected_amount_krw": 220000,
  "actual_settled_krw": 180000,
  "holdback_krw": 22000,
  "event_type": "POST_TRADE_REFUND"
}
```

→ `{"status": "COMPENSATED", "compensated_krw": 22000, "uncovered_krw": 18000, "credit_delta": -17}`

`event_type` is `POST_TRADE_REFUND` or `HALF_OPEN_TIMEOUT`; a shortfall found at timer expiry is
left `PENDING_RECONCILIATION` instead of being compensated automatically. Amounts are KRW
integers.

## Notes

- The same service is available to MCP clients without n8n — see the repository README and
  [`examples/claude_desktop_config.json`](../claude_desktop_config.json).
- `ruler_trial_reconcile` gives 3 free evaluation calls per client, so you can check the
  judgment before wiring up a wallet.
