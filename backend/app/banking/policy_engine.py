from dataclasses import asdict, dataclass

from app.schemas.common import DisputeCategory as C
from app.schemas.common import TransactionStatus as T

POLICY_ID = "UPI_P2P_V1"
POLICY_VERSION = 1
# Synthetic timeline text: NOT a real RBI/NPCI turnaround. Bank policy owners must supply this.
TIMELINE_TEXT = {
    "STANDARD_TIMELINE": (
        "This is a synthetic policy. The case is reviewed by the operations team; "
        "no refund date is promised."
    ),
}

_ELIGIBLE_STATUSES: dict[C, set[T]] = {
    C.UPI_P2P_BENEFICIARY_NOT_CREDITED: {T.DEBITED_CREDIT_UNCONFIRMED},
    C.UPI_MERCHANT_PAYMENT_PENDING: {T.PENDING, T.DEBITED_CREDIT_UNCONFIRMED},
    C.UPI_DEBIT_WITH_FAILED_STATUS: {T.FAILED},
}


@dataclass
class PolicyDecision:
    eligible: bool
    reason_code: str
    required_confirmation: bool = True
    priority: str = "NORMAL"
    policy_id: str = POLICY_ID
    customer_message_key: str = ""

    def dict(self) -> dict:
        return asdict(self)


def evaluate(*, authenticated: bool, owns_transaction: bool, transaction: dict | None,
             category: C, has_active_dispute: bool) -> PolicyDecision:
    def no(code: str, key: str) -> PolicyDecision:
        return PolicyDecision(False, code, customer_message_key=key)

    if not authenticated:
        return no("NOT_AUTHENTICATED", "auth_required")
    if not owns_transaction or transaction is None:
        return no("TRANSACTION_NOT_OWNED", "txn_not_found")
    if transaction.get("channel") != "UPI":
        return no("NOT_A_UPI_TRANSACTION", "not_upi")
    if category in (C.UPI_UNKNOWN_TRANSACTION, C.OTHER_UPI_ISSUE):
        return no("HANDOFF_REQUIRED", "handoff_offered")  # high-risk / unsupported: human path
    status = T(transaction["status"])
    if status not in _ELIGIBLE_STATUSES.get(category, set()):
        return no("STATUS_NOT_ELIGIBLE", "upi_dispute_not_applicable")
    if category == C.UPI_DEBIT_WITH_FAILED_STATUS and transaction.get("recipient_status") == "REVERSED":
        return no("ALREADY_REVERSED", "upi_dispute_not_applicable")
    if has_active_dispute:
        return no("DISPUTE_ALREADY_EXISTS", "dispute_exists")
    return PolicyDecision(True, status.value, customer_message_key="upi_dispute_can_be_registered")
