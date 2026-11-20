from datetime import date, datetime

from pydantic import BaseModel, Field

from app.schemas.common import DisputeCategory, DisputeStatus, StrictModel, TransactionStatus


class DisputePrepareRequest(StrictModel):
    transaction_id: str = Field(min_length=3, max_length=40)
    category: DisputeCategory
    customer_explanation: str = Field(default="", max_length=500)


class TransactionSummary(BaseModel):
    amount_paise: int
    currency: str
    transaction_date: date
    status: TransactionStatus


class DisputePrepareResponse(BaseModel):
    preparation_id: str
    eligible: bool
    reason_code: str
    transaction_summary: TransactionSummary
    confirmation_text: str
    expires_in_seconds: int


class DisputeConfirmRequest(StrictModel):
    preparation_id: str = Field(min_length=3, max_length=40)
    customer_confirmation: bool
    idempotency_key: str = Field(min_length=8, max_length=100)


class DisputeConfirmResponse(BaseModel):
    created: bool
    dispute_id: str
    status: DisputeStatus
    message: str


class DisputeView(BaseModel):
    dispute_id: str
    transaction_id: str
    category: DisputeCategory
    status: DisputeStatus
    priority: str
    policy_id: str
    timeline_message: str
    created_at: datetime
    updated_at: datetime
