"""Import real customer data from CSV files into the configured MongoDB (STORAGE_BACKEND=mongo).

    backend\\.venv\\Scripts\\python.exe scripts\\import_data.py --dir data-import            # check only, writes nothing
    backend\\.venv\\Scripts\\python.exe scripts\\import_data.py --dir data-import --apply    # write to MONGODB_DATABASE

Files in --dir (see data-import\\README.md for every column): customers.csv, accounts.csv, transactions.csv,
cards.csv, loans.csv. Everything is validated first; nothing is written unless every row is valid, and nothing is
written if a customer, phone number or id already exists. Names, phones and emails are encrypted with
DATA_ENCRYPTION_KEY exactly as the app does. Full account/card/UTR numbers are never stored: only the last 4 digits.
"""
import argparse
import asyncio
import csv
import re
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import get_settings  # noqa: E402
from app.core.crypto import FieldCipher  # noqa: E402
from app.infrastructure.mongodb import create_db, ensure_indexes  # noqa: E402
from app.repositories.customers import CustomerRepository  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
CHANNELS = {"UPI", "NEFT", "IMPS", "ATM", "CARD"}
TXN_STATUS = {"INITIATED", "PENDING", "SUCCESS", "FAILED", "REVERSED", "DEBITED_CREDIT_UNCONFIRMED", "UNKNOWN"}
RECIPIENT = {"NOT_APPLICABLE", "CREDITED", "NOT_CONFIRMED", "REVERSED"}
CARD_TYPES, CARD_STATUS = {"DEBIT", "CREDIT"}, {"ACTIVE", "BLOCKED", "EXPIRED"}
BLOCK_REASONS = {"LOST", "STOLEN", "SUSPECTED_FRAUD", "OTHER"}
COLUMNS = {
    "customers": ["customer_id", "full_name", "phone", "email", "status"],
    "accounts": ["account_id", "customer_id", "account_type", "account_last4", "balance_rupees", "available_rupees"],
    "transactions": ["transaction_id", "customer_id", "account_id", "channel", "direction", "amount_rupees",
                     "date_time", "status", "recipient_status", "payee_label", "reference"],
    "cards": ["card_id", "customer_id", "account_id", "card_type", "network", "last4", "status", "blocked_reason"],
    "loans": ["loan_id", "customer_id", "loan_type", "outstanding_rupees", "emi_rupees", "next_due_date",
              "emis_remaining"],
}
errors: list[str] = []


def err(file: str, line: int, msg: str) -> None:
    errors.append(f"{file}.csv line {line}: {msg}")


def read(folder: Path, name: str) -> list[tuple[int, dict]]:
    path = folder / f"{name}.csv"
    if not path.exists():
        errors.append(f"{name}.csv is missing")
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        missing = [c for c in COLUMNS[name] if c not in (rd.fieldnames or [])]
        if missing:
            errors.append(f"{name}.csv is missing columns: {', '.join(missing)}")
            return []
        return [(i, {k: (v or "").strip() for k, v in row.items()}) for i, row in enumerate(rd, start=2)
                if any((v or "").strip() for v in row.values())]


def paise(file: str, line: int, field: str, value: str, allow_blank=False) -> int | None:
    if not value and allow_blank:
        return None
    try:
        d = Decimal(value.replace(",", ""))
        if d < 0 or d.as_tuple().exponent < -2:
            raise InvalidOperation
        return int(d * 100)
    except InvalidOperation:
        err(file, line, f"{field} must be a non-negative rupee amount with at most 2 decimals, got '{value}'")
        return None


def phone_e164(file: str, line: int, value: str) -> str | None:
    digits = re.sub(r"[\s-]", "", value)
    if re.fullmatch(r"\+91[6-9]\d{9}", digits):
        return digits
    if re.fullmatch(r"(?:91)?[6-9]\d{9}", digits):
        return "+91" + digits[-10:]
    err(file, line, "phone must be an Indian mobile number (10 digits starting 6-9, optional +91)")
    return None


def when(file: str, line: int, value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=IST)  # no timezone given: Indian Standard Time
    except ValueError:
        err(file, line, f"date_time must be ISO like 2026-11-19 14:30 or 2026-11-19T14:30:00+05:30, got '{value}'")
        return None


def choice(file: str, line: int, field: str, value: str, allowed: set[str], upper=True) -> str:
    v = value.upper() if upper else value
    if v not in allowed:
        err(file, line, f"{field} must be one of {', '.join(sorted(allowed))}, got '{value}'")
    return v


