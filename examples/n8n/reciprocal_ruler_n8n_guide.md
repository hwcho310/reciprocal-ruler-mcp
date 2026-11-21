# Reciprocal Ruler × n8n — x402 정산 자동화 가이드

AI 에이전트끼리 거래할 때 **"무엇이 입증되면 얼마를 줄지"** 를 미리 고정하고(quote-lock),
실제 정산과 대조해 **부분결제·미지급·환불**을 판정하는(holdback reconcile) 유료 오라클입니다.
이 문서는 n8n에 그대로 import 해서 쓰는 워크플로우와, 막히는 지점(402 결제)을 뚫는 방법을 담았습니다.

| 파일 | 용도 |
|---|---|
| `reciprocal_ruler_n8n_workflow.json` | n8n **Import from File** 용 워크플로우 |
| `reciprocal_ruler_x402_payer.py` | 402 결제를 대신 해주는 릴레이(선택, 권장) |
| `reciprocal_ruler_n8n_guide.md` | 이 문서 |

- 서비스: `https://reciprocal-ruler-x402.vercel.app`
- 요금: `POST /v1/quote-lock` **$0.05**, `POST /v1/reconcile` **$0.25** (USDC, Base 네트워크)
- API 문서(Swagger): `…/docs` · 디스커버리: `…/.well-known/x402`

---

## 1. 5분 설치

1. n8n 우측 상단 **⋯ → Import from File** → `reciprocal_ruler_n8n_workflow.json` 선택
2. `1. Sample Event (edit me)` 노드를 열어 우리 거래 값으로 수정
3. **Manual Trigger** 실행
4. 결과 확인
   - 결제가 붙어 있으면: `6. Classify Result` 에서 `verdict: MATCHED | EXCEPTION | PENDING`
   - 결제가 없으면: `3c. quote-lock 402/Error` 에서 결제 요구 금액과 지갑 주소 확인 → **4장**으로

프로덕션은 **Webhook** 노드로 들어옵니다: `POST /webhook/ruler-settlement` 에 아래 3장의 이벤트 JSON을 밀어 넣으면 됩니다.

---

## 2. 워크플로우 구조

```
Manual Trigger ─┐
                ├→ 2. Normalize & Validate → 3a. POST /v1/quote-lock ($0.05)
Webhook ────────┘                                   │
                                       200 ─────────┴──────── 402/409/422
                                        │                        │
                              4. Attach quote_hash        3c. 402/Error ─┐
                                        │                               │
                          5a. POST /v1/reconcile ($0.25)                │
                                        │                               │
                               200 ─────┴──── 402/422/5xx               │
                                │                    │                  │
                       6. Classify Result      5c. 402/Error ───────────┤
                                │                                       │
                        7. MATCHED? ─── 예외 ───────────────────────────┤
                                │                                       ↓
                       8a. Audit Record                    10. Slack / 10b. Email
                                │                              (자리표시자, 기본 비활성)
                       8b. Audit Log Append
                          (자리표시자, 기본 비활성)
```

| 노드 | 하는 일 |
|---|---|
| `2. Normalize & Validate` | 두 요청 본문 생성, 필수값·규칙 버전 사전 검사(서버 409를 미리 차단) |
| `3a` / `5a` | 실제 유료 호출. `neverError + fullResponse` 라 **402여도 멈추지 않고** 상태코드·헤더를 그대로 넘깁니다 |
| `3b` / `5b` | HTTP 200 이면 계속, 아니면 오류 해석 노드로 분기 |
| `3c` / `5c` | `PAYMENT-REQUIRED` 헤더를 디코드해 **금액·지갑·원인**을 사람이 읽는 문구로 변환 |
| `4` | quote-lock 이 준 `quote_hash` 를 reconcile 본문에 주입, `PAYMENT-RESPONSE`(결제 영수증)도 함께 보관 |
| `6` | 상태 → 판정 변환(`SETTLED_OK`=MATCHED / `COMPENSATED`·`PENDING_RECONCILIATION`=예외) |
| `8a`/`8b` | 일치 건 append-only 감사 로그(탭 구분 한 줄) |
| `10`/`10b` | 예외·결제 실패 알림 자리표시자 |

