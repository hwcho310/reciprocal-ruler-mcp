"""End-to-end protocol tests: the real server as a subprocess, a local mock of the x402 API.

Nothing here touches the network or spends money. Each test drives actual JSON-RPC frames over
stdin/stdout, so the transport, the dispatcher and the tool handlers are all exercised.
"""

import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAY_TO = "0x1111111111111111111111111111111111111111"


# ------------------------------------------------------------------------------
# Mock of the Reciprocal Ruler API
# ------------------------------------------------------------------------------
def _challenge(price_atomic: str, path: str) -> dict:
    return {
        "x402Version": 2,
        "error": "Payment required",
        "resource": {"url": f"http://api.test{path}", "serviceName": "Reciprocal Ruler"},
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "asset": USDC,
                "amount": price_atomic,
                "payTo": PAY_TO,
                "maxTimeoutSeconds": 300,
                "extra": {"name": "USD Coin", "version": "2"},
            }
        ],
    }


class MockApi(BaseHTTPRequestHandler):
    trial_counts: dict = {}
    calls: list = []

    def log_message(self, *args):  # keep pytest output clean
        pass

    def _send(self, status, body, headers=None):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        MockApi.calls.append((self.path, payload, {k.lower(): v for k, v in self.headers.items()}))
        paid = bool(self.headers.get("X-PAYMENT"))

        if self.path == "/v1/trial/reconcile":
            key = self.headers.get("X-Trial-Client") or self.client_address[0]
            used = MockApi.trial_counts.get(key, 0) + 1
            MockApi.trial_counts[key] = used
            if used > 3:
                return self._send(402, {
                    "error": "TRIAL_EXHAUSTED",
                    "message": "Free trial used (3/3 calls for this client).",
                    "trial_usage": {"used": 3, "limit": 3, "remaining": 0},
                    "paid_endpoints": [{"path": "/v1/reconcile", "price_usd": "0.25"}],
                })
            return self._send(200, {
                "status": "COMPENSATED",
                "idempotent_replay": False,
                "tx_id": payload.get("tx_id"),
                "expected_krw": payload["expected_amount_krw"],
                "actual_krw": payload["actual_settled_krw"],
                "holdback_krw": payload["holdback_krw"],
                "compensated_krw": 22000,
                "uncovered_krw": 18000,
                "credit_delta": -17,
                "trial": {"free": True, "used": used, "limit": 3, "remaining": 3 - used},
                "note": "not written to the audit ledger",
            })

        if self.path == "/v1/quote-lock":
            if not paid:
                challenge = _challenge("50000", self.path)
                header = base64.b64encode(json.dumps(challenge).encode()).decode()
                return self._send(402, challenge, {"PAYMENT-REQUIRED": header})
            if payload["buyer_version"] != payload["supplier_version"]:
                return self._send(409, {"detail": {"error": "VERSION_MISMATCH"}})
            return self._send(200, {
                "status": "QUOTE_LOCKED",
                "quote_hash": "sha256:" + "a" * 64,
                "rule_version": payload["buyer_version"],
                "locked_at": "2026-01-01T00:00:00+00:00",
            })

        if self.path == "/v1/reconcile":
            if not paid:
                challenge = _challenge("250000", self.path)
                header = base64.b64encode(json.dumps(challenge).encode()).decode()
                return self._send(402, challenge, {"PAYMENT-REQUIRED": header})
            return self._send(200, {
                "status": "SETTLED_OK",
                "idempotent_replay": True,
                "tx_id": payload.get("tx_id"),
                "compensated_krw": 0,
                "uncovered_krw": 0,
                "credit_delta": 5,
            })

        return self._send(404, {"detail": "not found"})


@pytest.fixture(scope="session")
def mock_api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockApi)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


