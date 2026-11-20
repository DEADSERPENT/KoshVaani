# Threat model

Scope: the KoshVaani voice service desk. This is a design record, not a certification.

| # | Threat | Control | Where tested |
|---|---|---|---|
| 1 | Reading another customer's data by guessing ids | `customer_id` comes from the session; every repository query filters on it; unknown and not-yours return the same 404 | `test_banking_flow` (cross-customer, tracking) |
| 2 | Unauthenticated access | Auth state lives in Redis, set only by OTP verification; routes depend on it | `test_unauthenticated_transaction_query_rejected`, WS/agent unauth tests |
| 3 | OTP guessing | 3 attempts then the challenge is burned; code stored only as salted hash; never logged | `test_wrong_otp_does_not_authenticate_and_locks_out` |
| 4 | Prompt injection in speech ("ignore the rules...") | Customer text is data; tools enforce auth/ownership/policy regardless of model output | `test_model_cannot_use_invented_transaction_id` |
| 5 | Model calls unknown tools / passes extra fields | Fixed allowlist, `extra=forbid` schemas | `test_unregistered_tool_and_extra_fields_rejected` |
| 6 | Model invents ids | Transaction ids must come from this conversation's search results | same as 4 |
| 7 | Model confirms a dispute on its own | `confirm_dispute` needs this turn's customer words to be an explicit yes plus a matching preparation | `test_model_cannot_self_confirm` |
| 8 | Hallucinated references/amounts, PIN requests, refund claims | Response guard replaces ungrounded replies | `test_hallucinated_reference_replaced_by_safe_fallback`, normalizer guard tests |
| 9 | Duplicate disputes via retry or race | Idempotency key, Redis lock, unique partial index | `test_dispute_created_once_with_idempotency` |
| 10 | Redis outage causing unsafe writes | Fail closed on confirm | `test_redis_down_fails_closed_for_confirm` |
| 11 | Provider timeout causing an unverified action | Typed errors; STT failure stops before the agent | `test_stt_failure_is_safe_no_business_action` |
| 12 | Secrets in logs/responses | Keys only in server env; adapters never echo upstream bodies; request-id logging | `test_request_id_header_and_no_secrets_in_logs` |
| 13 | Customer token used on operator routes; shared or guessed operator credentials | Named operator accounts, scrypt-hashed passwords, 5-strike lockout, revocable server-side tokens, roles VIEWER/AGENT/ADMIN, operator id in the audit log | `test_ops_requires_operator_sign_in`, `test_operator_account_locks_after_repeated_failures`, `test_roles_limit_what_an_operator_can_do` |
| 14 | Unbounded audio / frame floods | Per-frame and total caps, idle timeout, per-session rate limit | `test_ws_audio_buffer_is_bounded` |
| 15 | PII at rest | Name, phone and email encrypted with AES-256-GCM (key from `DATA_ENCRYPTION_KEY`, bound to field name); phone lookup via HMAC blind index so no plaintext number is stored or searchable; redacted transcripts, masked UTR/phone, no raw audio, TTL retention | `test_personal_fields_are_encrypted_at_rest`, `tests/unit/test_crypto.py`, `test_ops_numbers_derive_from_events_and_detail_is_masked` |
| 16 | Personal data leaking to the model or logs | Live customer turn is redacted before reaching the LLM; model gets first name only; prompt tells it numbers are redacted | `test_model_input_never_contains_personal_identifiers`, `test_customer_context_tool_gives_first_name_only` |
| 18 | Model reply leaks personal data or unsafe content (PIN requests, refund claims, invented ids/amounts, phones, emails, account numbers) | Response guard replaces the reply with a safe fallback | `tests/api/test_guardrails.py` |
| 19 | Voice sent to third parties without consent; call started without notice consent | Session needs notice consent; audio endpoints and WebSocket audio need microphone consent | `test_voice_is_refused_without_microphone_consent`, `test_session_requires_privacy_notice_consent` |
| 20 | Learning which numbers are customers (login probing, timing, SMS failures) | Identical response for registered and unregistered numbers; SMS sent in the background; gateway errors swallowed and audited | `test_code_is_sent_by_sms_only_for_registered_numbers_and_never_returned`, `test_gateway_failure_looks_identical_to_the_caller` |
| 21 | Model blocks a card the customer did not ask to block, or a card that is not theirs | `confirm_card_block` needs this turn's explicit yes plus a matching session-bound preparation; card ids must come from `list_cards` in this conversation; every card query filters on `customer_id`; only one action may be pending so a bare "yes" is unambiguous | `test_model_cannot_confirm_a_block_the_customer_did_not_confirm`, `test_model_cannot_block_cards_it_invented_or_that_belong_to_someone_else`, `test_one_pending_action_at_a_time` |
| 22 | Double or raced card block; replayed confirmation | Atomic ACTIVE to BLOCKED update (one winner), single-use preparation, audit event only for the winner | `test_concurrent_confirmations_block_once`, `test_rest_card_block_prepare_confirm_and_ownership` |
| 23 | Attacker with a hijacked session unblocks a card or changes loan terms | No unblock/replace/prepayment tool exists: those are human-only and the voice agent hands off | `test_unblock_and_replacement_go_to_a_human_and_change_nothing` |
| 24 | Model states an invented balance, EMI, card or account digits | Response guard checks amounts and "ending NNNN" against tool output; money text is produced by the backend | `test_ungrounded_card_and_account_claims_are_replaced`, `test_balance_enquiry_is_grounded_and_audited` |
| 25 | Balance/statement/loan data read without a trace | `BALANCE_VIEWED`, `STATEMENT_VIEWED`, `CARDS_LISTED`, `LOAN_VIEWED` audit events per read | `test_balance_enquiry_is_grounded_and_audited` |
| 17 | Data kept longer than needed / no way to remove a customer | TTL on transcripts (7d) and call metadata (90d); admin erasure keeps regulated records only | `test_erasure_removes_personal_data_keeps_banking_records_and_needs_admin` |
| 26 | Login-code flooding, SMS cost abuse, number probing through the limit itself | Per-number (blind-index key, no number in Redis) and per-address counters, identical for registered and unknown numbers; 429 after the limit | `test_login_code_requests_are_limited_per_number_and_look_the_same_for_unknown_numbers`, `..._per_client_address` |
| 27 | Session token leaking into proxy/server logs through the WebSocket URL | Token never in a URL: a 30-second single-use ticket opens the stream; the session token no longer works there | `test_ws_rejects_bad_ticket_mismatched_session_and_the_session_token`, `test_ws_ticket_is_single_use` |
| 28 | Cached or framed responses exposing account data; browser sniffing; leftover API docs in production | `Cache-Control: no-store`, `nosniff`, `X-Frame-Options: DENY`, `no-referrer`, CSP `default-src 'none'` on every response; HSTS in production; docs/OpenAPI off in production; CORS limited to one origin | `tests/api/test_hardening.py` |
| 29 | Audit trail edited, reordered or truncated after the fact | Each event stores a hash of its content plus the previous hash (SHA-256 chain, compare-and-set head, unique sequence index); `scripts/verify_audit_chain.py` recomputes it and names the first broken event. Detects tampering; it does not prevent a database admin from rewriting the whole chain, so keep the head hash off-box for that | `tests/unit/test_audit_chain.py`, verified on real MongoDB incl. edit and delete |
| 30 | Password guessing, credential stuffing, customer enumeration through login | scrypt-hashed passwords (never logged or returned); 5 failures lock the number, 30 lock the client address; unknown number, wrong password, closed account and no password all give the same 401 and cost one hash; temporary passwords cannot open anything until changed; every attempt audited without the password | `tests/api/test_password_login.py` |

