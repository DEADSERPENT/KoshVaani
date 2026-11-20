from app.infrastructure.mongodb import utcnow


class CardRepository:
    """Every query takes customer_id: ownership is enforced here, not by callers."""

    def __init__(self, db):
        self.col = db.cards

    async def list_for_customer(self, customer_id: str) -> list[dict]:
        return [d async for d in self.col.find({"customer_id": customer_id}, {"_id": 0}).sort("card_id", 1)]

    async def get_owned(self, customer_id: str, card_id: str) -> dict | None:
        return await self.col.find_one({"customer_id": customer_id, "card_id": card_id}, {"_id": 0})

    async def block_if_active(self, customer_id: str, card_id: str, reason: str) -> bool:
        """Atomic: only one concurrent request can move ACTIVE -> BLOCKED; the rest see False."""
        now = utcnow()
        res = await self.col.update_one(
            {"customer_id": customer_id, "card_id": card_id, "status": "ACTIVE"},
            {"$set": {"status": "BLOCKED", "blocked_reason": reason, "blocked_at": now, "updated_at": now}})
        return res.modified_count == 1