`8b`(감사 로그 저장)와 `10`/`10b`(알림)는 **기본 비활성**입니다. Google Sheets·n8n Data Table·자체 엔드포인트, Slack·SMTP 중 쓰는 것으로 채우고 켜세요.

---

## 3. 데이터 계약 (서버 스펙 그대로)

### 3.1 `POST /v1/quote-lock` — $0.05

```json
{
  "buyer_version": "1.0",
  "supplier_version": "1.0",
  "buyer_id": "agent_buyer_01",
  "supplier_id": "agent_supplier_42",
  "terms": {
    "price_krw": 220000,
    "currency": "KRW",
    "free_cancel_until": "2026-11-25T18:00:00+09:00",
    "refund_ratio_on_cancel": 1.0,
    "holdback_ratio": 0.10
  }
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `buyer_version` / `supplier_version` | string | 규칙(약관) 버전. **두 값이 달라야 할 이유가 없다면 같게** — 다르면 409 |
| `buyer_id` / `supplier_id` | string | 거래 당사자 식별자(자유 문자열) |
| `terms` | object | 사전 합의 조건. **객체 내용 전체가 해시 대상** — 값이 하나라도 달라지면 다른 해시가 됩니다(키 순서는 정렬되어 해시되므로 무관) |

응답 `200`:

```json
{ "status": "QUOTE_LOCKED", "quote_hash": "sha256:4a7d…", "rule_version": "1.0", "locked_at": "2026-11-21T01:02:44+00:00" }
```

- 해시는 `rule_version + buyer_id + supplier_id + terms` 를 **키 정렬·공백 없이** 직렬화한 SHA-256 입니다. 같은 입력이면 항상 같은 해시 → 나중에 "그때 그 조건"이었음을 증명하는 근거가 됩니다.
- `409` = 규칙 버전 불일치. `detail.error: "VERSION_MISMATCH"`.

### 3.2 `POST /v1/reconcile` — $0.25

```json
{
  "idempotency_key": "idem_tx_20261121_0001",
  "tx_id": "tx_20261121_0001",
  "quote_hash": "sha256:4a7d…",
  "expected_amount_krw": 220000,
  "actual_settled_krw": 180000,
  "holdback_krw": 22000,
  "event_type": "POST_TRADE_REFUND"
}
```

| 필드 | 타입 | 주의 |
|---|---|---|
| `idempotency_key` | string | **재시도해도 같은 값**을 쓰세요. 같은 키로 다시 부르면 새 판정 없이 이전 결과가 `idempotent_replay: true` 로 재생됩니다 |
| `tx_id` | string | 거래 식별자 |
| `quote_hash` | string | quote-lock 이 돌려준 값을 그대로. 원장에 기록됩니다(서버가 레지스트리를 조회해 검증하지는 않으므로 **직접 정확히 넣어야** 합니다) |
| `expected_amount_krw` | int | 합의 금액(KRW 정수) |
| `actual_settled_krw` | int | 실제 정산된 금액 |
| `holdback_krw` | int | 유보금. **생략하면 22,000 이 들어갑니다** — 항상 명시하세요 |
| `event_type` | enum | `POST_TRADE_REFUND`(거래 후 환불·부분결제) 또는 `HALF_OPEN_TIMEOUT`(응답 없이 남은 건) |

응답 `200`:

```json
{
  "status": "COMPENSATED",
  "idempotent_replay": false,
  "tx_id": "tx_20261121_0001",
  "compensated_krw": 22000,
  "uncovered_krw": 18000,
  "credit_delta": -17
}
```

판정 규칙(`부족액 = expected − actual`):

| 조건 | `status` | `compensated_krw` | `uncovered_krw` | `credit_delta` |
|---|---|---|---|---|
| 부족액 ≤ 0 | `SETTLED_OK` | 0 | 0 | `+5` |
| 부족액 > 0, `POST_TRADE_REFUND` | `COMPENSATED` | `min(holdback, 부족액)` | 남은 부족액 | 미보전 있으면 `-17`, 없으면 `-5` |
| 부족액 > 0, `HALF_OPEN_TIMEOUT` | `PENDING_RECONCILIATION` | 0 | 부족액 전체 | `0` |

`credit_delta` 는 상대 신용 점수의 변화량(참고값)입니다. 워크플로우의 판정 매핑은 `SETTLED_OK` 만 감사 로그로 보내고, 나머지는 알림으로 보냅니다 — 유보금으로 다 메꿔진 `COMPENSATED` 도 "돈이 덜 들어온 건"이므로 알림이 맞습니다.

---

## 4. 결제(402) 붙이기 — 여기가 유일한 관문

### 4.1 무슨 일이 일어나는가

이 API는 **x402** 유료 엔드포인트입니다. 결제 없이 호출하면 이렇게 돌아옵니다(실측).

```
HTTP/2 402
payment-required: eyJ4NDAyVmVyc2lvbiI6MiwiZXJyb3IiOiJQYXltZW50IHJlcXVpcmVkIiwi…
content-type: application/json

