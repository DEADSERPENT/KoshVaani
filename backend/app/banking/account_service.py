from app.banking.session_service import SessionContext, require_customer
from app.banking.transaction_service import TransactionService
from app.context import AppContext
from app.core.money import spoken_amount
from app.repositories.accounts import AccountRepository
from app.repositories.audit import AuditRepository
from app.repositories.sessions import VoiceSessionRepository
from app.schemas.banking import AccountView
from app.schemas.transaction import TransactionSearch, TransactionSearchResult

MINI_STATEMENT_DEFAULT = 5


def to_account_view(doc: dict) -> AccountView:
    balance = doc.get("balance_paise", 0)
    available = doc.get("available_paise", balance)
    return AccountView(
        account_id=doc["account_id"], account_type=doc["account_type"],
        account_number_masked=doc["account_number_masked"], currency=doc["currency"], status=doc["status"],
        balance_paise=balance, available_paise=available,
        balance_text=spoken_amount(balance), available_text=spoken_amount(available))


class AccountService:
    """Read-only account enquiries. Every call is scoped to the verified customer and audited."""

    def __init__(self, ctx: AppContext):
        self.accounts = AccountRepository(ctx.db)
        self.txns = TransactionService(ctx.db)
        self.audit = AuditRepository(ctx.db)
        self.sessions = VoiceSessionRepository(ctx.db)

    async def balances(self, session: SessionContext) -> list[AccountView]:
        cid = require_customer(session)
        views = [to_account_view(d) for d in await self.accounts.list_for_customer(cid)]
        await self.audit.record(action="BALANCE_VIEWED", resource_type="CUSTOMER", resource_id=cid,
                                decision="ALLOWED", reason_code=f"ACCOUNTS_{len(views)}", session_id=session.session_id)
        await self.sessions.set_outcome_if_unset(session.session_id, "INFORMATION_PROVIDED")
        return views

    async def mini_statement(self, session: SessionContext, count: int = MINI_STATEMENT_DEFAULT) -> TransactionSearchResult:
        cid = require_customer(session)
        res = await self.txns.search(cid, session.session_id, TransactionSearch(limit=count))
        await self.audit.record(action="STATEMENT_VIEWED", resource_type="CUSTOMER", resource_id=cid,
                                decision="ALLOWED", reason_code=f"RESULTS_{res.count}", session_id=session.session_id)
        await self.sessions.set_outcome_if_unset(session.session_id, "INFORMATION_PROVIDED")
        # A statement is a listing, not a search for one item: callers must not treat it as 'ambiguous'.
        return TransactionSearchResult(transactions=res.transactions, count=res.count, ambiguous=False)
