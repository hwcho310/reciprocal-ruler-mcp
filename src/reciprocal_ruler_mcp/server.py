#!/usr/bin/env python3
"""Reciprocal Ruler MCP server.

A stdio Model Context Protocol server that exposes the Reciprocal Ruler x402 API
to any MCP-capable agent (Claude Desktop, Claude Code, Cursor, ...):

  * ruler_trial_reconcile - free settlement reconciliation (3 calls per client)
  * ruler_quote_lock      - paid x402 quote lock ($0.05, USDC on Base)
  * ruler_reconcile       - paid x402 settlement reconciliation ($0.25, USDC on Base)

The paid tools return the decoded x402 payment challenge (PAYMENT-REQUIRED) with
Base USDC instructions when no payment is supplied, so the calling agent can pay
with its own wallet and retry by passing the signed payload back through the
optional ``payment_header`` argument. This server never holds a key and never
signs anything itself.

Standard library only: no third-party dependency is required to run it.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib import error as urlerror
from urllib import request as urlrequest

SERVER_NAME = "reciprocal-ruler"
SERVER_VERSION = "0.1.0"
DEFAULT_API_BASE = "https://reciprocal-ruler-x402.vercel.app"
DEFAULT_TIMEOUT_SECONDS = 90.0
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
USDC_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
NETWORK_LABELS = {
    "eip155:8453": "Base mainnet (eip155:8453)",
    "eip155:84532": "Base Sepolia testnet (eip155:84532)",
}


def log(message: str) -> None:
    """Diagnostics go to stderr: stdout is reserved for JSON-RPC frames."""
    print(f"[{SERVER_NAME}-mcp] {message}", file=sys.stderr, flush=True)


# ==============================================================================
# HTTP client for the x402 API
# ==============================================================================
class ToolInputError(ValueError):
    """Raised when a tool argument is missing or has the wrong type."""


class ApiResult:
    def __init__(self, status: int, body: Any, headers: Dict[str, str], url: str) -> None:
        self.status = status
        self.body = body
        self.headers = headers
        self.url = url


def api_base() -> str:
    return (os.getenv("RULER_API_BASE") or DEFAULT_API_BASE).rstrip("/")


def api_timeout() -> float:
    raw = os.getenv("RULER_TIMEOUT_SECONDS")
    try:
        return float(raw) if raw else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def _header_map(raw_headers: Iterable[Tuple[str, str]]) -> Dict[str, str]:
    return {key.lower(): value for key, value in raw_headers}


def api_post(path: str, payload: Dict[str, Any], extra_headers: Optional[Dict[str, str]] = None) -> ApiResult:
    """POST JSON to the x402 API. One retry on a network/timeout failure.

    The free Render tier sleeps when idle, so the first call of a session can
    take a while to wake the service; the retry keeps that from surfacing as a
    hard tool failure.
    """
    url = api_base() + path
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": f"{SERVER_NAME}-mcp/{SERVER_VERSION}"}
    if extra_headers:
        headers.update(extra_headers)

    last_error: Optional[Exception] = None
    for attempt in range(2):
        request = urlrequest.Request(url, data=data, headers=headers, method="POST")
        try:
            with urlrequest.urlopen(request, timeout=api_timeout()) as response:
                return _to_result(response.status, response.read(), response.headers, url)
        except urlerror.HTTPError as exc:  # 402 and 4xx/5xx carry their own body
            return _to_result(exc.code, exc.read(), exc.headers, url)
        except (urlerror.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt == 0:
                log(f"{path}: {exc} - retrying once")
    raise RuntimeError(f"could not reach {url}: {last_error}")


def _to_result(status: int, raw_body: bytes, headers: Any, url: str) -> ApiResult:
    text = raw_body.decode("utf-8", errors="replace") if raw_body else ""
    try:
        body: Any = json.loads(text) if text else None
    except json.JSONDecodeError:
        body = text
    return ApiResult(status, body, _header_map(headers.items()), url)


# ==============================================================================
# x402 payment challenge decoding
# ==============================================================================
def _b64_json(value: str) -> Optional[Any]:
    cleaned = value.strip()
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        for pad in ("", "=", "==", "==="):
            try:
                return json.loads(decoder(cleaned + pad))
            except (binascii.Error, ValueError, json.JSONDecodeError):
                continue
    return None


def payment_challenge(result: ApiResult) -> Optional[Dict[str, Any]]:
    """Return the x402 challenge, from the PAYMENT-REQUIRED header or the body."""
    header = result.headers.get("payment-required")
    if header:
        decoded = _b64_json(header)
        if isinstance(decoded, dict):
            return decoded
    if isinstance(result.body, dict) and "accepts" in result.body:
        return result.body
    return None


def _atomic_to_usdc(amount: Any) -> Optional[str]:
    try:
        return f"{int(str(amount)) / 1_000_000:.6f}"
    except (TypeError, ValueError):
        return None


def payment_summary(challenge: Dict[str, Any]) -> Dict[str, Any]:
    accepts = challenge.get("accepts") or []
    option = accepts[0] if accepts and isinstance(accepts[0], dict) else {}
    network = option.get("network", "")
    resource = challenge.get("resource") if isinstance(challenge.get("resource"), dict) else {}
    return {
        "x402_version": challenge.get("x402Version"),
        "scheme": option.get("scheme"),
        "network": network,
        "network_name": NETWORK_LABELS.get(network, network or "unknown"),
        "asset": option.get("asset"),
        "asset_name": "USDC" if option.get("asset") == USDC_ASSET else option.get("extra", {}).get("name"),
        "amount_atomic": option.get("amount"),
        "amount_usdc": _atomic_to_usdc(option.get("amount")),
        "pay_to": option.get("payTo"),
        "max_timeout_seconds": option.get("maxTimeoutSeconds"),
        "resource_url": resource.get("url"),
        "resource_description": resource.get("description"),
    }


def payment_instructions(summary: Dict[str, Any]) -> List[str]:
    amount = summary.get("amount_usdc") or "the amount above"
    return [
        f"Pay {amount} USDC on {summary.get('network_name')} to {summary.get('pay_to')} "
        f"using the x402 '{summary.get('scheme')}' scheme (EIP-3009 transferWithAuthorization, "
        f"settle within {summary.get('max_timeout_seconds', 300)}s).",
        "This MCP server holds no wallet and cannot sign payments. Use an x402-capable client "
        "to produce the signed payment payload: the x402 Python SDK, x402-fetch / x402-axios "
        "for Node, or a Coinbase CDP / AgentKit wallet.",
        "Retry the same tool call with the signed payload in `payment_header` "
        "(sent as the X-PAYMENT request header); the API then answers HTTP 200.",
        "Not ready to pay? ruler_trial_reconcile returns up to 3 free evaluations per client.",
    ]


def payment_required_result(tool: str, paid_path: str, result: ApiResult) -> Dict[str, Any]:
    challenge = payment_challenge(result)
    out: Dict[str, Any] = {
        "ok": False,
        "status": "PAYMENT_REQUIRED",
        "tool": tool,
        "http_status": result.status,
        "paid_endpoint": paid_path,
        "reason": "HTTP 402 from the x402 gate: this call needs payment (or a payment_header).",
    }
    if challenge:
        summary = payment_summary(challenge)
        out["payment_summary"] = summary
        out["payment_challenge"] = challenge
        out["how_to_pay"] = payment_instructions(summary)
    else:
        out["response_body"] = result.body
        out["how_to_pay"] = ["The endpoint did not return a decodable x402 challenge; see response_body."]
    return out


# ==============================================================================
# Tool argument helpers
# ==============================================================================
def _require_str(args: Dict[str, Any], key: str) -> str:
    value = args.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ToolInputError(f"`{key}` is required and must be a non-empty string")
    if not isinstance(value, str):
        raise ToolInputError(f"`{key}` must be a string")
    return value


def _optional_str(args: Dict[str, Any], key: str, default: str = "") -> str:
    value = args.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ToolInputError(f"`{key}` must be a string")
    return value


def _require_int(args: Dict[str, Any], key: str, minimum: Optional[int] = None) -> int:
    if key not in args or args[key] is None:
        raise ToolInputError(f"`{key}` is required and must be an integer (KRW)")
    value = args[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise ToolInputError(f"`{key}` must be an integer amount in KRW")
    value = int(value)
    if minimum is not None and value < minimum:
        raise ToolInputError(f"`{key}` must be >= {minimum}")
    return value


def _optional_int(args: Dict[str, Any], key: str, default: int = 0) -> int:
    if args.get(key) is None:
        return default
    return _require_int(args, key)


EVENT_TYPES = ("POST_TRADE_REFUND", "HALF_OPEN_TIMEOUT")


def _event_type(args: Dict[str, Any]) -> str:
    value = _optional_str(args, "event_type", "POST_TRADE_REFUND") or "POST_TRADE_REFUND"
    if value not in EVENT_TYPES:
        raise ToolInputError(f"`event_type` must be one of {list(EVENT_TYPES)}")
    return value


def reconcile_payload(args: Dict[str, Any], auto_idempotency: bool) -> Dict[str, Any]:
    idempotency_key = _optional_str(args, "idempotency_key", "")
    if not idempotency_key:
        if not auto_idempotency:
            raise ToolInputError(
                "`idempotency_key` is required for the paid endpoint: it is what makes a retry "
                "free instead of a second charge. Reuse the same key when retrying."
            )
        idempotency_key = f"mcp-{uuid.uuid4().hex[:16]}"
    return {
        "idempotency_key": idempotency_key,
        "tx_id": _optional_str(args, "tx_id", "") or idempotency_key,
        "quote_hash": _optional_str(args, "quote_hash", ""),
        "expected_amount_krw": _require_int(args, "expected_amount_krw", 0),
        "actual_settled_krw": _require_int(args, "actual_settled_krw", 0),
        "holdback_krw": _optional_int(args, "holdback_krw", 0),
        "event_type": _event_type(args),
    }


def _payment_header(args: Dict[str, Any]) -> Dict[str, str]:
    value = _optional_str(args, "payment_header", "")
    if not value:
        return {}
    return {"X-PAYMENT": value.strip()}


def _trial_headers(args: Dict[str, Any]) -> Dict[str, str]:
    client_id = _optional_str(args, "trial_client_id", "") or (os.getenv("RULER_TRIAL_CLIENT_ID") or "")
    return {"X-Trial-Client": client_id.strip()} if client_id.strip() else {}


def _reconcile_view(body: Dict[str, Any]) -> Dict[str, Any]:
    view = {
        "judgment": body.get("status"),
        "tx_id": body.get("tx_id"),
        "expected_krw": body.get("expected_krw"),
        "actual_krw": body.get("actual_krw"),
        "holdback_krw": body.get("holdback_krw"),
        "compensated_krw": body.get("compensated_krw"),
        "uncovered_krw": body.get("uncovered_krw"),
        "credit_delta": body.get("credit_delta"),
    }
    return {key: value for key, value in view.items() if value is not None}


def _judgment_note(body: Dict[str, Any]) -> str:
    status = body.get("status")
    if status == "SETTLED_OK":
        return "Settled as expected: nothing to compensate."
    if status == "COMPENSATED":
        return (f"Shortfall covered from holdback: {body.get('compensated_krw')} KRW compensated, "
                f"{body.get('uncovered_krw')} KRW uncovered.")
    if status == "PENDING_RECONCILIATION":
        return "Half-open timeout with an unpaid balance: left pending, no automatic compensation."
    return f"Judgment: {status}"


# ==============================================================================
# Tools
# ==============================================================================
def tool_ruler_trial_reconcile(args: Dict[str, Any]) -> Dict[str, Any]:
    payload = reconcile_payload(args, auto_idempotency=True)
    result = api_post("/v1/trial/reconcile", payload, _trial_headers(args))
    if result.status == 200 and isinstance(result.body, dict):
        body = result.body
        usage = body.get("trial") or {}
        return {
            "ok": True,
            "free_trial": True,
            "http_status": 200,
            "result": _reconcile_view(body),
            "summary": _judgment_note(body),
            "trial_usage": usage,
            "note": body.get("note"),
        }
    if result.status == 402 and isinstance(result.body, dict) and result.body.get("error") == "TRIAL_EXHAUSTED":
        return {
            "ok": False,
            "status": "TRIAL_EXHAUSTED",
            "http_status": 402,
            "message": result.body.get("message"),
            "trial_usage": result.body.get("trial_usage"),
            "paid_endpoints": result.body.get("paid_endpoints"),
            "next_step": "Use ruler_reconcile (paid, $0.25 USDC on Base) for further reconciliations, "
            "or ruler_quote_lock ($0.05) to lock terms first.",
        }
    if result.status == 402:
        return payment_required_result("ruler_trial_reconcile", "/v1/trial/reconcile", result)
    return _unexpected("ruler_trial_reconcile", result)


def tool_ruler_quote_lock(args: Dict[str, Any]) -> Dict[str, Any]:
    terms = args.get("terms")
    if not isinstance(terms, dict) or not terms:
        raise ToolInputError("`terms` is required and must be an object, e.g. "
                             '{"price_krw": 220000, "refund_ratio_on_cancel": 1.0, "holdback_ratio": 0.10}')
    payload = {
        "buyer_version": _require_str(args, "buyer_version"),
        "supplier_version": _require_str(args, "supplier_version"),
        "buyer_id": _require_str(args, "buyer_id"),
        "supplier_id": _require_str(args, "supplier_id"),
        "terms": terms,
    }
    result = api_post("/v1/quote-lock", payload, _payment_header(args))
    if result.status == 200 and isinstance(result.body, dict):
        body = result.body
        return {
            "ok": True,
            "http_status": 200,
            "result": {
                "quote_hash": body.get("quote_hash"),
                "rule_version": body.get("rule_version"),
                "locked_at": body.get("locked_at"),
                "status": body.get("status"),
            },
            "summary": "Quote locked. Give the quote_hash to both agents; pass it back to "
                       "ruler_reconcile when the transaction settles.",
        }
    if result.status == 409:
        return {
            "ok": False,
            "status": "VERSION_MISMATCH",
            "http_status": 409,
            "response_body": result.body,
            "summary": "The two agents hold different rule versions, so no quote_hash is issued. "
                       "Align buyer_version and supplier_version, then retry.",
        }
    if result.status == 402:
        return payment_required_result("ruler_quote_lock", "/v1/quote-lock", result)
    return _unexpected("ruler_quote_lock", result)


def tool_ruler_reconcile(args: Dict[str, Any]) -> Dict[str, Any]:
    payload = reconcile_payload(args, auto_idempotency=False)
    result = api_post("/v1/reconcile", payload, _payment_header(args))
    if result.status == 200 and isinstance(result.body, dict):
        body = result.body
        return {
            "ok": True,
            "http_status": 200,
            "result": dict(_reconcile_view(body), idempotent_replay=body.get("idempotent_replay")),
            "summary": _judgment_note(body),
            "ledger": "Written to the append-only audit ledger.",
        }
    if result.status == 402:
        return payment_required_result("ruler_reconcile", "/v1/reconcile", result)
    return _unexpected("ruler_reconcile", result)


def _unexpected(tool: str, result: ApiResult) -> Dict[str, Any]:
    return {
        "ok": False,
        "status": "UNEXPECTED_RESPONSE",
        "tool": tool,
        "http_status": result.status,
        "url": result.url,
        "response_body": result.body,
        "summary": f"The API answered HTTP {result.status}, which this tool does not expect. "
        "Check the request arguments and the service status (GET /health).",
    }


TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "ruler_trial_reconcile",
        "title": "Reciprocal Ruler - free trial reconciliation",
        "description": (
            "Run a settlement reconciliation between two AI agents for free (up to 3 calls per client). "
            "Compares the expected amount with what actually settled and returns a deterministic judgment: "
            "SETTLED_OK, COMPENSATED (shortfall covered from the holdback), or PENDING_RECONCILIATION "
            "(half-open timeout, left pending). Use it to evaluate whether Reciprocal Ruler fits your "
            "agent-to-agent transaction flow before paying. Amounts are KRW integers."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "expected_amount_krw": {"type": "integer", "minimum": 0,
                                        "description": "Amount the buyer and supplier agreed the supplier should receive (KRW)."},
                "actual_settled_krw": {"type": "integer", "minimum": 0,
                                       "description": "Amount that actually settled (KRW)."},
                "holdback_krw": {"type": "integer", "minimum": 0, "default": 0,
                                 "description": "Holdback reserved from the payment, the pool used to compensate a shortfall (KRW)."},
                "event_type": {"type": "string", "enum": list(EVENT_TYPES), "default": "POST_TRADE_REFUND",
                               "description": "POST_TRADE_REFUND for a refund/shortfall after the trade; "
                                              "HALF_OPEN_TIMEOUT for an unfinished settlement when the timer expires."},
                "tx_id": {"type": "string", "description": "Your transaction or order id. Defaults to the idempotency key."},
                "idempotency_key": {"type": "string", "description": "Optional on the free trial; generated when omitted."},
                "quote_hash": {"type": "string", "description": "quote_hash from ruler_quote_lock for this trade, when one was issued."},
                "trial_client_id": {"type": "string",
                                    "description": "Stable id for your agent (sent as X-Trial-Client) so the 3 free calls follow "
                                                   "this id instead of the shared IP address."},
            },
            "required": ["expected_amount_krw", "actual_settled_krw"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "ruler_quote_lock",
        "title": "Reciprocal Ruler - quote lock (x402 paid)",
        "description": (
            "Lock the trade terms the two agents agreed on and get a deterministic quote_hash both sides can "
            "cite later. Costs $0.05 USDC on Base, paid with x402. Without a payment this tool returns the "
            "HTTP 402 payment challenge decoded into Base USDC instructions; pay it with an x402 wallet and "
            "pass the signed payload back as payment_header to complete the call. If the two agents run "
            "different rule versions the call is refused with VERSION_MISMATCH (409) and nothing is charged."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "buyer_version": {"type": "string", "description": "Rule version the buyer agent runs, e.g. '1.0'."},
                "supplier_version": {"type": "string", "description": "Rule version the supplier agent runs; must match buyer_version."},
                "buyer_id": {"type": "string", "description": "Identifier of the buyer agent."},
                "supplier_id": {"type": "string", "description": "Identifier of the supplier agent."},
                "terms": {
                    "type": "object",
                    "description": "Agreed terms, at minimum price_krw. Also useful: free_cancel_until, "
                                   "refund_ratio_on_cancel, holdback_ratio.",
                    "properties": {
                        "price_krw": {"type": "integer"},
                        "free_cancel_until": {"type": "string"},
                        "refund_ratio_on_cancel": {"type": "number"},
                        "holdback_ratio": {"type": "number"},
                    },
                    "required": ["price_krw"],
                    "additionalProperties": True,
                },
                "payment_header": {"type": "string",
                                   "description": "Signed x402 payment payload for this request (sent as X-PAYMENT). "
                                                  "Omit it to receive the payment challenge."},
            },
            "required": ["buyer_version", "supplier_version", "buyer_id", "supplier_id", "terms"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    },
    {
        "name": "ruler_reconcile",
        "title": "Reciprocal Ruler - settlement reconciliation (x402 paid)",
        "description": (
            "Idempotent settlement reconciliation recorded in an append-only audit ledger. Costs $0.25 USDC "
            "on Base via x402; without payment it returns the decoded payment challenge and Base USDC "
            "instructions. Reusing the same idempotency_key makes a retry return the original judgment "
            "instead of charging again, so always pass the key your first attempt used. Judgment is one of "
            "SETTLED_OK, COMPENSATED, PENDING_RECONCILIATION."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "idempotency_key": {"type": "string",
                                    "description": "Your stable key for this settlement (required: it makes retries free)."},
                "expected_amount_krw": {"type": "integer", "minimum": 0,
                                        "description": "Agreed amount the supplier should receive (KRW)."},
                "actual_settled_krw": {"type": "integer", "minimum": 0,
                                       "description": "Amount that actually settled (KRW)."},
                "holdback_krw": {"type": "integer", "minimum": 0, "default": 0,
                                 "description": "Holdback reserved from the payment (KRW)."},
                "event_type": {"type": "string", "enum": list(EVENT_TYPES), "default": "POST_TRADE_REFUND"},
                "tx_id": {"type": "string", "description": "Your transaction or order id."},
                "quote_hash": {"type": "string", "description": "quote_hash issued by ruler_quote_lock for this trade."},
                "payment_header": {"type": "string",
                                   "description": "Signed x402 payment payload (X-PAYMENT). Omit to receive the challenge."},
            },
            "required": ["idempotency_key", "expected_amount_krw", "actual_settled_krw"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    },
]

TOOL_HANDLERS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "ruler_trial_reconcile": tool_ruler_trial_reconcile,
    "ruler_quote_lock": tool_ruler_quote_lock,
    "ruler_reconcile": tool_ruler_reconcile,
}


# ==============================================================================
# JSON-RPC / MCP protocol
# ==============================================================================
def jsonrpc_result(request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def jsonrpc_error(request_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def handle_initialize(params: Dict[str, Any]) -> Dict[str, Any]:
    requested = params.get("protocolVersion")
    version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "title": "Reciprocal Ruler", "version": SERVER_VERSION},
        "instructions": (
            "Reciprocal Ruler is a settlement oracle for transactions between AI agents. "
            "Start with ruler_trial_reconcile (3 free calls) to see the judgment; the paid tools "
            "ruler_quote_lock ($0.05) and ruler_reconcile ($0.25) settle in USDC on Base over x402. "
            "Amounts are KRW integers. Call /health on the API to check the service before a paid call."
        ),
    }


def call_tool(params: Dict[str, Any]) -> Dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if name not in TOOL_HANDLERS:
        raise KeyError(f"unknown tool: {name}")
    if not isinstance(arguments, dict):
        raise ToolInputError("`arguments` must be an object")
    handler = TOOL_HANDLERS[name]
    try:
        outcome = handler(arguments)
    except ToolInputError as exc:
        return _tool_result({"ok": False, "status": "INVALID_ARGUMENTS", "summary": str(exc)}, is_error=True)
    except RuntimeError as exc:  # network failure after the retry
        return _tool_result({"ok": False, "status": "API_UNREACHABLE", "summary": str(exc),
                             "hint": "The service sleeps when idle on the free tier; retry in a few seconds "
                                     "or check " + api_base() + "/health"}, is_error=True)
    except Exception as exc:  # noqa: BLE001 - a tool failure must not kill the server
        log(f"tool {name} crashed: {exc!r}")
        return _tool_result({"ok": False, "status": "TOOL_ERROR", "summary": f"{type(exc).__name__}: {exc}"},
                            is_error=True)
    return _tool_result(outcome, is_error=not outcome.get("ok", True))


def _tool_result(payload: Dict[str, Any], is_error: bool) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "structuredContent": payload,
        "isError": is_error,
    }
    return result


def handle_message(message: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(message, dict):
        return jsonrpc_error(None, -32600, "Invalid Request")
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    is_notification = "id" not in message

    if not isinstance(params, dict):
        params = {}

    if method == "initialize":
        return None if is_notification else jsonrpc_result(request_id, handle_initialize(params))
    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return None if is_notification else jsonrpc_result(request_id, {})
    if method == "tools/list":
        return None if is_notification else jsonrpc_result(request_id, {"tools": TOOL_SPECS})
    if method == "tools/call":
        if is_notification:
            return None
        try:
            return jsonrpc_result(request_id, call_tool(params))
        except KeyError as exc:
            return jsonrpc_error(request_id, -32602, "Unknown tool", {"tool": str(exc)})
        except ToolInputError as exc:
            return jsonrpc_error(request_id, -32602, str(exc))
    if method in ("resources/list", "prompts/list"):
        key = method.split("/")[0]
        return None if is_notification else jsonrpc_result(request_id, {key: []})
    if is_notification:
        log(f"ignoring notification {method}")
        return None
    return jsonrpc_error(request_id, -32601, f"Method not found: {method}")


def send(message: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    log(f"v{SERVER_VERSION} on stdio, API {api_base()} (tools: {', '.join(TOOL_HANDLERS)})")
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                send(jsonrpc_error(None, -32700, f"Parse error: {exc}"))
                continue
            response = handle_message(message)
            if response is not None:
                send(response)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