# ------------------------------------------------------------------------------
# Minimal MCP stdio client
# ------------------------------------------------------------------------------
class StdioClient:
    def __init__(self, api_base: str):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["RULER_API_BASE"] = api_base
        env.pop("RULER_TRIAL_CLIENT_ID", None)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "reciprocal_ruler_mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env,
        )
        self.lines: "queue.Queue[str]" = queue.Queue()
        self.raw_lines: list = []
        threading.Thread(target=self._pump, daemon=True).start()
        self._next_id = 0

    def _pump(self):
        for line in self.proc.stdout:
            self.raw_lines.append(line)
            self.lines.put(line)
        self.lines.put("")

    def send_raw(self, text: str) -> None:
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()

    def send(self, method: str, params=None, request_id=None, notify=False):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            self._next_id += 1
            message["id"] = self._next_id if request_id is None else request_id
        self.send_raw(json.dumps(message))
        return None if notify else message["id"]

    def read(self, timeout: float = 20.0):
        line = self.lines.get(timeout=timeout)
        assert line != "", "server closed stdout"
        return json.loads(line)

    def request(self, method: str, params=None):
        request_id = self.send(method, params)
        message = self.read()
        assert message["id"] == request_id, message
        return message

    def call_tool(self, name: str, arguments: dict):
        return self.request("tools/call", {"name": name, "arguments": arguments})["result"]

    def initialize(self):
        response = self.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        })
        self.send("notifications/initialized", notify=True)
        return response["result"]

    def close(self):
        try:
            self.proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture
def client(mock_api):
    MockApi.trial_counts.clear()
    api = StdioClient(mock_api)
    api.initialize()
    yield api
    api.close()


RECONCILE_ARGS = {
    "expected_amount_krw": 220000,
    "actual_settled_krw": 180000,
    "holdback_krw": 22000,
}

QUOTE_ARGS = {
    "buyer_version": "1.0",
    "supplier_version": "1.0",
    "buyer_id": "buyer-a",
    "supplier_id": "supplier-b",
    "terms": {"price_krw": 220000, "refund_ratio_on_cancel": 1.0, "holdback_ratio": 0.1},
}


# ------------------------------------------------------------------------------
# Protocol
# ------------------------------------------------------------------------------
def test_initialize_negotiates_version_and_declares_tools(client):
    result = client.request("initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"},
    })["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"]["tools"]["listChanged"] is False
    assert result["serverInfo"]["name"] == "reciprocal-ruler"
    assert "ruler_trial_reconcile" in result["instructions"]


def test_notification_gets_no_reply_and_ping_does(client):
    client.send("notifications/initialized", notify=True)
    ping = client.request("ping")
    assert ping["result"] == {}


def test_tools_list_has_the_three_tools_with_valid_schemas(client):
    tools = client.request("tools/list")["result"]["tools"]
    assert [tool["name"] for tool in tools] == [
        "ruler_trial_verify_math",
        "ruler_verify_math",
        "ruler_trial_reconcile",
        "ruler_quote_lock",
        "ruler_reconcile",
    ]
    for tool in tools:
        assert tool["description"]
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert schema["required"], tool["name"]
        for name in schema["required"]:
            assert name in schema["properties"], (tool["name"], name)
        for prop in schema["properties"].values():
            assert prop.get("type") in {"string", "integer", "number", "object", "array", "boolean"}


def test_trial_reconcile_returns_real_judgment(client):
    result = client.call_tool("ruler_trial_reconcile", RECONCILE_ARGS)
    assert result["isError"] is False
    payload = result["structuredContent"]
    assert payload["result"]["judgment"] == "COMPENSATED"
    assert payload["result"]["compensated_krw"] == 22000
    assert payload["trial_usage"]["remaining"] == 2
    assert "text" in result["content"][0]["type"]


def test_trial_quota_three_then_exhausted(client):
    codes = []
    for index in range(4):
        payload = client.call_tool("ruler_trial_reconcile", dict(RECONCILE_ARGS, trial_client_id="quota-agent"))
        codes.append((payload["isError"], payload["structuredContent"].get("status")))
    assert codes == [(False, None)] * 3 + [(True, "TRIAL_EXHAUSTED")]
    exhausted = client.call_tool("ruler_trial_reconcile", dict(RECONCILE_ARGS, trial_client_id="quota-agent"))
    assert exhausted["structuredContent"]["paid_endpoints"] == [{"path": "/v1/reconcile", "price_usd": "0.25"}]


