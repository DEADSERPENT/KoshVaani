from app.core.errors import AppError
from app.repositories.audit import AuditRepository
from app.repositories.transactions import TransactionRepository
from app.schemas.common import as_utc
from app.schemas.transaction import TransactionSearch, TransactionSearchResult, TransactionView


def to_view(doc: dict) -> TransactionView:
    return TransactionView(
        transaction_id=doc["transaction_id"], channel=doc["channel"], direction=doc["direction"],
        amount_paise=doc["amount_paise"], currency=doc["currency"],
        transaction_at=as_utc(doc["transaction_at"]), status=doc["status"],
        recipient_status=doc["recipient_status"], payee_label=doc["merchant_or_payee_label"],
        utr_masked=doc["utr_masked"],
    )


class TransactionService:
    def __init__(self, db):
        self.repo = TransactionRepository(db)
        self.audit = AuditRepository(db)

    async def search(self, customer_id: str, session_id: str, q: TransactionSearch) -> TransactionSearchResult:
        docs = await self.repo.search(customer_id, q)
        await self.audit.record(action="TRANSACTION_SEARCH", resource_type="CUSTOMER", resource_id=customer_id,
                                decision="ALLOWED", reason_code=f"RESULTS_{len(docs)}", session_id=session_id)
        views = [to_view(d) for d in docs]
        return TransactionSearchResult(transactions=views, count=len(views), ambiguous=len(views) > 1)

    async def get(self, customer_id: str, transaction_id: str) -> TransactionView:
        doc = await self.repo.get_owned(customer_id, transaction_id)
        if not doc:  # same answer for "missing" and "someone else's": no existence leak
            raise AppError("TRANSACTION_NOT_FOUND", "I could not find that transaction on your account.")
        return to_view(doc)
