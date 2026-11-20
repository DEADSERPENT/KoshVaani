from app.banking.session_service import SessionContext, require_customer
from app.context import AppContext
from app.core.money import spoken_amount
from app.repositories.audit import AuditRepository
from app.repositories.loans import LoanRepository
from app.repositories.sessions import VoiceSessionRepository
from app.schemas.banking import LoanView
from app.schemas.common import as_utc


def to_loan_view(doc: dict) -> LoanView:
    return LoanView(
        loan_id=doc["loan_id"], loan_type=doc["loan_type"], status=doc["status"],
        outstanding_paise=doc["outstanding_paise"], emi_paise=doc["emi_paise"],
        next_due_date=as_utc(doc["next_due_date"]).date(), emis_remaining=doc["emis_remaining"],
        outstanding_text=spoken_amount(doc["outstanding_paise"]), emi_text=spoken_amount(doc["emi_paise"]),
        updated_at=as_utc(doc["updated_at"]))


class LoanService:
    """Read-only loan and EMI enquiries. Prepayment, restructuring and waivers are human-only."""

    def __init__(self, ctx: AppContext):
        self.loans = LoanRepository(ctx.db)
        self.audit = AuditRepository(ctx.db)
        self.sessions = VoiceSessionRepository(ctx.db)

    async def summary(self, session: SessionContext) -> list[LoanView]:
        cid = require_customer(session)
        views = [to_loan_view(d) for d in await self.loans.list_for_customer(cid)]
        await self.audit.record(action="LOAN_VIEWED", resource_type="CUSTOMER", resource_id=cid,
                                decision="ALLOWED", reason_code=f"LOANS_{len(views)}", session_id=session.session_id)
        await self.sessions.set_outcome_if_unset(session.session_id, "INFORMATION_PROVIDED")
        return views
