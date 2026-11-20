# Privacy and data handling

A design record for engineers and reviewers, not legal advice. Have counsel review it against the DPDP Act 2023,
RBI directions and your own bank's policy before real customers use the system.

## What personal data exists, where, and for how long

| Data | Where | Protection | Retention |
|---|---|---|---|
| Name, phone, email | `customers` | AES-256-GCM per field; phone lookup by HMAC blind index | Until erasure request |
| Phone shown to staff/UI | `phone_masked` | Last 4 digits only | Same |
| Call transcript text | `conversation_turns` | Redacted (phones, emails, long numbers, spoken secrets) before storage | `TRANSCRIPT_RETENTION_DAYS` (7), auto-expired |
| Call metadata (time, outcome) | `voice_sessions` | No transcript content | `SESSION_RETENTION_DAYS` (90), auto-expired |
| Voice audio | not stored | `STORE_RAW_AUDIO` is off and production refuses it | none |
| Login codes | Redis | Salted hash only, 3-minute TTL | 3 minutes |
| Audit trail | `audit_events` | Masked identifiers only; operator id on staff actions | Kept (regulatory) |
| Transactions, disputes | banking collections | Masked UTR; ownership enforced per customer | Kept (regulatory) |

## What leaves the system

- **LLM:** receives the redacted customer turn, a first name, transaction summaries (amount, date, status, payee label,
  masked UTR) and ids. It never receives phone numbers, emails, long account numbers, OTP/PIN digits or the full name.
  Use a model you host (Ollama) for anything touching account detail; a third-party model would receive payee labels.
- **Speech-to-text (Groq) and text-to-speech (Deepgram):** the customer's voice and the assistant's reply text go to
  these providers. Spoken digits cannot be redacted from audio, so the customer is told this at consent and the
  OTP is never spoken (it is typed in a separate step). A bank deployment should use providers under a data
  processing agreement, or self-hosted speech.
- **SMS provider (to be added):** receives the customer's phone number and the code.

## Customer rights

- **Consent:** the call screen asks for microphone and processing consent before any call starts.
- **Erasure:** `POST /api/v1/ops/customers/{id}/erase` (ADMIN only) removes name, phone, email, all call
  transcripts and handoff summaries, frees the phone number and disables login. Transactions, disputes and the audit
  trail stay because a bank must retain them; they remain linked to an inactive customer id. The action is audited.
- **Access / correction:** not implemented yet.

## Operator access

Named accounts with roles; viewing a conversation or changing a handoff is written to the audit log with the
operator id. Operators cannot see full phone numbers or emails through any API.

## Known gaps

Erasure does not end a call already in progress (sessions expire within 30 minutes); there is no access/correction
flow, no consent record per customer, and no breach-notification procedure.