{"error":"Payment required"}
```

`payment-required` 헤더는 **base64 로 감싼 JSON** 입니다. 풀면:

```json
{
  "x402Version": 2,
  "error": "Payment required",
  "resource": { "url": "https://reciprocal-ruler-x402.vercel.app/v1/quote-lock", … },
  "accepts": [{
    "scheme": "exact",
    "network": "eip155:8453",
    "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "amount": "50000",
    "payTo": "0xBfC0FE4fE6f807E204e6878cD17dd743d771eE1a",
    "maxTimeoutSeconds": 300,
    "extra": { "name": "USD Coin", "version": "2" }
  }]
}
```

- `amount` 는 **USDC 6자리 원자 단위**: `50000` = $0.05, `250000` = $0.25
- 결제 방식은 USDC(EIP-3009) `transferWithAuthorization` 서명 → **`PAYMENT-SIGNATURE` 헤더**(base64 JSON)에 담아 같은 요청을 다시 보냅니다. 결제가 성사되면 응답에 `PAYMENT-RESPONSE`(payer, tx hash, network)가 붙습니다.
- 구버전 호환 헤더는 `X-PAYMENT` 입니다. 이 서버는 v2(`PAYMENT-SIGNATURE`)를 씁니다.
- **결제 미들웨어가 라우트보다 앞**에 있습니다. 즉 미결제 상태에서는 본문이 틀렸든 규칙 버전이 안 맞든 **무조건 402** 입니다 — 바디 오류(409/422)는 결제 후에야 볼 수 있으니, 디버깅은 `2. Normalize & Validate` 의 사전 검사로 하세요.

> n8n 기본 **HTTP Request 노드는 이 서명을 만들지 못합니다.** 그래서 아래 세 가지 중 하나가 필요합니다.

### 4.2 방법 A — 결제 릴레이 (권장, 코드 수정 없음)

`reciprocal_ruler_x402_payer.py` 가 402 → 서명 → 재요청을 대신하고, **최종 응답을 그대로** 돌려줍니다. 워크플로우는 URL만 바꾸면 끝입니다.

```bash
python3 -m venv .venv
.venv/bin/pip install "x402[evm,requests]"

export EVM_PRIVATE_KEY=0x…        # 결제 전용 지갑 개인키 (소액만)
export RULER_RELAY_TOKEN=…        # 권장: 호출 측 인증 토큰
export RULER_MAX_USD_MICRO=300000 # 건당 상한 $0.30 (초과 시 결제 거부)
python3 reciprocal_ruler_x402_payer.py
# → relay listening on :8787
```

n8n에서 바꿀 곳은 **두 노드의 URL** 뿐입니다.

| 노드 | 기존 | 변경 |
|---|---|---|
| `3a` | `https://reciprocal-ruler-x402.vercel.app/v1/quote-lock` | `http://<릴레이주소>:8787/v1/quote-lock` |
| `5a` | `https://reciprocal-ruler-x402.vercel.app/v1/reconcile` | `http://<릴레이주소>:8787/v1/reconcile` |

