from fastapi import Request
from fastapi.responses import JSONResponse

# code -> (http status, retryable)
ERROR_CODES: dict[str, tuple[int, bool]] = {
    "VALIDATION_ERROR": (422, False),
    "SESSION_EXPIRED": (401, False),
    "AUTHENTICATION_REQUIRED": (401, False),
    "AUTHENTICATION_FAILED": (401, False),
    "CUSTOMER_NOT_FOUND": (404, False),
    "TRANSACTION_NOT_FOUND": (404, False),
    "TRANSACTION_AMBIGUOUS": (409, False),
    "DISPUTE_NOT_FOUND": (404, False),
    "CARD_NOT_FOUND": (404, False),
    "CARD_NOT_ELIGIBLE": (409, False),
    "DISPUTE_NOT_ELIGIBLE": (409, False),
    "CONFIRMATION_REQUIRED": (409, False),
    "IDEMPOTENCY_CONFLICT": (409, False),
    "PROVIDER_TIMEOUT": (504, True),
    "PROVIDER_UNAVAILABLE": (503, True),
    "LLM_INVALID_TOOL_CALL": (422, False),
    "POLICY_REJECTED": (403, False),
    "FORBIDDEN": (403, False),
    "RATE_LIMITED": (429, True),
    "STORE_UNAVAILABLE": (503, True),
    "INTERNAL_ERROR": (500, False),
}


class AppError(Exception):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.status_code, self.retryable = ERROR_CODES.get(code, (500, False))

    def envelope(self, request_id: str | None = None) -> dict:
        body = {
            "code": self.code,
            "message": self.message,
            "request_id": request_id,
            "retryable": self.retryable,
        }
        if self.details:  # non-sensitive hints for the UI, e.g. reason_code, handoff_recommended
            body["details"] = self.details
        return {"error": body}


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    rid = getattr(request.state, "request_id", None)
    return JSONResponse(status_code=exc.status_code, content=exc.envelope(rid))