def test_quote_lock_without_payment_returns_base_usdc_instructions(client):
    result = client.call_tool("ruler_quote_lock", QUOTE_ARGS)
    assert result["isError"] is True
    payload = result["structuredContent"]
    assert payload["status"] == "PAYMENT_REQUIRED"
    summary = payload["payment_summary"]
    assert summary["scheme"] == "exact"
    assert summary["network_name"] == "Base mainnet (eip155:8453)"
    assert summary["asset_name"] == "USDC"
    assert summary["amount_usdc"] == "0.050000"
    assert summary["pay_to"] == PAY_TO
    assert summary["x402_version"] == 2
    assert any("payment_header" in line for line in payload["how_to_pay"])
    assert payload["payment_challenge"]["accepts"][0]["amount"] == "50000"


def test_quote_lock_with_payment_header_succeeds(client):
    result = client.call_tool("ruler_quote_lock", dict(QUOTE_ARGS, payment_header="signed-payload"))
    payload = result["structuredContent"]
    assert payload["ok"] is True
    assert payload["result"]["quote_hash"].startswith("sha256:")
    sent = [call for call in MockApi.calls if call[0] == "/v1/quote-lock"][-1]
    assert sent[2].get("x-payment") == "signed-payload"


def test_quote_lock_version_mismatch_is_reported(client):
    result = client.call_tool("ruler_quote_lock", dict(QUOTE_ARGS, supplier_version="2.0", payment_header="signed"))
    payload = result["structuredContent"]
    assert payload["status"] == "VERSION_MISMATCH"
    assert result["isError"] is True


def test_reconcile_requires_idempotency_key_before_paying(client):
    count_before = len(MockApi.calls)
    result = client.call_tool("ruler_reconcile", RECONCILE_ARGS)
    payload = result["structuredContent"]
    assert result["isError"] is True
    assert payload["status"] == "INVALID_ARGUMENTS"
    assert "idempotency_key" in payload["summary"]
    assert len(MockApi.calls) == count_before, "no request may be sent without an idempotency key"


def test_paid_reconcile_402_then_paid_200(client):
    args = dict(RECONCILE_ARGS, idempotency_key="idem-test-1")
    challenge = client.call_tool("ruler_reconcile", args)["structuredContent"]
    assert challenge["status"] == "PAYMENT_REQUIRED"
    assert challenge["payment_summary"]["amount_usdc"] == "0.250000"
    paid = client.call_tool("ruler_reconcile", dict(args, payment_header="signed-payload"))["structuredContent"]
    assert paid["ok"] is True
    assert paid["result"]["judgment"] == "SETTLED_OK"
    assert paid["result"]["idempotent_replay"] is True


def test_invalid_arguments_do_not_break_the_server(client):
    result = client.call_tool("ruler_trial_reconcile", {"expected_amount_krw": "many"})
    assert result["isError"] is True
    assert result["structuredContent"]["status"] == "INVALID_ARGUMENTS"
    assert client.request("ping")["result"] == {}


def test_unknown_tool_and_unknown_method_are_protocol_errors(client):
    unknown_tool = client.request("tools/call", {"name": "nope", "arguments": {}})
    assert unknown_tool["error"]["code"] == -32602
    unknown_method = client.request("does/not/exist")
    assert unknown_method["error"]["code"] == -32601


def test_parse_error_is_reported_and_stream_survives(client):
    client.send_raw("{not json")
    message = client.read()
    assert message["error"]["code"] == -32700
    assert client.request("ping")["result"] == {}


def test_stdout_carries_only_json_rpc_frames(client):
    client.request("tools/list")
    time.sleep(0.2)
    for line in client.raw_lines:
        json.loads(line)


def test_trial_client_id_comes_from_environment(mock_api):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["RULER_API_BASE"] = mock_api
    env["RULER_TRIAL_CLIENT_ID"] = "env-agent-" + uuid.uuid4().hex[:6]
    proc = subprocess.Popen([sys.executable, "-m", "reciprocal_ruler_mcp"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, env=env)
    try:
        def rpc(method, params=None, request_id=1):
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method,
                                         "params": params or {}}) + "\n")
            proc.stdin.flush()
            return json.loads(proc.stdout.readline())

        rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "pytest", "version": "0"}})
        rpc("tools/call", {"name": "ruler_trial_reconcile", "arguments": RECONCILE_ARGS}, request_id=2)
        headers = [call[2] for call in MockApi.calls if call[0] == "/v1/trial/reconcile"][-1]
        assert headers.get("x-trial-client") == env["RULER_TRIAL_CLIENT_ID"]
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
