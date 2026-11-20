from datetime import datetime, time, timedelta, timezone

from app.schemas.transaction import TransactionSearch


class TransactionRepository:
    """Every query takes customer_id: ownership is enforced here, not by callers."""

    def __init__(self, db):
        self.col = db.transactions

    async def search(self, customer_id: str, q: TransactionSearch) -> list[dict]:
        filt: dict = {"customer_id": customer_id}
        if q.transaction_type == "UPI":
            filt["channel"] = "UPI"
        elif q.transaction_type in ("DEBIT", "CREDIT"):
            filt["direction"] = q.transaction_type
        if q.amount_paise is not None:
            filt["amount_paise"] = q.amount_paise
        rng: dict = {}
        if q.date_from:
            rng["$gte"] = datetime.combine(q.date_from, time.min, tzinfo=timezone.utc)
        if q.date_to:
            rng["$lt"] = datetime.combine(q.date_to + timedelta(days=1), time.min, tzinfo=timezone.utc)
        if rng:
            filt["transaction_at"] = rng
        return [d async for d in self.col.find(filt, {"_id": 0}).sort("transaction_at", -1).limit(q.limit or 20)]

    async def get_owned(self, customer_id: str, transaction_id: str) -> dict | None:
        return await self.col.find_one(
            {"customer_id": customer_id, "transaction_id": transaction_id}, {"_id": 0}
        )
