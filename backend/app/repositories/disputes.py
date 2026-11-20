from pymongo.errors import DuplicateKeyError

from app.infrastructure.mongodb import utcnow
from app.schemas.common import ACTIVE_DISPUTE_STATUSES


class DisputeRepository:
    def __init__(self, db):
        self.col = db.disputes
        self.counters = db.counters

    async def next_dispute_id(self) -> str:
        now = utcnow()
        day = now.strftime("%Y%m%d")
        doc = await self.counters.find_one_and_update(
            {"_id": f"dispute-{day}"}, {"$inc": {"seq": 1}}, upsert=True, return_document=True
        )
        return f"KV-{day}-{doc['seq']:06d}"

    async def get_owned(self, customer_id: str, dispute_id: str) -> dict | None:
        return await self.col.find_one({"customer_id": customer_id, "dispute_id": dispute_id}, {"_id": 0})

    async def find_active(self, transaction_id: str, category: str) -> dict | None:
        return await self.col.find_one(
            {"transaction_id": transaction_id, "category": category, "active": True}, {"_id": 0}
        )

    async def find_by_idempotency(self, key_hash: str) -> dict | None:
        return await self.col.find_one({"idempotency_key_hash": key_hash}, {"_id": 0})

    async def insert(self, doc: dict) -> dict:
        """Returns the stored doc. On a unique-index collision returns the existing one with `_dup`."""
        doc["active"] = doc["status"] in {s.value for s in ACTIVE_DISPUTE_STATUSES}
        try:
            await self.col.insert_one(dict(doc))
            return doc
        except DuplicateKeyError:
            existing = await self.find_active(doc["transaction_id"], doc["category"])
            if existing is None:
                raise
            existing["_dup"] = True
            return existing
