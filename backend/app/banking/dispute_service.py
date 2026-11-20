import json

from app.banking import policy_engine
from app.banking.session_service import SessionContext, guard_redis
from app.banking.transaction_service import TransactionService
from app.context import AppContext
from app.core.errors import AppError
from app.core.money import spoken_amount, spoken_date
from app.core.security import new_id, redact, sha256
from app.infrastructure.mongodb import utcnow
from app.repositories.audit import AuditRepository
from app.repositories.disputes import DisputeRepository
from app.repositories.sessions import VoiceSessionRepository
from app.repositories.transactions import TransactionRepository
from app.schemas.common import DisputeCategory, as_utc
from app.schemas.dispute import (
    DisputeConfirmRequest, DisputeConfirmResponse, DisputePrepareRequest, DisputePrepareResponse,
    DisputeView, TransactionSummary,
)


class DisputeService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.r = ctx.redis
        self.txns = TransactionRepository(ctx.db)
        self.disputes = DisputeRepository(ctx.db)
        self.audit = AuditRepository(ctx.db)
        self.sessions = VoiceSessionRepository(ctx.db)

    @staticmethod
    def _require_auth(session: SessionContext) -> str:
        if not session.customer_id:
            raise AppError("AUTHENTICATION_REQUIRED",
                           "Please verify your identity before I access transaction information.")
        return session.customer_id

    async def prepare(self, session: SessionContext, req: DisputePrepareRequest) -> DisputePrepareResponse:
        cid = self._require_auth(session)
        s = self.ctx.settings
        txn = await self.txns.get_owned(cid, req.transaction_id)
        if not txn:
            raise AppError("TRANSACTION_NOT_FOUND", "I could not find that transaction on your account.")
        active = await self.disputes.find_active(txn["transaction_id"], req.category.value)
        decision = policy_engine.evaluate(
            authenticated=True, owns_transaction=True, transaction=txn,
            category=req.category, has_active_dispute=active is not None)
        if not decision.eligible:
            await self.audit.record(action="DISPUTE_PREPARED", resource_type="TRANSACTION",
                                    resource_id=txn["transaction_id"], decision="DENIED",
                                    reason_code=decision.reason_code, session_id=session.session_id,
                                    policy_id=decision.policy_id)
            raise AppError("DISPUTE_NOT_ELIGIBLE", "This transaction is not eligible for a dispute.",
                           details={"reason_code": decision.reason_code,
                                    "existing_dispute_id": active["dispute_id"] if active else None})
        prep_id = new_id("PREP", 6)
        await guard_redis(self.r.set(f"kv:prep:{prep_id}", json.dumps({
            "session_id": session.session_id, "customer_id": cid,
            "transaction_id": txn["transaction_id"], "category": req.category.value,
            "explanation": redact(req.customer_explanation), "priority": decision.priority,
        }), ex=s.preparation_ttl_seconds))
        tx_date = as_utc(txn["transaction_at"]).date()
        await self.audit.record(action="DISPUTE_PREPARED", resource_type="TRANSACTION",
                                resource_id=txn["transaction_id"], decision="ALLOWED",
                                reason_code=decision.reason_code, session_id=session.session_id,
                                policy_id=decision.policy_id)
        return DisputePrepareResponse(
            preparation_id=prep_id, eligible=True, reason_code=decision.reason_code,
            transaction_summary=TransactionSummary(
                amount_paise=txn["amount_paise"], currency=txn["currency"],
                transaction_date=tx_date, status=txn["status"]),
            confirmation_text=(
                f"I found a UPI debit of {spoken_amount(txn['amount_paise'])} on {spoken_date(tx_date)}. "
                "The recipient credit is not confirmed in this record. "
                "Would you like me to register a dispute?"),
            expires_in_seconds=s.preparation_ttl_seconds)

    async def confirm(self, session: SessionContext, req: DisputeConfirmRequest) -> DisputeConfirmResponse:
        cid = self._require_auth(session)
        s = self.ctx.settings
        key_hash = sha256(f"{cid}:{req.idempotency_key}")
        idem_key = f"kv:idempotency:confirm:{key_hash}"

        cached = await guard_redis(self.r.get(idem_key))  # fail closed if Redis is down
        if cached:
            data = json.loads(cached)
            if data["preparation_id"] != req.preparation_id:
                raise AppError("IDEMPOTENCY_CONFLICT", "That idempotency key was already used for another request.")
            return DisputeConfirmResponse(**{**data["result"], "created": False})

        if not req.customer_confirmation:
            await self.audit.record(action="DISPUTE_CONFIRMATION", resource_type="PREPARATION",
                                    resource_id=req.preparation_id, decision="DENIED",
                                    reason_code="CUSTOMER_DECLINED", session_id=session.session_id)
            raise AppError("CONFIRMATION_REQUIRED", "No dispute was created because you did not confirm.")

        raw = await guard_redis(self.r.get(f"kv:prep:{req.preparation_id}"))
        prep = json.loads(raw) if raw else None
        if not prep or prep["session_id"] != session.session_id or prep["customer_id"] != cid:
            raise AppError("CONFIRMATION_REQUIRED", "That confirmation has expired. Please review the details again.")

        txn = await self.txns.get_owned(cid, prep["transaction_id"])
        category = DisputeCategory(prep["category"])

        # A repeat request for the same case (different key / network retry) returns the existing dispute.
        existing = await self.disputes.find_active(prep["transaction_id"], category.value)
        if existing is None:
            existing = await self.disputes.find_by_idempotency(key_hash)
        if existing:
            return await self._finish(idem_key, req.preparation_id, existing, created=False)

        decision = policy_engine.evaluate(authenticated=True, owns_transaction=txn is not None,
                                          transaction=txn, category=category, has_active_dispute=False)
        if not decision.eligible:
            raise AppError("POLICY_REJECTED", "This dispute can no longer be registered.",
                           details={"reason_code": decision.reason_code})

        lock_key = f"kv:lock:dispute:{prep['transaction_id']}"
        got = await guard_redis(self.r.set(lock_key, session.session_id, nx=True, ex=10))
        if not got:
            raise AppError("IDEMPOTENCY_CONFLICT", "Another request for this transaction is in progress.")
        try:
            now = utcnow()
            doc = {
                "dispute_id": await self.disputes.next_dispute_id(), "customer_id": cid,
                "transaction_id": prep["transaction_id"], "category": category.value, "status": "OPEN",
                "priority": decision.priority, "customer_explanation_redacted": prep["explanation"],
                "created_by": "KOSHVAANI_VOICE_AGENT", "policy_id": decision.policy_id,
                "policy_version": policy_engine.POLICY_VERSION,
                "resolution_message_key": "STANDARD_TIMELINE", "idempotency_key_hash": key_hash,
                "created_at": now, "updated_at": now,
            }
            stored = await self.disputes.insert(doc)  # unique index is the final duplicate guard
            created = not stored.pop("_dup", False)
            if created:
                await self.audit.record(action="DISPUTE_CREATED", resource_type="DISPUTE",
                                        resource_id=stored["dispute_id"], decision="ALLOWED",
                                        reason_code="CUSTOMER_CONFIRMED_AND_POLICY_ELIGIBLE",
                                        session_id=session.session_id, policy_id=decision.policy_id)
                await self.sessions.set_outcome(session.session_id, "DISPUTE_CREATED")
            return await self._finish(idem_key, req.preparation_id, stored, created=created)
        finally:
            await self.r.delete(lock_key)
            await self.r.delete(f"kv:prep:{req.preparation_id}")

    async def _finish(self, idem_key: str, prep_id: str, dispute: dict, *, created: bool) -> DisputeConfirmResponse:
        msg = ("Your complaint has been registered. Keep this reference number for tracking."
               if created else "A complaint already exists for this transaction. Here is its reference.")
        result = {"created": created, "dispute_id": dispute["dispute_id"], "status": dispute["status"],
                  "message": msg}
        await guard_redis(self.r.set(idem_key, json.dumps({"preparation_id": prep_id, "result": result}),
                                     ex=self.ctx.settings.idempotency_ttl_seconds))
        return DisputeConfirmResponse(**result)

    async def track(self, session: SessionContext, dispute_id: str) -> DisputeView:
        cid = self._require_auth(session)
        doc = await self.disputes.get_owned(cid, dispute_id)
        if not doc:  # identical answer for unknown and other customers' cases
            raise AppError("DISPUTE_NOT_FOUND", "I could not find a complaint with that reference on your account.")
        await self.audit.record(action="DISPUTE_TRACKED", resource_type="DISPUTE", resource_id=dispute_id,
                                decision="ALLOWED", reason_code="OWNER_LOOKUP", session_id=session.session_id)
        return DisputeView(
            dispute_id=doc["dispute_id"], transaction_id=doc["transaction_id"], category=doc["category"],
            status=doc["status"], priority=doc["priority"], policy_id=doc["policy_id"],
            timeline_message=policy_engine.TIMELINE_TEXT.get(doc["resolution_message_key"], ""),
            created_at=as_utc(doc["created_at"]), updated_at=as_utc(doc["updated_at"]))
