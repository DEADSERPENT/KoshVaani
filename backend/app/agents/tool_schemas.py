from datetime import date

from pydantic import Field

from app.schemas.banking import CardBlockReason
from app.schemas.common import DisputeCategory, StrictModel


class NoArgs(StrictModel):
    pass


class SearchArgs(StrictModel):
    amount: float | None = Field(default=None, gt=0, description="Amount in rupees")
    date_from: date | None = None
    date_to: date | None = None
    transaction_type: str | None = Field(default=None, pattern="^(UPI|DEBIT|CREDIT)$")


class TransactionIdArgs(StrictModel):
    transaction_id: str = Field(min_length=3, max_length=40)


class DisputeIdArgs(StrictModel):
    dispute_id: str = Field(min_length=3, max_length=40)


class PrepareArgs(StrictModel):
    transaction_id: str = Field(min_length=3, max_length=40)
    issue_code: DisputeCategory
    customer_explanation: str = Field(default="", max_length=500)


class ConfirmArgs(StrictModel):
    preparation_id: str = Field(min_length=3, max_length=40)
    customer_confirmation: bool
    idempotency_key: str = Field(default="", max_length=100)  # ignored: the backend derives its own


class HandoffArgs(StrictModel):
    reason: str = Field(min_length=3, max_length=60)
    summary: str = Field(default="", max_length=500)


class StatementArgs(StrictModel):
    count: int = Field(default=5, ge=1, le=5, description="How many recent transactions to read out")


class PrepareCardBlockArgs(StrictModel):
    card_id: str = Field(min_length=3, max_length=40)
    reason: CardBlockReason


class ConfirmCardBlockArgs(StrictModel):
    preparation_id: str = Field(min_length=3, max_length=40)
    customer_confirmation: bool


TOOL_MODELS: dict[str, type[StrictModel]] = {
    "get_customer_context": NoArgs,
    "search_customer_transactions": SearchArgs,
    "get_transaction_status": TransactionIdArgs,
    "get_dispute_status": DisputeIdArgs,
    "prepare_dispute": PrepareArgs,
    "confirm_dispute": ConfirmArgs,
    "get_account_balance": NoArgs,
    "get_mini_statement": StatementArgs,
    "list_cards": NoArgs,
    "prepare_card_block": PrepareCardBlockArgs,
    "confirm_card_block": ConfirmCardBlockArgs,
    "get_loan_summary": NoArgs,
    "request_human_handoff": HandoffArgs,
}

TOOL_DESCRIPTIONS = {
    "get_customer_context": "Get the verified customer's name and masked phone.",
    "search_customer_transactions": "Search the verified customer's own transactions by amount (rupees) and/or date range.",
    "get_transaction_status": "Get the recorded status of one transaction returned by a previous search.",
    "get_dispute_status": "Get the recorded status of the customer's own dispute by its KV- reference.",
    "prepare_dispute": "Validate a dispute for a transaction returned by a previous search. Does not create it.",
    "confirm_dispute": "Create the prepared dispute. Only allowed after the customer clearly says yes.",
    "get_account_balance": "Get the verified customer's account balance and available balance.",
    "get_mini_statement": "Get the customer's most recent transactions across all channels (UPI, NEFT, IMPS, ATM, card).",
    "list_cards": "List the customer's cards (type, last four digits, status). Use before blocking a card.",
    "prepare_card_block": "Validate blocking a card returned by list_cards. Does not block it.",
    "confirm_card_block": "Block the prepared card. Only allowed after the customer clearly says yes.",
    "get_loan_summary": "Get the customer's loans: outstanding amount, EMI amount and next due date.",
    "request_human_handoff": "Transfer the customer to a human agent.",
}


def ollama_tools() -> list[dict]:
    return [{"type": "function", "function": {
        "name": name, "description": TOOL_DESCRIPTIONS[name],
        "parameters": model.model_json_schema()}} for name, model in TOOL_MODELS.items()]
