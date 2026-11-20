from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Request models reject unexpected fields (also used for LLM tool arguments)."""

    model_config = ConfigDict(extra="forbid")


class TransactionStatus(str, Enum):
    INITIATED = "INITIATED"
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    REVERSED = "REVERSED"
    DEBITED_CREDIT_UNCONFIRMED = "DEBITED_CREDIT_UNCONFIRMED"
    UNKNOWN = "UNKNOWN"


class DisputeCategory(str, Enum):
    UPI_P2P_BENEFICIARY_NOT_CREDITED = "UPI_P2P_BENEFICIARY_NOT_CREDITED"
    UPI_MERCHANT_PAYMENT_PENDING = "UPI_MERCHANT_PAYMENT_PENDING"
    UPI_DEBIT_WITH_FAILED_STATUS = "UPI_DEBIT_WITH_FAILED_STATUS"
    UPI_UNKNOWN_TRANSACTION = "UPI_UNKNOWN_TRANSACTION"
    OTHER_UPI_ISSUE = "OTHER_UPI_ISSUE"


class DisputeStatus(str, Enum):
    DRAFT = "DRAFT"
    OPEN = "OPEN"
    UNDER_REVIEW = "UNDER_REVIEW"
    WAITING_FOR_BANK_RAILS = "WAITING_FOR_BANK_RAILS"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"
    CLOSED = "CLOSED"


ACTIVE_DISPUTE_STATUSES = {
    DisputeStatus.OPEN,
    DisputeStatus.UNDER_REVIEW,
    DisputeStatus.WAITING_FOR_BANK_RAILS,
}


def as_utc(dt: datetime) -> datetime:
    """Mongo drivers/mocks may hand back naive datetimes; treat them as UTC."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