- 상태코드·본문·`PAYMENT-RESPONSE` 헤더가 그대로 흘러오므로 `3b`/`5b` 분기와 `6` 판정 로직은 손대지 않습니다.
- 토큰을 켰다면 두 노드에 Header Auth 자격증명(`Authorization: Bearer <토큰>`)을 붙이세요.
- **n8n Cloud** 는 사내 localhost에 닿지 못합니다. Render·Fly·사내 서버 등 **릴레이가 공개 접근 가능한 곳**에 있어야 합니다(개인키는 그 서버의 환경변수로만).

릴레이 동작 확인:

```bash
curl -s http://localhost:8787/health
# {"ok": true, "payer": "0x…", "upstream": "https://reciprocal-ruler-x402.vercel.app", …}
```

### 4.3 방법 B — 직접 서명하기 (파이썬, 검증된 코드)

릴레이를 두기 싫다면 결제 지점에서 직접 서명하세요. 아래 코드는 이 API에 실제로 요청을 보내 **402 → 서명 → 재전송**까지 동작을 확인한 최소본입니다(잔액 0 지갑이라 `insufficient_funds` 가 돌아왔고, USDC가 있으면 그대로 통과합니다).

```python
import os, requests
from eth_account import Account
from x402 import x402ClientSync
from x402.mechanisms.evm.exact import ExactEvmClientScheme
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.http.clients import x402_requests

account = Account.from_key(os.environ["EVM_PRIVATE_KEY"])
client = x402ClientSync()
client.register("eip155:8453", ExactEvmClientScheme(EthAccountSigner(account)))
session = x402_requests(client)   # 402 → 서명 → 재요청 자동

r = session.post(
    "https://reciprocal-ruler-x402.vercel.app/v1/quote-lock",
    json={"buyer_version": "1.0", "supplier_version": "1.0",
          "buyer_id": "agent_buyer_01", "supplier_id": "agent_supplier_42",
          "terms": {"price_krw": 220000, "holdback_ratio": 0.1}},
    timeout=120,
)
print(r.status_code, r.text)
print(r.headers.get("PAYMENT-RESPONSE"))   # 성공 시 결제 영수증(base64)
```

TypeScript라면 같은 일을 `@x402/fetch`(x402 v2 SDK, `@x402/core` 기반)로 합니다. 이 서버도 v2 프로토콜(`PAYMENT-SIGNATURE` / `PAYMENT-REQUIRED`)이라 같은 계열입니다. 구 v1 패키지(`x402-fetch` 1.x, `x402-axios` 1.x)는 헤더 규약이 달라 v2 서버에는 쓰지 마세요. Coinbase 계열 클라이언트는 `@coinbase/x402`(v2)로 배포되어 있습니다. 어느 쪽이든 **프로토콜은 동일**합니다: 402 → `accepts[0]` 선택 → EIP-3009 서명 → `PAYMENT-SIGNATURE`(base64) → 같은 요청 재전송.

self-hosted n8n이면 `NODE_FUNCTION_ALLOW_EXTERNAL` 로 `@x402/fetch` 를 허용해 **Code 노드 안에서 직접 결제**할 수도 있습니다. Cloud에서는 외부 모듈을 못 쓰므로 4.2 릴레이를 쓰세요.

### 4.4 방법 C — 이미 쓰는 결제 경로로 교체

x402 결제를 대신 처리해 주는 지갑 API(예: 1Shot 같은 서비스)나 n8n 커뮤니티의 x402 노드를 이미 쓰고 있다면, 그 노드로 `3a`/`5a`만 바꾸면 됩니다. **경로·본문은 그대로**이고, 결제 계층만 교체되는 구조입니다. 어떤 경로든 요청 본문/응답 해석은 3장 계약을 따릅니다.

### 4.5 지갑 준비 체크리스트