def build(folder: Path) -> dict[str, list[dict]]:
    now = datetime.now(timezone.utc)
    out: dict[str, list[dict]] = {k: [] for k in COLUMNS}
    seen: dict[str, set] = {k: set() for k in ["customer", "phone", "account", "txn", "card", "loan"]}
    cust_ids: set[str] = set()
    acc_owner: dict[str, str] = {}

    def unique(kind, file, line, value, label):
        if not value:
            err(file, line, f"{label} is required")
        elif value in seen[kind]:
            err(file, line, f"duplicate {label} '{value}'")
        seen[kind].add(value)

    for line, r in read(folder, "customers"):
        unique("customer", "customers", line, r["customer_id"], "customer_id")
        cust_ids.add(r["customer_id"])
        if not r["full_name"]:
            err("customers", line, "full_name is required")
        ph = phone_e164("customers", line, r["phone"])
        if ph:
            unique("phone", "customers", line, ph, "phone")
        if r["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", r["email"]):
            err("customers", line, f"email looks invalid: '{r['email']}'")
        out["customers"].append({"customer_id": r["customer_id"], "display_name": r["full_name"], "phone_e164": ph or "",
                                 "email": r["email"], "status": choice("customers", line, "status", r["status"] or "ACTIVE", {"ACTIVE", "BLOCKED", "CLOSED"}),
                                 "created_at": now})

    for line, r in read(folder, "accounts"):
        unique("account", "accounts", line, r["account_id"], "account_id")
        if r["customer_id"] not in cust_ids:
            err("accounts", line, f"customer_id '{r['customer_id']}' is not in customers.csv")
        if not re.fullmatch(r"\d{4}", r["account_last4"]):
            err("accounts", line, "account_last4 must be exactly 4 digits (never put the full account number here)")
        bal = paise("accounts", line, "balance_rupees", r["balance_rupees"])
        avail = paise("accounts", line, "available_rupees", r["available_rupees"], allow_blank=True)
        acc_owner[r["account_id"]] = r["customer_id"]
        out["accounts"].append({
            "account_id": r["account_id"], "customer_id": r["customer_id"],
            "account_type": choice("accounts", line, "account_type", r["account_type"] or "SAVINGS", {"SAVINGS", "CURRENT"}),
            "account_number_masked": "XXXXXX" + r["account_last4"], "currency": "INR", "status": "ACTIVE",
            "balance_paise": bal or 0, "available_paise": bal if avail is None else avail, "created_at": now})
        if avail is not None and bal is not None and avail > bal:
            err("accounts", line, "available_rupees cannot be more than balance_rupees")

    for line, r in read(folder, "transactions"):
        unique("txn", "transactions", line, r["transaction_id"], "transaction_id")
        if acc_owner.get(r["account_id"]) != r["customer_id"]:
            err("transactions", line, f"account_id '{r['account_id']}' does not belong to customer '{r['customer_id']}'")
        ch = choice("transactions", line, "channel", r["channel"], CHANNELS)
        direction = choice("transactions", line, "direction", r["direction"], {"DEBIT", "CREDIT"})
        rec = r["recipient_status"].upper()
        if not rec:
            if ch == "UPI" and direction == "DEBIT":
                err("transactions", line, "recipient_status is required for UPI debits "
                                          "(CREDITED, NOT_CONFIRMED or REVERSED)")
            rec = "NOT_APPLICABLE"
        choice("transactions", line, "recipient_status", rec, RECIPIENT)
        ref = re.sub(r"\W", "", r["reference"])
        out["transactions"].append({
            "transaction_id": r["transaction_id"], "customer_id": r["customer_id"], "account_id": r["account_id"],
            "channel": ch, "direction": direction, "amount_paise": paise("transactions", line, "amount_rupees", r["amount_rupees"]) or 0,
            "currency": "INR", "transaction_at": when("transactions", line, r["date_time"]),
            "status": choice("transactions", line, "status", r["status"], TXN_STATUS), "recipient_status": rec,
            "merchant_or_payee_label": r["payee_label"], "utr_masked": ("*" * 8 + ref[-4:]) if ref else "",
            "source": "IMPORTED", "created_at": now})

    for line, r in read(folder, "cards"):
        unique("card", "cards", line, r["card_id"], "card_id")
        if acc_owner.get(r["account_id"]) != r["customer_id"]:
            err("cards", line, f"account_id '{r['account_id']}' does not belong to customer '{r['customer_id']}'")
        if not re.fullmatch(r"\d{4}", r["last4"]):
            err("cards", line, "last4 must be exactly 4 digits (never put the full card number here)")
        status = choice("cards", line, "status", r["status"] or "ACTIVE", CARD_STATUS)
        reason = r["blocked_reason"].upper() or None
        if reason and reason not in BLOCK_REASONS:
            err("cards", line, f"blocked_reason must be blank or one of {', '.join(sorted(BLOCK_REASONS))}, got '{reason}'")
        out["cards"].append({
            "card_id": r["card_id"], "customer_id": r["customer_id"], "account_id": r["account_id"],
            "card_type": choice("cards", line, "card_type", r["card_type"], CARD_TYPES), "network": r["network"],
            "last4": r["last4"], "status": status, "blocked_reason": reason if status == "BLOCKED" else None})

    for line, r in read(folder, "loans"):
        unique("loan", "loans", line, r["loan_id"], "loan_id")
        if r["customer_id"] not in cust_ids:
            err("loans", line, f"customer_id '{r['customer_id']}' is not in customers.csv")
        try:
            d = date.fromisoformat(r["next_due_date"])
            due = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        except ValueError:
            err("loans", line, f"next_due_date must be YYYY-MM-DD, got '{r['next_due_date']}'")
            due = None
        if not r["emis_remaining"].isdigit():
            err("loans", line, "emis_remaining must be a whole number")
        out["loans"].append({
            "loan_id": r["loan_id"], "customer_id": r["customer_id"], "loan_type": r["loan_type"].upper(), "status": "ACTIVE",
            "outstanding_paise": paise("loans", line, "outstanding_rupees", r["outstanding_rupees"]) or 0,
            "emi_paise": paise("loans", line, "emi_rupees", r["emi_rupees"]) or 0, "next_due_date": due,
            "emis_remaining": int(r["emis_remaining"] or 0), "updated_at": now})
        if not r["loan_type"]:
            err("loans", line, "loan_type is required, e.g. HOME, PERSONAL, VEHICLE")
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="data-import")
    ap.add_argument("--apply", action="store_true", help="write to the database (default: check only)")
    args = ap.parse_args()
    s = get_settings()
    data = build(Path(args.dir))
    counts = ", ".join(f"{len(v)} {k}" for k, v in data.items())
    if errors:
        print(f"NOT IMPORTED. {len(errors)} problem(s) to fix first:")
        print("\n".join("  - " + e for e in errors[:60]) + (f"\n  ... and {len(errors) - 60} more" if len(errors) > 60 else ""))
        return 1
    print(f"All rows valid: {counts}.")
    if not args.apply:
        print("Check only: nothing written. Re-run with --apply to import.")
        return 0
    if s.storage_backend != "mongo":
        sys.exit("STORAGE_BACKEND must be 'mongo' to import into MongoDB.")
    if s.is_production:
        sys.exit("Refusing to import with APP_ENV=production from this script.")
    if not s.data_encryption_key:
        sys.exit("Set DATA_ENCRYPTION_KEY first: personal data encrypted with a throwaway key could never be read again.")
    cipher = FieldCipher(s.data_encryption_key)
    db = create_db(s)
    await ensure_indexes(db, s.transcript_retention_days, s.session_retention_days)
    clash = []
    for col, key in (("customers", "customer_id"), ("accounts", "account_id"), ("transactions", "transaction_id"),
                     ("cards", "card_id"), ("loans", "loan_id")):
        ids = [r[key] for r in data[col]]
        clash += [f"{col}: {i}" for i in ids if await db[col].count_documents({key: i}, limit=1)]
    for c in data["customers"]:
        if await CustomerRepository(db, cipher).get_by_phone(c["phone_e164"]):
            clash.append(f"customers: a customer with the phone of {c['customer_id']} already exists")
    if clash:
        print("NOT IMPORTED. These already exist in the database:\n" + "\n".join("  - " + c for c in clash[:40]))
        return 1
    repo = CustomerRepository(db, cipher)
    for c in data["customers"]:
        await repo.create(**c)
    for col in ("accounts", "transactions", "cards", "loans"):
        if data[col]:
            await db[col].insert_many(data[col])
    print(f"Imported into '{s.mongodb_database}': {counts}.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
