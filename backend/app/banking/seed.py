"""Synthetic fixtures. All identities, phones, accounts and UTRs are fictional.

Transaction dates are relative to `today` (default: the real UTC date) so "yesterday" always finds the main
scenario. Pass a fixed `today` for fully deterministic data (tests do this via the app clock).
"""
from datetime import date, datetime, timezone

from app.infrastructure.mongodb import ensure_indexes
from app.repositories.customers import CustomerRepository

CUSTOMERS = [
    ("CUS-1001", "Aarav Mehta", "+919900000101", "4821"),
    ("CUS-1002", "Diya Rao", "+919900000102", "5532"),
    ("CUS-1003", "Kabir Shah", "+919900000103", "6643"),
    ("CUS-1004", "Meera Nair", "+919900000104", "7754"),
]


def _at(today: date, days_ago: int, hour: int = 7) -> datetime:
    d = date.fromordinal(today.toordinal() - days_ago)
    return datetime(d.year, d.month, d.day, hour, 30, tzinfo=timezone.utc)


def build_transactions(today: date) -> list[dict]:
    created = _at(today, 0, 6)

    def txn(tid, cust, acc, paise, at, status, recipient, payee, utr):
        return {
            "transaction_id": tid, "customer_id": cust, "account_id": acc, "channel": "UPI",
            "direction": "DEBIT", "amount_paise": paise, "currency": "INR", "transaction_at": at,
            "status": status, "recipient_status": recipient, "merchant_or_payee_label": payee,
            "utr_masked": "*" * 8 + utr[-4:], "source": "SYNTHETIC_LEDGER", "created_at": created,
        }

    def other(tid, cust, acc, channel, direction, paise, at, label, ref):
        # Non-UPI rails (NEFT/IMPS/ATM/card): no recipient-credit uncertainty, so recipient_status is not applicable.
        return {
            "transaction_id": tid, "customer_id": cust, "account_id": acc, "channel": channel,
            "direction": direction, "amount_paise": paise, "currency": "INR", "transaction_at": at,
            "status": "SUCCESS", "recipient_status": "NOT_APPLICABLE", "merchant_or_payee_label": label,
            "utr_masked": "*" * 8 + ref[-4:], "source": "SYNTHETIC_LEDGER", "created_at": created,
        }

    return [
        other("TXN-NEFT-1001", "CUS-1001", "ACC-1001", "NEFT", "CREDIT", 6200000, _at(today, 6, 4), "Salary Credit", "SYN000100001"),
        other("TXN-ATM-1001", "CUS-1001", "ACC-1001", "ATM", "DEBIT", 500000, _at(today, 4, 14), "ATM Withdrawal", "SYN000100002"),
        other("TXN-CARD-1001", "CUS-1001", "ACC-1001", "CARD", "DEBIT", 189900, _at(today, 3, 11), "Metro Mart", "SYN000100003"),
        other("TXN-IMPS-2001", "CUS-1002", "ACC-1002", "IMPS", "CREDIT", 2500000, _at(today, 5, 10), "Rent Received", "SYN000200001"),
        other("TXN-CARD-3001", "CUS-1003", "ACC-1003", "CARD", "DEBIT", 349900, _at(today, 2, 18), "Cloud Electronics", "SYN000300001"),
        other("TXN-NEFT-4001", "CUS-1004", "ACC-1004", "NEFT", "CREDIT", 4500000, _at(today, 8, 4), "Salary Credit", "SYN000400001"),
        txn("TXN-UPI-1001", "CUS-1001", "ACC-1001", 425000, _at(today, 1), "DEBITED_CREDIT_UNCONFIRMED",
            "NOT_CONFIRMED", "Rohan Stores", "SYN000009012"),
        txn("TXN-UPI-2001", "CUS-1002", "ACC-1002", 150000, _at(today, 1, 5), "DEBITED_CREDIT_UNCONFIRMED",
            "NOT_CONFIRMED", "Anika Sharma", "SYN000002001"),
        txn("TXN-UPI-2002", "CUS-1002", "ACC-1002", 150000, _at(today, 1, 9), "DEBITED_CREDIT_UNCONFIRMED",
            "NOT_CONFIRMED", "Vihaan Gupta", "SYN000002002"),
        txn("TXN-UPI-3001", "CUS-1003", "ACC-1003", 99900, _at(today, 1), "SUCCESS",
            "CREDITED", "Green Grocers", "SYN000003001"),
        txn("TXN-UPI-4001", "CUS-1004", "ACC-1004", 780000, _at(today, 2), "DEBITED_CREDIT_UNCONFIRMED",
            "NOT_CONFIRMED", "Ishaan Traders", "SYN000004001"),
    ]


