# Architecture

```
Browser (your frontend) --REST/WebSocket--> FastAPI
   FastAPI --> Redis       sessions, auth, OTP challenge (hash), preparations, idempotency, locks, rate limits
   FastAPI --> MongoDB     customers, accounts, transactions, disputes, voice_sessions, conversation_turns, audit_events, handoffs
   voice pipeline: audio --> STT adapter --> orchestrator --> TTS adapter --> audio
   orchestrator: LLM adapter (Ollama | rules) <--> ToolRegistry --> banking services --> repositories
```

## Layers (backend/app)

| Path | Role | Must not |
|---|---|---|
| `api/v1/` | HTTP/WebSocket routes, request validation | contain business rules |
| `voice/gateway.py`, `pipeline.py` | WebSocket event contract; shared STT -> agent -> TTS steps | leak provider formats to the browser |
| `voice/conversation_orchestrator.py` | per-turn loop: state, LLM, tool calls, response guard, persistence | let the model touch storage |
| `voice/transcript_normalizer.py` | deterministic amount/date/yes-no/intent parsing | rely on the LLM for numbers |
| `agents/` | prompt, tool schemas (strict), tool registry, response guard | execute anything outside the allowlist |
| `providers/` | `ollama_llm`, `rules_llm`, `groq_stt`, `deepgram_tts`, `mock_speech`, resilience (retry + circuit breaker) | decide banking outcomes |
| `banking/` | auth (OTP), sessions, transactions, account balances/statement, cards (block), loans, policy engine, disputes, handoffs, seed | trust client-supplied customer ids |
| `repositories/` | all DB access; every customer query takes `customer_id` | expose raw queries to the model |
| `infrastructure/` | Mongo (real or mongomock), Redis (real or fakeredis), metrics helpers | |

## Capabilities

| Customer asks | Tools | Writes? | Gate |
|---|---|---|---|
| balance | `get_account_balance` | no (audited read) | OTP session |
| recent transactions | `get_mini_statement` (all channels, max 5) | no (audited read) | OTP session |
| UPI payment problem | `search_customer_transactions`, `prepare_dispute`, `confirm_dispute` | dispute | policy engine + explicit yes |
| dispute status | `get_dispute_status` | no | ownership |
| lost / stolen / misused card | `list_cards`, `prepare_card_block`, `confirm_card_block` | card to BLOCKED | explicit yes + atomic update |
| card unblock / replacement | `request_human_handoff` | handoff only | human-only by policy |
| loan / EMI | `get_loan_summary` | no (audited read) | OTP session |

Money is integer paise; the backend produces all spoken amounts (Indian digit grouping, e.g. `1,23,456 rupees`).

## Key flows

**Dispute (one customer turn at a time)**
1. STT gives a final transcript. Only a final transcript can trigger the agent.
2. Not authenticated: fixed reply, no tools, no model call. Asked for a human: handoff in code, not by the model.
3. Otherwise the LLM may propose tools (max 4 loop iterations, 2 calls each). Each call is validated by a strict
   Pydantic schema; unknown tools and extra fields become `LLM_INVALID_TOOL_CALL` results fed back to the model.
4. `search_customer_transactions` records which transaction ids the customer owns and has seen. `prepare_dispute`
   only accepts those ids, runs the policy engine, and stores a 5-minute preparation in Redis bound to
   session + customer.
5. `confirm_dispute` runs only if this turn's customer words are an explicit yes and the preparation matches.
   The backend derives the idempotency key (the model's is ignored).
6. `DisputeService.confirm` re-checks auth, ownership and policy, takes a Redis lock, inserts the dispute (unique
   partial index on `{transaction_id, category}` where active is the final duplicate guard), writes an audit event.
7. The reply passes the response guard (every id/amount must come from tool output) before being spoken.

**Card block** follows the same shape: `list_cards` records the card ids the customer owns; `prepare_card_block`
only accepts those ids, stores a 5-minute preparation bound to session + customer and clears any pending dispute;
`confirm_card_block` runs only on an explicit yes and moves the card ACTIVE to BLOCKED in one atomic update, so
concurrent confirmations produce one block and one audit event. Unblocking and replacement have no tool.

**Failure behaviour**: Redis down -> writes fail closed (`STORE_UNAVAILABLE`). STT down/timeout -> typed error, no
banking action. LLM down -> safe message offering a human. TTS down -> text reply still returned.

## Storage notes

- `STORAGE_BACKEND=memory` (default) uses mongomock + fakeredis: no services to install, data resets on restart.
  `mongo` uses real MongoDB/Redis (not exercised by the automated tests, see Known limitations in the README).
- Money is integer paise. Datetimes are UTC.
- Transcripts are stored redacted (phones, long digit runs, emails, spoken secrets). Raw audio is never stored.
  `conversation_turns` expires after `TRANSCRIPT_RETENTION_DAYS` via a TTL index (real MongoDB only).
- Audit events are append-only at the application layer (no update/delete code path).
