# 🎙️ KoshVaani

**An English voice banking support agent.** A customer speaks or types, verifies with a one-time code, and the agent
answers from the bank's data. For actions that change something (a dispute, a card block) it applies a fixed policy,
asks for an explicit "yes", and does exactly one change. Anything it can't do safely goes to a human.


## 🚀 What it does

**Customer**
- Voice or text, with spoken replies
- Sign in with mobile number and password (temporary passwords must be changed at first sign-in); nothing is shown before it
- Balance, mini statement, loans and EMIs, card status
- UPI payment disputes: finds the transaction, checks policy, confirms, registers one case and reads back the reference
- Block a lost or stolen card, with confirmation
- Handoff to a human for everything else (card unblock or replacement, limit changes, transfers, waivers)

**Operator console**
- Live KPIs, handoff queue, per-call redacted transcripts and audit trail
- Named accounts with roles (`VIEWER`, `AGENT`, `ADMIN`), lockout and revocable sessions

## 🤖 How the agent stays safe

- **Database first.** Recognised requests are planned and worded from the database, so the answer is the data, not a guess.
- **Model for free text only.** The LLM never writes to the database, picks ids or decides eligibility. A response guard blocks invented amounts and ids, PIN or refund talk, and offers of actions reserved for humans.
- **Explicit confirmation.** One change per clear "yes"; retries and races cannot create duplicates.
- **Privacy.** Names, phones and emails are encrypted at rest; only the last 4 digits of accounts and cards are stored; transcripts are redacted; there is no raw audio storage.
- **Audit.** Every read and action is logged in a hash-chained trail that can be verified for tampering.

## 🧱 Architecture

```
Browser ──audio/text──▶ FastAPI ──▶ STT (Groq Whisper)
                           ├─▶ Orchestrator: planner (database-grounded) + Ollama LLM (free text)
                           │        └─▶ Tools: allowlist, ownership checks, policy engine
                           ├─▶ Response guard ──▶ TTS (Deepgram Aura) ──audio──▶ Browser
                           └─▶ MongoDB (data, audit) + Redis (sessions, locks)
```

Speech and language providers sit behind small interfaces, so another engine plugs in with one adapter.

## 🛠 Tech stack

| Layer | Technology |
|---|---|
| Frontend | React 18, TypeScript, Vite (separate customer and operator builds) |
| Backend | Python 3.11, FastAPI, Pydantic v2 |
| Data | MongoDB, Redis |
| Speech | Groq Whisper (STT), Deepgram Aura (TTS) |
| LLM | Ollama `qwen3.5:4b` (fits a 6 GB GPU) |
| Security | AES-256-GCM, HMAC blind index, scrypt, SHA-256 audit chain |

## ⚙️ Quick start

Needs Python 3.11+, Node 18+, MongoDB and Redis running locally, and optionally Ollama (`ollama pull qwen3.5:4b`).

```powershell
cd KoshVaani
Copy-Item .env.example .env

cd backend
python -m venv .venv ; .venv\Scripts\activate ; pip install -r requirements.txt
python -c "from app.core.crypto import generate_key; print(generate_key())"   # put in DATA_ENCRYPTION_KEY
python -m pytest -q
uvicorn app.main:app --port 8000

cd ..\frontend ; npm install ; npm run dev        # http://localhost:5173
```

**Load your data:** fill the CSVs in `data-import\` (see its README), then
`python scripts\import_data.py --dir data-import --apply`. Give customers a password with
`python scripts\set_customer_password.py --all-missing --out data-import\temp-passwords.csv` (they choose their own at first sign-in).
Create the first operator with `python scripts\create_operator.py`.

### Key settings (`.env`)

| Variable | Purpose |
|---|---|
| `STORAGE_BACKEND` | `mongo` for real MongoDB and Redis |
| `DATA_ENCRYPTION_KEY` | Encrypts personal data. Back it up: losing it makes stored data unreadable |
| `LLM_PROVIDER` | `ollama` or `rules` |
| `STT_PROVIDER` / `GROQ_API_KEY` | `groq` or `mock` |
| `TTS_PROVIDER` / `DEEPGRAM_API_KEY` | `deepgram` or `mock` |
| `AUTH_MODE` | `password` (mobile number + password, default) or `otp` (texted code; needs `SMS_URL` and `SMS_BODY_TEMPLATE`) |
| `APP_ENV` | `production` refuses to start with any development shortcut on |

## 🌐 API

Base `/api/v1`; full contracts in [`docs/api-contracts.md`](docs/api-contracts.md).

| Area | Endpoints |
|---|---|
| Session and login | `POST /sessions`, `/auth/challenges`, `/auth/challenges/{id}/verify` |
| Conversation | `POST /conversation/turn`, `/voice/turn`, WebSocket `/voice/sessions/{id}/stream` |
| Account data | `GET /accounts`, `/cards`, `/loans`, `/transactions` |
| Actions | `POST /disputes/prepare` + `/confirm`, `/cards/block/prepare` + `/confirm`, `/handoffs` |
| Operator | `POST /ops/auth/login`, `GET /ops/overview`, `/ops/sessions`, `/ops/handoffs` |

## 📊 Results

| Check | Result |
|---|---|
| Automated tests | 206 passing |
| Call-flow script | 67/67 checks, including on real MongoDB and Redis |
| Free-form evaluation (35 cases incl. injections and out-of-scope asks, real Ollama) | 100% safe, 100% correct |
| Voice turn (Groq + Deepgram) | 0.6 to 0.9 s end to end on short utterances |
| Load (one worker, one laptop) | 25 concurrent callers: turn p95 293 ms; 100 callers: about 88 req/s, no errors |

Details and method: [`docs/evaluation-report.md`](docs/evaluation-report.md).

