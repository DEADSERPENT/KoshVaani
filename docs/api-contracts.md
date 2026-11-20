# KoshVaani API contracts (for the frontend)

Base URL `http://127.0.0.1:8000`. Interactive reference: `/docs` (OpenAPI). All REST routes are under `/api/v1`.
Everything is JSON unless noted. Money is **integer paise** (`425000` = ₹4,250). Timestamps are UTC ISO-8601.

## Conventions

- **Session token**: `Authorization: Bearer <session_token>` on every customer route. The server derives the
  customer from the session; never send a customer id except in the login challenge.
- **Errors** use one envelope; branch on `error.code`, show `error.message`:
  ```json
  {"error": {"code": "AUTHENTICATION_REQUIRED", "message": "...", "request_id": "req-...", "retryable": false}}
  ```
  | code | HTTP | meaning |
  |---|---|---|
  | VALIDATION_ERROR | 422 | bad body/field/audio |
  | SESSION_EXPIRED | 401 | missing/expired token: start a new session |
  | AUTHENTICATION_REQUIRED | 401 | session not verified yet |
  | AUTHENTICATION_FAILED | 401 | wrong/expired OTP; after 3 misses the challenge is burned and `error.details.handoff_recommended` is `true` |
  | CUSTOMER_NOT_FOUND / TRANSACTION_NOT_FOUND / DISPUTE_NOT_FOUND | 404 | unknown **or not yours** (indistinguishable by design) |
  | DISPUTE_NOT_ELIGIBLE | 409 | policy says no |
  | CONFIRMATION_REQUIRED | 409 | declined, expired or missing confirmation |
  | IDEMPOTENCY_CONFLICT | 409 | key reused for a different request, or a write is in progress |
  | POLICY_REJECTED / FORBIDDEN | 403 | |
  | RATE_LIMITED | 429 | retryable |
  | PROVIDER_TIMEOUT / PROVIDER_UNAVAILABLE | 504 / 503 | retryable; no banking action was taken |
  | STORE_UNAVAILABLE | 503 | Redis down: writes fail closed |
- Some errors add `error.details` with safe hints: `DISPUTE_NOT_ELIGIBLE` gives `{reason_code, existing_dispute_id|null}`,
  `POLICY_REJECTED` gives `{reason_code}`, `AUTHENTICATION_FAILED` gives `{handoff_recommended}`.
- Every response carries `X-Request-ID`.

## Session and login (separate from speech: the OTP is never spoken)

| Call | Body, then response |
|---|---|
| `POST /sessions` | `{channel?, language?, consent_microphone?, consent_notice?}`. `consent_notice` must be true (403 `POLICY_REJECTED` otherwise). Voice routes and WebSocket `audio.start` need `consent_microphone:true` for the session (403 `POLICY_REJECTED`, `reason_code: MIC_CONSENT_MISSING`); typed turns do not. returns `{session_id, session_token, status, language, simulated:true}` (201) |
| `GET /sessions/{id}` | returns `{session_id, status, outcome, authenticated, language}` (own session only) |
| `POST /sessions/{id}/end` | returns `{session_id, status:"COMPLETED"}`; clears auth (use for End Call / logout) |
| `POST /auth/challenges` | `{phone:"9900000101"}` (10-digit Indian mobile; `+91`, `91`, `0` prefixes, spaces and dashes accepted; malformed gives 422) returns `{challenge_id, delivery_mode, expires_in_seconds, message, dev_code}`. The response is identical whether or not the number is registered (no customer enumeration); an unregistered number gets a challenge that can never verify, and `dev_code` is null. |
| `POST /auth/challenges/{challenge_id}/verify` | `{code:"123456"}` returns `{authenticated, customer_display_name, phone_masked}` |

**Sign-in depends on `AUTH_MODE`.** In the default `password` mode use `POST /auth/login` `{phone, password}` (returns `{authenticated, customer_display_name, phone_masked, must_change_password}`; every failure gives the same 401, and 5 failures lock the number for 15 minutes with 429) and `POST /auth/password` `{current_password, new_password}` (minimum 10 characters, not only digits, not containing the phone number). A customer signed in with a temporary password has `must_change_password: true` and `authenticated: false`: nothing opens until the password is changed. The challenge endpoints below answer 403 in this mode, and `/auth/login` answers 403 in `otp` mode.

