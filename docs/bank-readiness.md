# Bank readiness

KoshVaani is a voice service desk for banks. This page says what has been done toward running it for a real bank, what was
measured, and what only the bank can supply. It is a status record, not a certification.

## Controls in place (each has a test; see `docs/threat-model.md` for the full table)

| Area | What is built |
|---|---|
| Identity | Mobile number + password (scrypt hashes, lockout, identical failures, forced change of temporary passwords) or, with `AUTH_MODE=otp`, a texted one-time code (3 wrong codes burn it, salted hash only); nothing is shown before sign-in |
| Abuse limits | Login-code requests limited per number and per client address; per-session request rate limit; audio size and frame caps; operator lockout after 5 failures |
| Data protection | Name, phone, email encrypted at rest (AES-256-GCM, key from `DATA_ENCRYPTION_KEY`); phone lookup by keyed blind index; only last 4 digits of accounts, cards and UTRs stored; transcripts redacted; no raw audio; retention TTLs; admin erasure that keeps regulated records |
| Decisions | Policy and every write live in the backend; the model cannot confirm, create or choose ids; one change per explicit "yes"; idempotent and race-safe; fails closed if Redis is down |
| Model safety | Recognised requests are planned and worded from database output; the model only handles free text; response guard rejects ungrounded amounts/ids, PIN or refund talk, and offers of actions reserved for humans |
| Audit | Every read and action is recorded; events are SHA-256 hash-chained and `scripts/verify_audit_chain.py` detects edits, deletions and reordering |
| Transport and headers | `no-store`, `nosniff`, frame denial, CSP and HSTS (production); docs/OpenAPI off in production; strict CORS; WebSocket opened with a 30-second single-use ticket instead of the session token; TLS and proxy config in `infra/Caddyfile` |
| Operations | Production start-up lock refuses unsafe settings; operator accounts with hashed passwords, roles and server-side revocable tokens; `/healthz` and `/readyz` |
| Supply chain | `pip-audit` (no known vulnerabilities), `bandit` (no findings at medium or higher), `npm audit --omit=dev` (0) on 2026-11-20 |

## Measured (one laptop, one uvicorn worker, real MongoDB 9 and Redis, deterministic planner and mock speech)

`scripts/load_test.py`: each caller opens a session, verifies by OTP, and asks 6 questions. Load generator and server share the
machine, so these are conservative.

| Concurrent callers | Throughput | Agent turn p50 / p95 | Failures |
|---|---|---|---|
| 25 | 129 req/s | 109 ms / 293 ms | 0 |
| 100 | 88 req/s | about 0.5 s / about 2.5 s | 0 |
| 200 | 88 req/s | about 1 s / about 4.9 s | 0 |

One worker saturates near 90 requests per second here and degrades gracefully (no errors). It was not profiled further. Audit
writes are serialised per process to keep the hash chain intact, so scale by adding workers or instances and re-test.
Real voice turns with Groq and Deepgram measured 0.6 to 0.9 s end to end on short utterances (small sample).

## What is not done

1. **Second factor and password reset.** Password login is one factor, and there is no self-service reset (an operator re-issues a temporary password). `AUTH_MODE=otp` adds a texted code but needs an SMS gateway; a bank may require more (registered device, voice biometrics, step-up before a card block).
2. **Operator MFA and password reset.** Operators have named accounts and lockout but no second factor and no self-service reset.
3. **Core banking and UPI integration.** The agent reads and writes only its own MongoDB. Connecting it to real systems and real dispute rails is bank-specific work.
4. **Telephony.** Browser microphone only; no SIP or phone line.
5. **Indian languages.** English only.
6. **Independent review.** No penetration test, no compliance review against the bank's own policy, the RBI directions or the DPDP Act, no disaster-recovery or backup drill, no large-scale load test.
7. **Speech accuracy on real calls.** Measured on short, clean samples, not on accented or noisy telephone audio.

## What the bank must provide

An SMS gateway URL and key if it wants texted codes, a MongoDB and Redis it operates (with backups and encryption at rest), a TLS certificate and reverse proxy,
a place to keep `DATA_ENCRYPTION_KEY` (a KMS or vault, backed up: losing it makes stored personal data unreadable), the real customer
data (`docs/runbook.md`, "Importing real customer data"), and its own security and compliance sign-off.
