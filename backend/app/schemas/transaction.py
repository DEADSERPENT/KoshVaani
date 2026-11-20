from datetime import date, datetime

from pydantic import BaseModel, Field

from app.schemas.common import StrictModel, TransactionStatus


class TransactionSearch(StrictModel):
    amount_paise: int | None = Field(default=None, gt=0)
    date_from: date | None = None
    date_to: date | None = None
    transaction_type: str | None = Field(default=None, pattern="^(UPI|DEBIT|CREDIT)$")
    limit: int | None = Field(default=None, ge=1, le=20)  # newest first; None means the repository default


class TransactionView(BaseModel):
    """What leaves the service boundary: masked, no internal ids or full UTR."""

    transaction_id: str
    channel: str
    direction: str
    amount_paise: int
    currency: str
    transaction_at: datetime
    status: TransactionStatus
    recipient_status: str
    payee_label: str
    utr_masked: str


class TransactionSearchResult(BaseModel):
    transactions: list[TransactionView]
    count: int
    ambiguous: bool
