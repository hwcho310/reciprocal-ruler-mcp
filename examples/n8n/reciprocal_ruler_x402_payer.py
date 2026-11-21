#!/usr/bin/env python3
"""
Reciprocal Ruler — x402 payer relay
===================================

n8n 의 HTTP Request 노드는 x402 결제 서명(PAYMENT-SIGNATURE)을 만들지 못합니다.
이 릴레이는 그 한 가지만 대신 합니다: 402 를 받으면 Base USDC 로 서명해 자동으로
다시 보내고, 최종 응답(상태코드·본문·PAYMENT-RESPONSE 헤더)을 **그대로** 돌려줍니다.

그래서 n8n 워크플로우에서는 두 노드의 URL 만 바꾸면 됩니다.

    https://reciprocal-ruler-x402.vercel.app/v1/quote-lock
    -> http://<relay-host>:8787/v1/quote-lock

설치
----
    python3 -m venv .venv
    .venv/bin/pip install "x402[evm,requests]"

실행
----
    export EVM_PRIVATE_KEY=0x...        # 결제 전용 지갑의 개인키 (소액만 넣어두세요)
    export RULER_RELAY_TOKEN=...        # (권장) 호출 측이 보낼 Bearer 토큰
    export RULER_MAX_USD_MICRO=300000   # 건당 상한, USDC 6자리 단위 = $0.30
    python3 reciprocal_ruler_x402_payer.py

확인
----
    curl -s http://localhost:8787/health

주의
----
- 핫월렛입니다. USDC 를 조금만 두고, 개인키는 저장소·로그에 남기지 마세요.
- RULER_RELAY_TOKEN 을 설정하면 모든 호출에 `Authorization: Bearer <토큰>` 이 필요합니다
  (n8n HTTP 노드의 Authentication: Generic Credential / Header Auth 로 넣으세요).
- 기본 동작은 업스트림 호스트(reciprocal-ruler-x402.vercel.app)로만 전달합니다.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

UPSTREAM = os.environ.get("RULER_UPSTREAM", "https://reciprocal-ruler-x402.vercel.app").rstrip("/")
ALLOWED_HOST = urlparse(UPSTREAM).netloc
PORT = int(os.environ.get("RULER_RELAY_PORT", "8787"))
TOKEN = os.environ.get("RULER_RELAY_TOKEN", "").strip()
MAX_USD_MICRO = int(os.environ.get("RULER_MAX_USD_MICRO", "300000"))  # $0.30
TIMEOUT = float(os.environ.get("RULER_RELAY_TIMEOUT", "120"))
PROXY_PATHS = ("/v1/quote-lock", "/v1/reconcile")

MAX_BODY = 1_000_000


def _load_sdk():
    from eth_account import Account
    from x402 import x402ClientSync
    from x402.http.clients import x402_requests
    from x402.mechanisms.evm.exact import ExactEvmClientScheme
    from x402.mechanisms.evm.signers import EthAccountSigner

    key = os.environ.get("EVM_PRIVATE_KEY", "").strip()
    if not key:
        sys.exit("EVM_PRIVATE_KEY 가 없습니다. 결제 전용 지갑의 개인키를 환경변수로 넣으세요.")
    account = Account.from_key(key)
    client = x402ClientSync()
    client.register("eip155:8453", ExactEvmClientScheme(EthAccountSigner(account)))
    session = x402_requests(client)
    return account.address, session


def _plain_session():
    import requests

    return requests.Session()


PAYER_ADDRESS, PAID_SESSION = _load_sdk()
PLAIN = _plain_session()


def _required_amount_atomic(headers) -> int | None:
    import base64

    raw = headers.get("PAYMENT-REQUIRED") or headers.get("payment-required")
    if not raw:
        return None
    try:
        data = json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception:
        return None
    accepts = data.get("accepts") or []
    if not accepts:
        return None
    try:
        return int(accepts[0].get("amount"))
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "reciprocal-ruler-relay/1.0"

    def log_message(self, fmt, *args):  # 조용히
        sys.stderr.write("[relay] " + (fmt % args) + "\n")

    def _send(self, status: int, payload: bytes, content_type: str, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type or "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, obj, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", extra_headers)

    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            self._json(200, {
                "ok": True,
                "payer": PAYER_ADDRESS,
                "upstream": UPSTREAM,
                "max_usd_micro": MAX_USD_MICRO,
                "auth_required": bool(TOKEN),
            })
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in PROXY_PATHS:
            self._json(404, {"error": "not_found", "allowed": list(PROXY_PATHS)})
            return

        if TOKEN:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {TOKEN}":
                self._json(401, {"error": "unauthorized"})
                return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._json(400, {"error": "bad_content_length"})
            return
        raw_body = self.rfile.read(length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:
            self._json(400, {"error": "body_must_be_json"})
            return

        url = UPSTREAM + path
        if urlparse(url).netloc != ALLOWED_HOST:
            self._json(400, {"error": "host_not_allowed"})
            return

        # 1) 결제 전 한 번 찔러보고 요구 금액을 확인한다 (아직 과금 없음).
        try:
            probe = PLAIN.post(url, json=payload, timeout=TIMEOUT)
        except Exception as exc:  # 업스트림 접속 실패
            self._json(502, {"error": "upstream_unreachable", "detail": str(exc)[:200]})
            return

        if probe.status_code == 200:
            # 무료 응답이면 그대로 전달
            self._send(200, probe.content, probe.headers.get("Content-Type", "application/json"))
            return

        amount = _required_amount_atomic(probe.headers)
        if probe.status_code == 402 and amount is not None and MAX_USD_MICRO and amount > MAX_USD_MICRO:
            self._json(402, {
                "error": "relay_spend_cap_exceeded",
                "required_atomic": amount,
                "cap_atomic": MAX_USD_MICRO,
            })
            return
        if probe.status_code != 402:
            # 결제와 무관한 오류(409/422/5xx)는 그대로 전달
            self._send(probe.status_code, probe.content, probe.headers.get("Content-Type", "application/json"))
            return

        # 2) 결제 붙여서 실제 호출.
        try:
            paid = PAID_SESSION.post(url, json=payload, timeout=TIMEOUT)
        except Exception as exc:
            self._json(502, {"error": "payment_attempt_failed", "detail": str(exc)[:300]})
            return

        extra = {
            "X-Relay-Payer": PAYER_ADDRESS,
            "X-Relay-Paid": "true" if paid.status_code == 200 else "false",
        }
        receipt = paid.headers.get("PAYMENT-RESPONSE") or paid.headers.get("payment-response")
        if receipt:
            extra["PAYMENT-RESPONSE"] = receipt
        self._send(
            paid.status_code,
            paid.content,
            paid.headers.get("Content-Type", "application/json"),
            extra,
        )


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"relay listening on :{PORT}  payer={PAYER_ADDRESS}  upstream={UPSTREAM}", flush=True)
    print(f"max per call: {MAX_USD_MICRO} micro-USDC  auth: {'on' if TOKEN else 'off'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
