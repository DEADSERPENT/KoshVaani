"""Card controls. Blocking is a protective, customer-initiated action, so it is self-service behind a
spoken/clicked confirmation. Unblocking and replacement are NOT self-service: they go to a human agent."""
import json

from app.banking.session_service import SessionContext, guard_redis, require_customer
from app.context import AppContext
from app.core.errors import AppError
from app.core.security import new_id
from app.repositories.audit import AuditRepository
from app.repositories.cards import CardRepository
from app.repositories.sessions import VoiceSessionRepository
from app.schemas.banking import (
    CardBlockConfirmResponse, CardBlockPrepareResponse, CardBlockReason, CardView,
)

POLICY_ID = "CARD_BLOCK_V1"
_TYPE_WORD = {"DEBIT": "debit", "CREDIT": "credit"}


def to_card_view(doc: dict) -> CardView:
    return CardView(card_id=doc["card_id"], card_type=doc["card_type"], network=doc["network"],
                    last4=doc["last4"], status=doc["status"], blocked_reason=doc.get("blocked_reason"))


def card_phrase(view: CardView) -> str:
    return f"{_TYPE_WORD[view.card_type.value]} card ending {view.last4}"


class CardService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.r = ctx.redis
        self.cards = CardRepository(ctx.db)
        self.audit = AuditRepository(ctx.db)
        self.sessions = VoiceSessionRepository(ctx.db)

    async def list(self, session: SessionContext) -> list[CardView]:
        cid = require_customer(session)
        views = [to_card_view(d) for d in await self.cards.list_for_customer(cid)]
        await self.audit.record(action="CARDS_LISTED", resource_type="CUSTOMER", resource_id=cid,
                                decision="ALLOWED", reason_code=f"CARDS_{len(views)}", session_id=session.session_id)
        await self.sessions.set_outcome_if_unset(session.session_id, "INFORMATION_PROVIDED")
        return views

    async def prepare_block(self, session: SessionContext, card_id: str,
                            reason: CardBlockReason) -> CardBlockPrepareResponse:
        cid = require_customer(session)
        doc = await self.cards.get_owned(cid, card_id)
        if not doc:  # same answer for "missing" and "someone else's": no existence leak
            raise AppError("CARD_NOT_FOUND", "I could not find that card on your account.")
        view = to_card_view(doc)
        if view.status.value != "ACTIVE":
            await self.audit.record(action="CARD_BLOCK_PREPARED", resource_type="CARD", resource_id=card_id,
                                    decision="DENIED", reason_code=f"CARD_{view.status.value}",
                                    session_id=session.session_id, policy_id=POLICY_ID)
            raise AppError("CARD_NOT_ELIGIBLE", f"That {card_phrase(view)} is already {view.status.value.lower()}.",
                           details={"reason_code": f"CARD_{view.status.value}"})
        s = self.ctx.settings
        prep_id = new_id("CPREP", 6)
        await guard_redis(self.r.set(f"kv:cardprep:{prep_id}", json.dumps({
            "session_id": session.session_id, "customer_id": cid, "card_id": card_id, "reason": reason.value,
        }), ex=s.preparation_ttl_seconds))
        await self.audit.record(action="CARD_BLOCK_PREPARED", resource_type="CARD", resource_id=card_id,
                                decision="ALLOWED", reason_code=reason.value, session_id=session.session_id,
                                policy_id=POLICY_ID)
        return CardBlockPrepareResponse(
            preparation_id=prep_id, card=view, expires_in_seconds=s.preparation_ttl_seconds,
            confirmation_text=(
                f"I can block your {card_phrase(view)}. A blocked card stops working for payments and withdrawals, "
                "and unblocking it needs a human agent. Would you like me to block it?"))

    async def confirm_block(self, session: SessionContext, preparation_id: str,
                            confirmed: bool) -> CardBlockConfirmResponse:
        cid = require_customer(session)
        if not confirmed:
            await self.audit.record(action="CARD_BLOCKED", resource_type="PREPARATION", resource_id=preparation_id,
                                    decision="DENIED", reason_code="CUSTOMER_DECLINED", session_id=session.session_id)
            raise AppError("CONFIRMATION_REQUIRED", "No card was blocked because you did not confirm.")
        raw = await guard_redis(self.r.get(f"kv:cardprep:{preparation_id}"))
        prep = json.loads(raw) if raw else None
        if not prep or prep["session_id"] != session.session_id or prep["customer_id"] != cid:
            raise AppError("CONFIRMATION_REQUIRED", "That confirmation has expired. Please review the details again.")
        blocked = await self.cards.block_if_active(cid, prep["card_id"], prep["reason"])
        doc = await self.cards.get_owned(cid, prep["card_id"])
        view = to_card_view(doc)
        if blocked:
            await self.audit.record(action="CARD_BLOCKED", resource_type="CARD", resource_id=prep["card_id"],
                                    decision="ALLOWED", reason_code="CUSTOMER_CONFIRMED_" + prep["reason"],
                                    session_id=session.session_id, policy_id=POLICY_ID)
            await self.sessions.set_outcome(session.session_id, "CARD_BLOCKED")
        await guard_redis(self.r.delete(f"kv:cardprep:{preparation_id}"))
        msg = (f"Your {card_phrase(view)} is now blocked. To unblock it or get a replacement, "
               "I can transfer you to a human agent." if blocked
               else f"Your {card_phrase(view)} was already blocked.")
        return CardBlockConfirmResponse(blocked=blocked, card=view, message=msg)