## Production start-up lock

In every environment except `test`, `ECHO_DEV_OTP` must be off (the API never returns a login code), and with `AUTH_MODE=otp` login codes must go out by SMS (`OTP_PROVIDER=http`, a gateway URL and message template). With `APP_ENV=production` it also refuses to start unless: storage is real MongoDB/Redis, `ECHO_DEV_OTP` is off,
an SMS gateway (`OTP_PROVIDER=http`, https URL, body template) is configured, speech and LLM providers are real (not mock/rules), the frontend origin is https, and raw audio storage is off. Seeding never runs in
production. Tested in `tests/unit/test_production_lock.py`.

## Known gaps (not mitigated)

- Balances and statements are read out loud, so anyone near the customer's speaker hears them. A production bank
  would decide per product whether voice may read balances, and may require a stronger factor than SMS OTP.
- Card block has no step-up check beyond the OTP session. A real bank may require a second factor for it.

- Operator accounts have no MFA or password-reset flow yet, and there is no operator-management API (accounts are created by script).
- The session token is a bearer token returned in JSON (a cookie-based transport would be needed for production).
- OTP is only as strong as the SMS channel (SIM-swap, SMS interception). Requests are rate-limited per number and per address, but a bank would want a stronger second factor (registered device, voice biometrics).
- The rules LLM and regex intent classifier are brittle; an Ollama model's tool accuracy has not been measured.
- TLS is terminated by a reverse proxy (`infra/Caddyfile` is provided) and has not been tested end-to-end here. CORS is limited to one origin and the needed methods. No penetration test has been done; load was measured on one laptop (see docs/bank-readiness.md).

- Password login is a single factor. A bank would normally add a second one (a texted code in `AUTH_MODE=otp`, a registered device, or voice biometrics) and a self-service reset flow, which does not exist yet (an operator re-issues a temporary password with `scripts/set_customer_password.py`).