1. **Base 네트워크**를 지원하는 EVM 지갑(개인키를 코드/서버에서 쓸 수 있는 것)
2. Base 체인의 **USDC** 입금 — 가스는 facilitator가 대납하므로 ETH는 보통 불필요
3. 건당 $0.05~$0.25 이므로 **소액만** 넣고, 릴레이에는 `RULER_MAX_USD_MICRO` 상한을 걸기
4. 개인키는 환경변수/시크릿 매니저에만. 워크플로우 JSON·로그·채팅에 붙여넣지 않기

---

## 5. 감사 로그와 알림 붙이기

| 대상 | 노드 | 방법 |
|---|---|---|
| 정상 일치 로그 | `8b. Audit Log Append` | (a) **Google Sheets** 노드로 교체 — `audit_line` 을 탭 구분으로 분해하거나 컬럼별 매핑, (b) **Data Table** 노드에 append, (c) 자체 엔드포인트 URL 로 POST |
| 예외·결제 실패 알림 | `10. Slack Alert` | 채널 선택 + Slack 자격증명 → 활성화 |
| 위와 동일 | `10b. Email Alert` | SMTP 자격증명, `toEmail` 수정 → 활성화 |

두 알림 노드는 `{{ $json.alert_text }}` 하나만 씁니다. `3c`·`5c`·`6` 이 모두 같은 필드를 만들기 때문에, 알림 받는 곳이 하나면 충분합니다.

---

## 6. 실측 검증 기록 (2026-11-21)

| 확인 항목 | 결과 |
|---|---|
| `GET /` · `GET /health` | 200, `network: eip155:8453`, `pay_to_configured: true` |
| `POST /v1/quote-lock` 미결제 | **402** + `PAYMENT-REQUIRED`(amount `50000`, USDC/Base, payTo 기재) |
| `POST /v1/reconcile` 미결제 | **402** + amount `250000` |
| 본문이 틀린 요청(버전 불일치) 미결제 | **402** — 결제가 검증보다 먼저 |
| 서명은 했으나 잔액 0인 지갑 | **402** `{"error":"insufficient_funds"}` (서명 형식은 접수됨) |
| 결제 릴레이 `/health` | 200 |
| 릴레이 경유 quote-lock | 402 `insufficient_funds` 를 그대로 전달, `X-Relay-Paid: false` |
| 릴레이 상한(`MAX=1`) | 402 `relay_spend_cap_exceeded` (`required_atomic: 250000`) |
| 워크플로우 JSON | `python3 -m json.tool` 통과, 노드 20개, 모든 연결 참조 유효, JS 코드 문법 검사 통과 |

**검증하지 못한 것:** 잔액이 $0 인 지갑뿐이라 **실제 결제 성사 후 200 응답**과 `PAYMENT-RESPONSE` 영수증은 아직 실측하지 못했습니다. USDC를 넣은 뒤 위 4.3 코드로 한 번 호출하면 확인됩니다(이때 `insufficient_funds` 가 사라지고 `QUOTE_LOCKED` 가 나옵니다).

---

## 7. 운영 팁 / 함정

- **본문은 결제 전에 검증**: 이 서버는 결제가 검증보다 앞이라, 본문이 틀려도 402가 먼저 나옵니다. 지갑이 채워진 상태에서 잘못된 본문을 보내면 **돈만 나가고 422**가 올 수 있으니, 반드시 `2. Normalize & Validate` 같은 사전 검사를 거쳐 호출하세요.
- **콜드 스타트**: 무료 호스팅이라 첫 호출이 30~60초 걸릴 수 있습니다. 워크플로우 HTTP 노드 timeout은 90초로 잡아 두었고, 재시도 2회가 걸려 있습니다. Cron으로 5분마다 `/health` 를 때리면 콜드 스타트를 줄일 수 있습니다.
- **재시도는 같은 `idempotency_key`**: 네트워크 오류 후 다시 부르면 `idempotent_replay: true` 로 이전 판정이 재생됩니다. 중복 정산이 생기지 않습니다.
- **`holdback_krw` 생략 금지** — 생략하면 22,000 이 들어갑니다.
- **`quote_hash` 는 quote-lock 응답값 그대로**. 생성 규칙을 직접 재현할 필요 없고, 재현하면 오히려 틀리기 쉽습니다.
- **금액은 정수 KRW**. 소수부가 있으면 거절되니 `220000` 처럼 정수로 보내세요.
- **`terms` 는 계약 그 자체**: 해시 대상이므로 같은 거래에서 값을 바꾸면 다른 견적이 됩니다. 바꿔야 하면 새로 quote-lock 하세요.
- **개인정보**: `buyer_id`/`supplier_id`/`terms` 에 고객 실명·연락처를 넣지 말고 내부 식별자(UUID)를 쓰세요. 서버 원장에 기록됩니다.

