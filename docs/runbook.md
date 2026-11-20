# Runbook

## First run (no keys, no Docker)

```powershell
cd E:\PROJECTS\KoshVaani
Copy-Item .env.example .env
backend\.venv\Scripts\activate.bat
cd backend
python -m pytest -q                      # 206 tests; hermetic, ignores your real .env keys
uvicorn app.main:app --reload --port 8000
```
Open `http://127.0.0.1:8000/docs`. Defaults: in-memory storage, rules LLM, mock speech.

## Turn on real components (one at a time)

| Component | `.env` | Notes |
|---|---|---|
| Groq Whisper STT | `STT_PROVIDER=groq`, `GROQ_API_KEY=...` | free key at console.groq.com |
| Deepgram Aura TTS | `TTS_PROVIDER=deepgram`, `DEEPGRAM_API_KEY=...` | free credit at console.deepgram.com. Default voice `aura-asteria-en` (~1 s); the `aura-2-*` voices measured 3-9 s (`DEEPGRAM_TTS_MODEL` to change) |
| Ollama LLM | `LLM_PROVIDER=ollama`, `OLLAMA_MODEL=qwen3.5:4b` | `ollama pull qwen3.5:4b` first. Recognised requests (balance, statement, loans, cards, dispute confirm, unblock -> human) are planned and worded by the deterministic planner from database tool output; the model only sees free-form text. Thinking is off and the model stays loaded (`OLLAMA_KEEP_ALIVE`, default 30m). Benchmarked on an RTX 3060 6 GB: qwen3.5:4b is the best tool caller that fits fully on the GPU. Do not test via `ollama run` (it loads a 262k context and spills to CPU) |
| Real MongoDB/Redis | `STORAGE_BACKEND=mongo` + URIs | then `python scripts\seed_demo_data.py` |

Verify speech with real providers: `python scripts\live_speech_check.py` (TTS then STT round trip, prints WER for that sample).
Check `/readyz` -> `providers` to see what is active.

## Scenarios to rehearse (seeded data)

1. **Happy path** `CUS-1001`: "I sent four thousand two hundred and fifty rupees yesterday, the money was deducted but the recipient has not received it" -> confirm -> a `KV-...` reference. Repeat "yes": same reference, no second case.
2. **Ambiguity** `CUS-1002`: "my 1500 rupee payment yesterday failed" -> asks which one -> "the second one".
3. **Not eligible** `CUS-1003`: successful payment -> explanation and human-agent offer.
4. **Tracking** `CUS-1004`: "what happened to complaint KV-20261110-000001".
5. **Isolation**: as `CUS-1001` ask about `KV-20261110-000001` -> not found.
6. **Handoff**: "I want a human agent".
7. **Balance** `CUS-1001`: "what is my balance" -> savings account ending 4821, 85,234 rupees 50 paise, with 84,334 rupees 50 paise available (a card hold).
8. **Mini statement** `CUS-1001`: "give me my mini statement" -> UPI, card, ATM and salary credit, newest first.
9. **Card block** `CUS-1001` (two cards): "my card was stolen, block it" -> "which card?" -> "the debit one" -> confirm -> blocked. The credit card stays active. Say "yes" again: nothing else changes.
10. **Card already blocked / unblock** `CUS-1004`: "block my card" -> already blocked; "please unblock my card" -> human handoff (unblocking is never self-service).
11. **Loan** `CUS-1003`: "when is my next EMI due" -> personal loan, outstanding amount, EMI and due date. `CUS-1002` has no loan.

Seed transactions are dated relative to today (the in-memory app seeds at startup; `seed_demo_data.py` seeds on the day you run it),
so "yesterday" finds them. Re-run the reset on a later day for fresh relative dates.

## Reset

- In memory: restart the server.
- Real MongoDB: `python scripts\reset_demo_data.py --yes` (drops only the configured database).

## Measure

`python scripts\benchmark_voice.py --runs 10` prints a latency table for the configured LLM/TTS. Copy results with
exact environment details into `docs/evaluation-report.md`. ASR/WER needs your own recorded English test set.

