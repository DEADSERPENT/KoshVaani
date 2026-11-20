from app.banking.session_service import SessionContext
from app.context import AppContext
from app.core.security import redact
from app.repositories.audit import AuditRepository
from app.repositories.sessions import VoiceSessionRepository


class HandoffService:
    def __init__(self, ctx: AppContext):
        self.sessions = VoiceSessionRepository(ctx.db)
        self.audit = AuditRepository(ctx.db)

    async def create(self, session: SessionContext, reason: str, summary: str, refs: dict | None = None) -> str:
        """Payload carries the verified customer id (never secrets) plus matched ids and the reason."""
        hid = await self.sessions.create_handoff(
            session.session_id, session.customer_id, reason, redact(summary), refs or {})
        await self.audit.record(action="HANDOFF_CREATED", resource_type="HANDOFF", resource_id=hid,
                                decision="ALLOWED", reason_code=reason, session_id=session.session_id)
        return hid
