from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field

from app.schemas.common import StrictModel


class AccountView(BaseModel):
    """Masked: the full account number never leaves the service boundary."""

    account_id: str
    account_type: str
    account_number_masked: str
    currency: str
    status: str
    balance_paise: int
    available_paise: int
    balance_text: str
    available_text: str


class CardType(str, Enum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class CardStatus(str, Enum):
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    EXPIRED = "EXPIRED"


class CardBlockReason(str, Enum):
    LOST = "LOST"
    STOLEN = "STOLEN"
    SUSPECTED_FRAUD = "SUSPECTED_FRAUD"
    OTHER = "OTHER"


class CardView(BaseModel):
    card_id: str
    card_type: CardType
    network: str
    last4: str
    status: CardStatus
    blocked_reason: str | None = None


class CardBlockPrepareRequest(StrictModel):
    card_id: str = Field(min_length=3, max_length=40)
    reason: CardBlockReason


class CardBlockPrepareResponse(BaseModel):
    preparation_id: str
    card: CardView
    confirmation_text: str
    expires_in_seconds: int


class CardBlockConfirmRequest(StrictModel):
    preparation_id: str = Field(min_length=3, max_length=40)
    customer_confirmation: bool


class CardBlockConfirmResponse(BaseModel):
    blocked: bool  # False when the card was already blocked (repeat request)
    card: CardView
    message: str


class LoanView(BaseModel):
    loan_id: str
    loan_type: str
    status: str
    outstanding_paise: int
    emi_paise: int
    next_due_date: date
    emis_remaining: int
    outstanding_text: str
    emi_text: str
    updated_at: datetime
