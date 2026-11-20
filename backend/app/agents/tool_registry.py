import logging
import time
from datetime import timedelta

from pydantic import ValidationError

from app.agents import tool_schemas as ts
from app.banking.account_service import AccountService
from app.banking.card_service import CardService
from app.banking.dispute_service import DisputeService
from app.banking.handoff_service import HandoffService
from app.banking.loan_service import LoanService
from app.banking.session_service import SessionContext
from app.banking.transaction_service import TransactionService
from app.context import AppContext
from app.core.errors import AppError
from app.core.money import spoken_amount, spoken_date
from app.core.security import sha256
from app.repositories.customers import CustomerRepository
from app.schemas.dispute import DisputeConfirmRequest, DisputePrepareRequest
from app.schemas.transaction import TransactionSearch
from app.voice.transcript_normalizer import is_affirmative

log = logging.getLogger("tools")


def _txn_for_llm(v) -> dict:
    d = v.model_dump(mode="json")
    d["amount_text"] = spoken_amount(v.amount_paise)
    d["date_text"] = spoken_date(v.transaction_at.date())
    return d


class ToolRegistry:
    """Fixed allowlist. The LLM proposes; this class validates, authorizes and executes."""

    def __init__(self, ctx: AppContext, session: SessionContext, state: dict, user_text: str):
        self.ctx, self.session, self.state, self.user_text = ctx, session, state, user_text
        self.txns = TransactionService(ctx.db)
        self.disputes = DisputeService(ctx)
        self.accounts = AccountService(ctx)
        self.cardsvc = CardService(ctx)
        self.loans = LoanService(ctx)

    async def call(self, name: str, raw_args: dict) -> dict:
        """Always returns {ok, data|error}; never raises to the model."""
        started = time.perf_counter()
        model = ts.TOOL_MODELS.get(name)
        if model is None:
            return self._err("LLM_INVALID_TOOL_CALL", f"Unknown tool {name!r}.")
        try:
            args = model.model_validate(raw_args or {})
        except ValidationError:
            return self._err("LLM_INVALID_TOOL_CALL", "Tool arguments were not valid.")
        try:
            data = await getattr(self, f"_t_{name}")(args)
            result = {"ok": True, "data": data}
        except AppError as e:
            result = self._err(e.code, e.message, e.details)
        log.info("tool_executed", extra={"tool": name, "session_id": self.session.session_id,
                                         "duration_ms": round((time.perf_counter() - started) * 1000),
                                         "outcome": "ok" if result["ok"] else result["error"]["code"]})
        return result

    @staticmethod
    def _err(code: str, message: str, details: dict | None = None) -> dict:
        return {"ok": False, "error": {"code": code, "message": message, "details": details or {}}}

    def _need_auth(self) -> str:
        if not self.session.customer_id:
            raise AppError("AUTHENTICATION_REQUIRED", "The customer has not verified their identity yet.")
        return self.session.customer_id

    def _known_txn(self, transaction_id: str) -> None:
        # IDs must come from backend search results in this conversation, never from model invention.
        seen = self.state.get("known_txns", []) + self.state.get("listed_txns", [])
        if transaction_id not in seen:
            raise AppError("LLM_INVALID_TOOL_CALL", "Search for the transaction first.")

    def _known_card(self, card_id: str) -> None:
        if card_id not in self.state.get("known_cards", []):
            raise AppError("LLM_INVALID_TOOL_CALL", "List the customer's cards first.")

    async def _t_get_customer_context(self, _):
        cid = self._need_auth()
        c = await CustomerRepository(self.ctx.db, self.ctx.cipher).get(cid)
        # Data minimisation: the model only needs a first name to greet; it never gets the full name or any number.
        return {"customer_id": cid, "first_name": (c["display_name"].split() or [""])[0]}

    async def _t_search_customer_transactions(self, a: ts.SearchArgs):
        cid = self._need_auth()
        q = TransactionSearch(amount_paise=round(a.amount * 100) if a.amount else None, date_from=a.date_from,
                              date_to=a.date_to or a.date_from, transaction_type=a.transaction_type)
        res = await self.txns.search(cid, self.session.session_id, q)
        self.state["known_txns"] = [t.transaction_id for t in res.transactions]
        return {"count": res.count, "ambiguous": res.ambiguous,
                "transactions": [_txn_for_llm(t) for t in res.transactions]}

    async def _t_get_transaction_status(self, a: ts.TransactionIdArgs):
        cid = self._need_auth()
        self._known_txn(a.transaction_id)
        return _txn_for_llm(await self.txns.get(cid, a.transaction_id))

    async def _t_get_dispute_status(self, a: ts.DisputeIdArgs):
        self._need_auth()
        return (await self.disputes.track(self.session, a.dispute_id)).model_dump(mode="json")

    async def _t_prepare_dispute(self, a: ts.PrepareArgs):
        self._need_auth()
        self._known_txn(a.transaction_id)
        prep = await self.disputes.prepare(self.session, DisputePrepareRequest(
            transaction_id=a.transaction_id, category=a.issue_code, customer_explanation=a.customer_explanation))
        self.state["pending_prep"] = prep.preparation_id
        self.state.pop("pending_card", None)  # one pending action at a time: a bare "yes" must be unambiguous
        return prep.model_dump(mode="json")

    async def _t_confirm_dispute(self, a: ts.ConfirmArgs):
        self._need_auth()
        prep_id = self.state.get("pending_prep")
        # The model cannot self-confirm: this turn's customer words must be an explicit yes.
        if not prep_id or a.preparation_id != prep_id or not is_affirmative(self.user_text):
            raise AppError("CONFIRMATION_REQUIRED", "The customer has not clearly confirmed.")
        res = await self.disputes.confirm(self.session, DisputeConfirmRequest(
            preparation_id=prep_id, customer_confirmation=True,
            idempotency_key=sha256(f"{self.session.session_id}:{prep_id}")[-32:]))
        self.state.pop("pending_prep", None)
        self.state["last_dispute"] = res.dispute_id
        return res.model_dump(mode="json")

    async def _t_get_account_balance(self, _):
        self._need_auth()
        accounts = await self.accounts.balances(self.session)
        return {"count": len(accounts), "accounts": [
            {"account_type": a.account_type, "account_number_masked": a.account_number_masked,
             "last4": a.account_number_masked[-4:], "balance_text": a.balance_text,
             "available_text": a.available_text, "has_hold": a.available_paise != a.balance_paise}
            for a in accounts]}

    async def _t_get_mini_statement(self, a: ts.StatementArgs):
        self._need_auth()
        res = await self.accounts.mini_statement(self.session, a.count)
        # Listed, not "searched": they can be looked up or disputed later but never make a turn 'ambiguous'.
        self.state["listed_txns"] = [t.transaction_id for t in res.transactions]
        return {"count": res.count, "transactions": [_txn_for_llm(t) for t in res.transactions]}

    async def _t_list_cards(self, _):
        self._need_auth()
        cards = await self.cardsvc.list(self.session)
        self.state["known_cards"] = [c.card_id for c in cards]
        return {"count": len(cards), "cards": [c.model_dump(mode="json") for c in cards]}

    async def _t_prepare_card_block(self, a: ts.PrepareCardBlockArgs):
        self._need_auth()
        self._known_card(a.card_id)
        prep = await self.cardsvc.prepare_block(self.session, a.card_id, a.reason)
        self.state["pending_card"] = prep.preparation_id
        self.state.pop("pending_prep", None)
        return prep.model_dump(mode="json")

    async def _t_confirm_card_block(self, a: ts.ConfirmCardBlockArgs):
        self._need_auth()
        prep_id = self.state.get("pending_card")
        # The model cannot self-confirm: this turn's customer words must be an explicit yes.
        if not prep_id or a.preparation_id != prep_id or not is_affirmative(self.user_text):
            raise AppError("CONFIRMATION_REQUIRED", "The customer has not clearly confirmed.")
        res = await self.cardsvc.confirm_block(self.session, prep_id, True)
        self.state.pop("pending_card", None)
        self.state["last_card_block"] = res.card.card_id
        return res.model_dump(mode="json")

    async def _t_get_loan_summary(self, _):
        self._need_auth()
        loans = await self.loans.summary(self.session)
        return {"count": len(loans), "loans": [
            {**l.model_dump(mode="json"), "due_text": spoken_date(l.next_due_date)} for l in loans]}

    async def _t_request_human_handoff(self, a: ts.HandoffArgs):
        hid = await HandoffService(self.ctx).create(
            self.session, a.reason, a.summary,
            {"transactions": self.state.get("known_txns", []), "dispute": self.state.get("last_dispute"),
             "card_block": self.state.get("last_card_block")})
        self.state["handoff_id"] = hid
        return {"handoff_id": hid, "status": "QUEUED"}