## Local frontend build

`npm run build` refuses a non-https `VITE_API_BASE` by design. For a local-only build: `$env:ALLOW_INSECURE_API=1; npm run build` (and `npm run build:ops`), then `python scripts\check_frontend_bundle.py`.

## Full call-flow check (real providers)

Start a fresh backend (`LLM_PROVIDER=ollama`, bootstrap operator env vars as in the `scripts\e2e_scenarios.py` header) and run it: 67 checks, about 20 s. It needs a fresh backend each time.

Free-form model evaluation: `python scripts\eval_freeform.py --runs 3` (35 cases x runs; exits 1 on any safety failure; results go in `docs/evaluation-report.md`).

## Real MongoDB + Redis (no Docker)

Windows services work: MongoDB Community and Memurai (Redis-compatible). Use a throwaway database first:
`$env:STORAGE_BACKEND="mongo"; $env:MONGODB_DATABASE="koshvaani_verify"; $env:REDIS_URL="redis://localhost:6379/15"`, then `python scripts\seed_demo_data.py`, start the backend, check `/readyz` shows mongodb/redis `ok`, and run the call-flow script. Drop the database afterwards.

## Customer sign-in

`AUTH_MODE=password` (default): customers sign in with mobile number + password. After an import run
`python scripts\set_customer_password.py --all-missing --out data-import\temp-passwords.csv`; each customer gets a random temporary
password and must choose their own at first sign-in. The file holds working credentials: hand them out over a channel you trust and
delete it. For one customer: `--customer CUS-0001` (you type the password, hidden) or `--customer CUS-0001 --generate`.
To re-issue a forgotten password, set `password_hash` aside by running the script with `--customer ID --generate`.

`AUTH_MODE=otp`: a texted code through your SMS gateway (`OTP_PROVIDER=http`, `SMS_URL`, `SMS_BODY_TEMPLATE`); the app refuses to start without them.

## Importing real customer data

Fill the CSV templates in `data-import\` (column guide: `data-import\README.md`), then:
`python scripts\import_data.py --dir data-import` (check only), and with `--apply` to write to `MONGODB_DATABASE`.
It validates every row first, refuses to touch existing ids or phones, encrypts name/phone/email, and keeps only the last 4
digits of account, card and UTR numbers. With `STORAGE_BACKEND=mongo` the app never seeds anything itself.

## Hardening checks

- Audit trail: `python scriptserify_audit_chain.py` recomputes the hash chain and exits 1 naming the first altered, missing or
  reordered event. Run it on a schedule and after any incident. Keep the latest chain head hash somewhere off the database host
  if you also need to detect a full rewrite by a database administrator.
- Load: start a backend on a throwaway database with `LLM_PROVIDER=rules STT_PROVIDER=mock TTS_PROVIDER=mock` and
  `OTP_REQUESTS_PER_PHONE` / `OTP_REQUESTS_PER_IP` raised, then `python scripts\load_test.py --callers 25 --turns 6`. The
  script pre-opens its connections (a burst of simultaneous connects adds about 2 s of client-side delay on Windows).
- Login-code limits default to 5 per number and 30 per client address per 15 minutes (`OTP_REQUESTS_PER_PHONE`,
  `OTP_REQUESTS_PER_IP`, `OTP_WINDOW_SECONDS`). The call-flow and free-form evaluation scripts log in from one address many
  times, so raise the two limits when you run them.
- Supply chain: `pip install pip-audit bandit`, then `pip_audit -r backend
equirements.txt`, `bandit -r backendpp -ll`, and
  `npm audit --omit=dev` in `frontend`.

## TLS and reverse proxy

`infra\Caddyfile` terminates TLS for the API and the customer bundle, adds HSTS and the host-level headers from
`docsrontend-security.md`, and keeps request headers and bodies out of logs. Run the backend on 127.0.0.1 with
`--proxy-headers --forwarded-allow-ips 127.0.0.1` so the per-address limit sees the real client. Deploy the operator bundle
(`npm run build:ops`) only on an internal host. Not tested end to end in this repo.
