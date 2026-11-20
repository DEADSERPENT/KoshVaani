import secrets

from app.context import AppContext
from app.core.errors import AppError
from app.infrastructure.mongodb import utcnow
from app.repositories.audit import AuditRepository

ERASED_NOTE = "[erased on customer request]"


class PrivacyService:
    """Right-to-erasure. Removes what identifies the person and everything said in calls; keeps what a bank must
    retain (transactions, disputes, the audit trail). Those records stay linked to an inactive customer id."""

    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.db = ctx.db

    async def erase_customer(self, customer_id: str, actor_id: str) -> dict:
        cust = await self.db.customers.find_one({"customer_id": customer_id}, {"_id": 0})
        if not cust:
            raise AppError("CUSTOMER_NOT_FOUND", "Customer not found.")
        await self.db.customers.update_one({"customer_id": customer_id}, {
            "$set": {"status": "ERASED", "phone_masked": "", "erased_at": utcnow(),
                     "phone_hash": "erased-" + secrets.token_hex(16)},  # frees the number and blocks login
            "$unset": {"name_enc": "", "phone_enc": "", "email_enc": ""}})
        sessions = [s["session_id"] async for s in self.db.voice_sessions.find({"customer_id": customer_id},
                                                                               {"session_id": 1, "_id": 0})]
        turns = await self.db.conversation_turns.delete_many({"session_id": {"$in": sessions}})
        await self.db.voice_sessions.update_many({"customer_id": customer_id}, {"$set": {"customer_id": None}})
        await self.db.handoffs.update_many({"customer_id": customer_id},
                                           {"$set": {"summary": ERASED_NOTE, "customer_id": None}})
        for sid in sessions:
            await self.ctx.redis.delete(f"kv:conv:{sid}")
        await AuditRepository(self.db).record(
            action="CUSTOMER_ERASED", resource_type="CUSTOMER", resource_id=customer_id, decision="ALLOWED",
            reason_code="ERASURE_REQUEST", actor_type="OPERATOR", actor_id=actor_id)
        return {"customer_id": customer_id, "sessions": len(sessions), "turns_deleted": turns.deleted_count}