`POST /auth/challenges` (otp mode) is limited per phone number (default 5 per 15 minutes) and per client address (default 30), counted identically for registered and unregistered numbers; over the limit gives 429 `RATE_LIMITED`. `delivery_mode` is `SMS`: the code is always sent through the configured gateway and the app will not start without one. `dev_code` is always null; it is returned only by a server started with `APP_ENV=test` (automated tests), never in a real run.

Seeded customers (log in by phone): `9900000101` Aarav (one eligible UPI debit), `9900000102` Diya (two same-day ₹1,500 matches),
`9900000103` Kabir (successful payment, not disputable), `9900000104` Meera (existing dispute `KV-20261110-000001`).

## Conversation (text) and REST voice

- `POST /conversation/turn` `{text}` returns a turn result (below). Use it as the typed fallback.
- `POST /voice/transcribe` multipart `file` returns `{text, language, provider, is_final, confidence|null, request_id}`
- `POST /voice/synthesize` `{text}` returns audio bytes (`Content-Type` from the provider; header `X-Provider`)
- `POST /voice/turn` multipart `file` (one utterance; `audio/webm|ogg|wav|mpeg|mp4|flac`) returns a turn result plus
  `transcript`, `audio:{base64, content_type, error}`, `providers:{stt,tts}`, `latency_ms:{asr,tts,total}`.
  If `audio.error` is set the text reply is still valid: show it.

**Turn result**
```json
{"turn_index":0,"reply":"I found a UPI debit of 4,250 rupees on 11 November. ...","intent":"UPI_PAYMENT_ISSUE",
 "state":"READY_TO_CONFIRM","entities":{"amount_paise":425000,"date":"2026-11-11","dispute_reference":null},
 "tool_trace":[{"tool":"search_customer_transactions","ok":true,"error_code":null}],
 "preparation_id":"PREP-...","pending_action":"DISPUTE","dispute_id":null,"blocked_card_id":null,"handoff_id":null,
 "llm":{"provider":"rules","model":"rules-v1"},"latency_ms":8}
```
`state` is one of `LISTENING | NEEDS_AUTH | NEEDS_CLARIFICATION | READY_TO_CONFIRM | HANDOFF`.
`preparation_id` is set only while an action awaits confirmation, and `pending_action` says which: `DISPUTE` or
`CARD_BLOCK` (null otherwise). Only one action is pending at a time; starting a second one replaces the first.
`dispute_id` / `blocked_card_id` are set once the action was done. Show `intent`, `entities` and `tool_trace` in a
developer panel, not the customer view.

`intent` is one of `BALANCE_ENQUIRY, MINI_STATEMENT, CARD_BLOCK, CARD_STATUS, CARD_UNBLOCK, LOAN_ENQUIRY,
UPI_PAYMENT_ISSUE, TRANSACTION_STATUS, DISPUTE_TRACKING, HUMAN_HANDOFF, RESTRICTED_REQUEST, UNKNOWN`. `CARD_UNBLOCK` (unblock or
replacement) and `RESTRICTED_REQUEST` (money transfer, limit change, account closure, fee waiver or prepayment) always
become a human handoff by policy. A UI that veils balances should veil the amounts in the reply text of a `BALANCE_ENQUIRY` turn too.

## Direct banking routes (cards, tables)

