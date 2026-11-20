# Data to import into KoshVaani

Fill the five CSV files in this folder (open them in Excel or Google Sheets, keep the header row, save as CSV UTF-8).
Then run, from the repo root:

```powershell
backend\.venv\Scripts\python.exe scripts\import_data.py --dir data-import           # checks every row, writes nothing
backend\.venv\Scripts\python.exe scripts\import_data.py --dir data-import --apply   # writes to MongoDB "koshvaani"
```

Nothing is written unless every row is valid and nothing already exists. This folder is git-ignored: it holds real data.
Never put full account, card or UTR numbers anywhere: only last 4 digits are asked for or stored.

You need one row per customer, one row per account, and as many transaction/card/loan rows as you want the agent to know about
(cards and loans are optional; a customer with none just hears "no cards/loans on your profile").

## customers.csv: who can log in
| Column | Required | Meaning |
|---|---|---|
| customer_id | yes | Your own unique id, e.g. `CUS-0001`. Used to link the other files |
| full_name | yes | Encrypted at rest. The agent greets the first name |
| phone | yes | Indian mobile, 10 digits (or +91...). The one-time code is sent here. Unique per customer. Encrypted at rest |
| email | no | Encrypted at rest |
| status | no | `ACTIVE` (default), `BLOCKED` or `CLOSED` |

## accounts.csv
| Column | Required | Meaning |
|---|---|---|
| account_id | yes | Your unique id, e.g. `ACC-0001` |
| customer_id | yes | Must exist in customers.csv |
| account_type | no | `SAVINGS` (default) or `CURRENT` |
| account_last4 | yes | LAST 4 DIGITS ONLY of the account number |
| balance_rupees | yes | Current balance in rupees, e.g. `85234.50` |
| available_rupees | no | Available balance if different (card holds). Blank = same as balance |

## transactions.csv: what the agent can look up and dispute
| Column | Required | Meaning |
|---|---|---|
| transaction_id | yes | Unique id |
| customer_id, account_id | yes | The account must belong to that customer |
| channel | yes | `UPI`, `NEFT`, `IMPS`, `ATM` or `CARD`. Only `UPI` can be disputed |
| direction | yes | `DEBIT` (money out) or `CREDIT` (money in) |
| amount_rupees | yes | e.g. `4250` or `4250.00` |
| date_time | yes | `2026-11-19 14:30` (assumed IST) or full ISO with timezone |
| status | yes | `SUCCESS`, `PENDING`, `FAILED`, `REVERSED`, `INITIATED`, `DEBITED_CREDIT_UNCONFIRMED`, `UNKNOWN` |
| recipient_status | UPI debits | `CREDITED`, `NOT_CONFIRMED` (money left, recipient not confirmed) or `REVERSED`. Blank for non-UPI |
| payee_label | no | Merchant or person name as it should be spoken, e.g. `Rohan Stores` |
| reference | no | UTR / reference number. Only the last 4 characters are kept |

Dispute policy reads these two fields: a UPI debit with status `DEBITED_CREDIT_UNCONFIRMED` and recipient `NOT_CONFIRMED` is
disputable; `SUCCESS` + `CREDITED` is not.

## cards.csv (optional)
| Column | Required | Meaning |
|---|---|---|
| card_id | yes | Unique id |
| customer_id, account_id | yes | Account must belong to that customer |
| card_type | yes | `DEBIT` or `CREDIT` |
| network | no | `Visa`, `RuPay`, `Mastercard`... |
| last4 | yes | LAST 4 DIGITS ONLY |
| status | no | `ACTIVE` (default), `BLOCKED`, `EXPIRED` |
| blocked_reason | no | For BLOCKED cards: `LOST`, `STOLEN`, `SUSPECTED_FRAUD` or `OTHER`. Blank if unknown |

## loans.csv (optional)
| Column | Required | Meaning |
|---|---|---|
| loan_id | yes | Unique id |
| customer_id | yes | Must exist in customers.csv |
| loan_type | yes | e.g. `HOME`, `PERSONAL`, `VEHICLE` |
| outstanding_rupees | yes | Principal still owed |
| emi_rupees | yes | Monthly instalment |
| next_due_date | yes | `YYYY-MM-DD` |
| emis_remaining | yes | Whole number |

Disputes, handoffs, sessions and audit events are created by the app itself; do not import them.
