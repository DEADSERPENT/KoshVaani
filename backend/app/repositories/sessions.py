from app.core.security import new_id
from app.infrastructure.mongodb import utcnow


class VoiceSessionRepository:
    def __init__(self, db):
        self.col = db.voice_sessions
        self.handoffs = db.handoffs

    async def create(self, session_id: str, channel: str, language: str) -> None:
        await self.col.insert_one({
            "session_id": session_id, "customer_id": None, "channel": channel, "language": language,
            "status": "CREATED", "started_at": utcnow(), "ended_at": None, "outcome": None,
            "escalation_reason": None,
        })

    async def set_customer(self, session_id: str, customer_id: str) -> None:
        await self.col.update_one({"session_id": session_id}, {"$set": {"customer_id": customer_id}})

    async def set_outcome(self, session_id: str, outcome: str) -> None:
        await self.col.update_one({"session_id": session_id}, {"$set": {"outcome": outcome}})

    async def set_outcome_if_unset(self, session_id: str, outcome: str) -> None:
        """Keeps the first meaningful outcome (a created dispute is not downgraded to 'information provided')."""
        await self.col.update_one({"session_id": session_id, "outcome": None}, {"$set": {"outcome": outcome}})

    async def create_handoff(self, session_id: str, customer_id: str | None, reason: str, summary: str,
                             refs: dict) -> str:
        handoff_id = new_id("HO")
        await self.handoffs.insert_one({
            "handoff_id": handoff_id, "session_id": session_id, "customer_id": customer_id,
            "reason": reason, "summary": summary, "refs": refs, "status": "QUEUED", "created_at": utcnow(),
        })
        await self.col.update_one(
            {"session_id": session_id}, {"$set": {"status": "HANDOFF", "outcome": "HANDOFF", "escalation_reason": reason}}
        )
        return handoff_id

    async def end(self, session_id: str) -> None:
        doc = await self.col.find_one({"session_id": session_id})
        outcome = (doc or {}).get("outcome") or "ABANDONED"
        status = "HANDOFF" if (doc or {}).get("status") == "HANDOFF" else "COMPLETED"
        await self.col.update_one({"session_id": session_id},
                                  {"$set": {"status": status, "ended_at": utcnow(), "outcome": outcome}})