# customer -> (balance_paise, available_paise). Available is lower where a card authorisation is on hold.
BALANCES = {"CUS-1001": (8523450, 8433450), "CUS-1002": (2150000, 2150000),
            "CUS-1003": (14250075, 14250075), "CUS-1004": (96500, 96500)}


def build_cards() -> list[dict]:
    def card(cid, cust, acc, ctype, network, last4, status="ACTIVE", reason=None):
        return {"card_id": cid, "customer_id": cust, "account_id": acc, "card_type": ctype, "network": network,
                "last4": last4, "status": status, "blocked_reason": reason}

    return [
        card("CRD-1001-D", "CUS-1001", "ACC-1001", "DEBIT", "RuPay", "4821"),
        card("CRD-1001-C", "CUS-1001", "ACC-1001", "CREDIT", "Visa", "7316"),
        card("CRD-1002-D", "CUS-1002", "ACC-1002", "DEBIT", "Visa", "5532"),
        card("CRD-1003-D", "CUS-1003", "ACC-1003", "DEBIT", "Mastercard", "6643"),
        card("CRD-1004-D", "CUS-1004", "ACC-1004", "DEBIT", "RuPay", "7754", "BLOCKED", "LOST"),
    ]


def build_loans(today: date) -> list[dict]:
    def due(days: int) -> datetime:
        d = date.fromordinal(today.toordinal() + days)
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)

    now = _at(today, 0, 6)

    def loan(lid, cust, ltype, outstanding, emi, days, remaining):
        return {"loan_id": lid, "customer_id": cust, "loan_type": ltype, "status": "ACTIVE",
                "outstanding_paise": outstanding, "emi_paise": emi, "next_due_date": due(days),
                "emis_remaining": remaining, "updated_at": now}

    return [
        loan("LN-1001", "CUS-1001", "VEHICLE", 31250000, 1475000, 9, 24),
        loan("LN-1003", "CUS-1003", "PERSONAL", 18400000, 920000, 3, 20),
    ]


def build_dispute(today: date) -> dict:
    # The reference is fixed so docs and scenarios can name it; only the timestamps move with `today`.
    return {
        "dispute_id": "KV-20261110-000001", "customer_id": "CUS-1004", "transaction_id": "TXN-UPI-4001",
        "category": "UPI_P2P_BENEFICIARY_NOT_CREDITED", "status": "UNDER_REVIEW", "active": True,
        "priority": "NORMAL", "customer_explanation_redacted": "Recipient has not received the payment.",
        "created_by": "KOSHVAANI_VOICE_AGENT", "policy_id": "UPI_P2P_V1", "policy_version": 1,
        "resolution_message_key": "STANDARD_TIMELINE", "idempotency_key_hash": "sha256:seed-4001",
        "created_at": _at(today, 2, 12), "updated_at": _at(today, 1, 12),
    }


async def seed(db, cipher, today: date | None = None) -> None:
    today = today or datetime.now(timezone.utc).date()
    created = _at(today, 0, 6)
    await ensure_indexes(db)
    for cid, name, phone, tail in CUSTOMERS:
        n = cid[-4:]
        await CustomerRepository(db, cipher).create(
            customer_id=cid, display_name=name, phone_e164=phone, status="ACTIVE", created_at=created,
            email=f"{name.split()[0].lower()}.test@example.test")
        await db.accounts.insert_one({
            "account_id": f"ACC-{n}", "customer_id": cid, "account_type": "SAVINGS",
            "account_number_masked": "XXXXXX" + tail, "currency": "INR", "status": "ACTIVE",
            "balance_paise": BALANCES[cid][0], "available_paise": BALANCES[cid][1], "created_at": created})
    await db.transactions.insert_many(build_transactions(today))
    await db.cards.insert_many(build_cards())
    await db.loans.insert_many(build_loans(today))
    await db.disputes.insert_one(build_dispute(today))


async def reset(db, cipher, today: date | None = None) -> None:
    """Clears ONLY the database handed in, then reseeds the known fixtures."""
    for name in await db.list_collection_names():
        await db[name].drop()
    await seed(db, cipher, today)