---

## 8. 문제 해결

| 증상 | 원인 | 조치 |
|---|---|---|
| 402 + `Payment required` | 결제 헤더 없음 | 4.2 릴레이 경유 또는 4.3 직접 서명 |
| 402 + `insufficient_funds` | 지갑 USDC 부족 | Base 체인 USDC 입금 |
| 402 + `relay_spend_cap_exceeded` | 릴레이 상한 초과 | `RULER_MAX_USD_MICRO` 조정 |
| 409 `VERSION_MISMATCH` | 규칙 버전 불일치 | 두 에이전트 버전 합의 후 재호출(과금됨 — 사전에 `2. Normalize` 가 먼저 막아줍니다) |
| 422 | 본문 형식 오류 | 필드명, int 타입, enum 값 확인 |
| 5xx / 타임아웃 | 콜드 스타트·일시 장애 | 같은 `idempotency_key` 로 재시도 |
| 노드가 빨간색인데 응답은 옴 | `neverError` 미설정 | 본 템플릿은 설정돼 있음. 커스터마이즈 시 유지 |

---

## 9. English quick start

**What it is.** Reciprocal Ruler is a paid x402 oracle for agent-to-agent settlement:
`POST /v1/quote-lock` ($0.05, USDC on Base) freezes the agreed terms into a deterministic
`sha256:` hash; `POST /v1/reconcile` ($0.25) compares expected vs. actually settled amounts and
returns `SETTLED_OK` / `COMPENSATED` / `PENDING_RECONCILIATION` plus holdback compensation,
uncovered amount and a credit delta. Reconcile is idempotent on `idempotency_key`.

**Import.** n8n → ⋯ → *Import from File* → `reciprocal_ruler_n8n_workflow.json`. Edit the
`1. Sample Event` node, run the Manual Trigger. The webhook entry is `POST /webhook/ruler-settlement`.

**Payments.** n8n's HTTP Request node cannot sign x402 payments. Either
(a) run `reciprocal_ruler_x402_payer.py` and point the two HTTP nodes at
`http://<relay>:8787/v1/quote-lock` and `/v1/reconcile` (it 402 → signs → retries and passes the
response through unchanged), or (b) sign yourself:

```python
from eth_account import Account
from x402 import x402ClientSync
from x402.mechanisms.evm.exact import ExactEvmClientScheme
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.http.clients import x402_requests

client = x402ClientSync()
client.register("eip155:8453", ExactEvmClientScheme(EthAccountSigner(Account.from_key(PRIVATE_KEY))))
session = x402_requests(client)          # handles the 402 → PAYMENT-SIGNATURE retry
r = session.post("https://reciprocal-ruler-x402.vercel.app/v1/quote-lock", json=payload)
```

**Protocol facts.** 402 carries a base64 JSON `PAYMENT-REQUIRED` header
(`accepts[0]`: scheme `exact`, network `eip155:8453`, asset USDC
`0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`, `amount` in 6-decimal units). Pay by attaching
base64 `PAYMENT-SIGNATURE` (x402 v2; legacy `X-PAYMENT`) and replaying the request. Success adds a
base64 `PAYMENT-RESPONSE` (payer, tx hash, network). The paywall sits in front of validation, so
body errors (409/422) are only visible after payment — validate locally first.

**Gotchas.** `holdback_krw` defaults to 22000 if omitted. `quote_hash` must be the value returned
by quote-lock (it is recorded, not re-validated). Amounts are integer KRW. The free hosting tier may
cold-start for ~30–60s, so keep client timeouts ≥ 90s and reuse the same `idempotency_key` on retry.