| Call | Notes |
|---|---|
| `GET /transactions?amount_paise=&date_from=&date_to=&transaction_type=UPI&limit=` | `{transactions[], count, ambiguous}`, newest first; `utr_masked` only. `limit` 1-20 (use `limit=5` for a mini statement). `channel` is `UPI, NEFT, IMPS, ATM` or `CARD` |
| `GET /accounts` | `[{account_id, account_type, account_number_masked, currency, status, balance_paise, available_paise, balance_text, available_text}]`. `available` is below `balance` while a card authorisation is on hold |
| `GET /cards` | `[{card_id, card_type: DEBIT or CREDIT, network, last4, status: ACTIVE, BLOCKED or EXPIRED, blocked_reason}]` |
| `POST /cards/block/prepare` `{card_id, reason: LOST, STOLEN, SUSPECTED_FRAUD or OTHER}` | returns `{preparation_id, card, confirmation_text, expires_in_seconds}`. Blocks nothing. 404 `CARD_NOT_FOUND` (also for another customer's card); 409 `CARD_NOT_ELIGIBLE` if already blocked |
| `POST /cards/block/confirm` `{preparation_id, customer_confirmation}` | returns `{blocked, card, message}`; `blocked:false` means it was already blocked. The preparation is single-use and bound to the session; `customer_confirmation:false` gives 409 `CONFIRMATION_REQUIRED` and blocks nothing |
| `GET /loans` | `[{loan_id, loan_type, status, outstanding_paise, emi_paise, next_due_date, emis_remaining, outstanding_text, emi_text}]` (read-only) |
| `GET /transactions/{id}` | one owned transaction |
| `POST /disputes/prepare` `{transaction_id, category, customer_explanation?}` | returns `{preparation_id, eligible, reason_code, transaction_summary, confirmation_text, expires_in_seconds}`; 409 `DISPUTE_NOT_ELIGIBLE` includes `error.details.existing_dispute_id` when a case already exists |
| `POST /disputes/confirm` `{preparation_id, customer_confirmation, idempotency_key}` | returns `{created, dispute_id, status, message}`. Retrying the same key returns the same dispute with `created:false`. Generate a random `idempotency_key` (8+ chars) per click |
| `GET /disputes/{dispute_id}` | returns `{dispute_id, transaction_id, category, status, priority, policy_id, timeline_message, ...}` |
| `POST /handoffs` `{reason, summary?}` | returns `{handoff_id, status:"QUEUED"}` |

There is no REST or voice endpoint to unblock a card: that is a human-agent action (`POST /handoffs`).

Categories: `UPI_P2P_BENEFICIARY_NOT_CREDITED, UPI_MERCHANT_PAYMENT_PENDING, UPI_DEBIT_WITH_FAILED_STATUS,
UPI_UNKNOWN_TRANSACTION, OTHER_UPI_ISSUE`. Transaction statuses: `INITIATED, PENDING, SUCCESS, FAILED, REVERSED,
DEBITED_CREDIT_UNCONFIRMED, UNKNOWN`. Dispute statuses: `DRAFT, OPEN, UNDER_REVIEW, WAITING_FOR_BANK_RAILS,
RESOLVED, REJECTED, CLOSED`.

## Voice WebSocket

`POST /api/v1/voice/ws-ticket` (bearer session token, needs microphone consent) returns `{ticket, expires_in_seconds}`.
Then connect to `ws://127.0.0.1:8000/api/v1/voice/sessions/{session_id}/stream?ticket=<ticket>`.

Browsers cannot set headers on WebSockets, so the stream is opened with a **30-second, single-use ticket**; the long-lived
session token never appears in a URL or proxy log. A missing, used, expired or wrong-session ticket (and the session token
itself) closes the socket with code **4401** before any event. Idle for more than 120 s gives `error SESSION_EXPIRED`,
then close.

**Client to server** (JSON text frames unless noted)
```json
{"type":"audio.start","content_type":"audio/webm"}
<binary frames, each 64 KB or less, total within MAX_AUDIO_BYTES>
{"type":"audio.end"}
{"type":"text.input","text":"..."}
{"type":"client.confirmation","value":true,"preparation_id":"PREP-..."}
{"type":"ping"}   {"type":"session.start"}   {"type":"session.stop"}
```
`audio.end` triggers STT, then the agent, then TTS. `text.input` is the typed fallback (when `ALLOW_TEXT_FALLBACK`).
Audio is uploaded as a whole utterance: there is no streaming or partial STT yet, so `transcript.partial` is never
sent. Only the final transcript triggers the agent.

**Server to client**
```json
{"type":"session.ready","session_id":"VS-...","authenticated":true}
{"type":"assistant.state","state":"LISTENING|TRANSCRIBING|UNDERSTANDING|SPEAKING|HANDOFF"}
{"type":"transcript.final","text":"...","provider":"groq_whisper"}
{"type":"tool.result","tool":"prepare_dispute","status":"success|error","error_code":null}
{"type":"assistant.message","text":"...","intent":"...","state":"READY_TO_CONFIRM","entities":{},"preparation_id":"PREP-...","pending_action":"DISPUTE|CARD_BLOCK|null"}
{"type":"case.created","dispute_id":"KV-20261112-000001"}
{"type":"card.blocked","card_id":"CRD-1001-D"}
{"type":"handoff.created","handoff_id":"HO-..."}
{"type":"audio.chunk","content_type":"audio/mpeg","seq":0,"final":true,"data":"<base64>"}
{"type":"turn.completed","latency_ms":{"asr":640,"tts":510,"total":1608}}
{"type":"error","code":"PROVIDER_TIMEOUT","retryable":true,"message":"..."}
{"type":"pong"}   {"type":"session.completed","outcome":"DISPUTE_CREATED"}
```
`outcome` is one of `DISPUTE_CREATED, CARD_BLOCKED, INFORMATION_PROVIDED, HANDOFF, ABANDONED`.
Order within a turn: `transcript.final`, `tool.result` (0 or more), `assistant.message`, then `case.created`, `card.blocked` or
`handoff.created` if applicable, `audio.chunk` (1 or more), `turn.completed`. Concatenate chunks in `seq` order.
If TTS fails you get `assistant.message` followed by `error` and no audio: render the text.

When `assistant.message.state` is `READY_TO_CONFIRM`, `preparation_id` is set. Use it for Yes/No buttons via
`client.confirmation` (works for both disputes and card blocks); it expires after 5 minutes. Without a pending preparation the server answers
`error CONFIRMATION_REQUIRED`. A spoken or typed "yes" takes the same path. `tool.result` is emitted after the turn
runs, not live while tools execute.

## Operations API (operator sign-in)

Operators are named accounts (create with `python scripts/create_operator.py`). Sign in, then send
`Authorization: Bearer <token>`. A customer session token is **not** accepted. Roles, lowest to highest:
`VIEWER` (overview, session list, metrics) < `AGENT` (adds session detail and the handoff queue) < `ADMIN`.
Missing/expired token gives 401 `AUTHENTICATION_REQUIRED`; too low a role gives 403 `FORBIDDEN`.

| Call | Notes |
|---|---|
| `POST /ops/auth/login` `{username, password}` | returns `{token, operator_id, username, role, expires_in_seconds}`. Wrong username and wrong password give the same 401. 5 failures lock the username for 15 minutes (429). |
| `POST /ops/auth/logout` | revokes the token immediately |
| `GET /ops/auth/me` | `{operator_id, username, role}` |

| Call | Returns |
|---|---|
| `GET /ops/overview?date=YYYY-MM-DD` | `sessions, disputes_created, cards_blocked, resolved_by_self_service (disputes + card blocks + information-only sessions), human_handoffs, provider_errors, blocked_policy_attempts, ambiguous_matches, latency_ms{p50,p90,p95,samples,stage_avg}, sessions_by_outcome, handoff_reasons, intents, providers` (defaults to today, UTC) |
| `GET /ops/sessions?outcome=&status=&date=&limit=` | `{sessions[], count}`, each with `turn_count` |
| `GET /ops/sessions/{id}` | `session, turns[] (redacted), tool_timeline[], latency_by_turn[], matched_transactions[], disputes[], handoffs[], audit[]` (viewing is audited) |
| `GET /ops/handoffs?status=QUEUED` | `{handoffs[], count}` |
| `POST /ops/handoffs/{id}/status` `{status:"IN_PROGRESS"\|"RESOLVED"}` | updates the queue entry |
| `GET /ops/metrics` | Prometheus text |
| `POST /ops/customers/{customer_id}/erase` | ADMIN only. Removes personal data and call content, keeps banking records; returns `{customer_id, sessions, turns_deleted}`. See `docs/privacy.md`. |

Public: `GET /healthz`, `GET /readyz` (`{ready, mongodb, redis, providers:{llm,stt,tts}}`).
Operator actions that read customer conversations or change a handoff are written to the audit log with the operator id.
