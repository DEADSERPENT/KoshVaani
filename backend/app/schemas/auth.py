import re

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import StrictModel


class SessionCreateRequest(StrictModel):
    channel: str = "WEB_VOICE"
    language: str = "en-IN"
    consent_microphone: bool = False
    consent_notice: bool = True


class SessionCreateResponse(BaseModel):
    session_id: str
    session_token: str
    status: str
    language: str
    simulated: bool


def normalize_indian_mobile(raw: str) -> str:
    """Return +91XXXXXXXXXX for 10-digit mobiles (optional 0 / 91 / +91 prefix, spaces and dashes allowed)."""
    digits = re.sub(r"[\s\-()]", "", raw)
    m = re.fullmatch(r"(?:\+?91|0)?([6-9]\d{9})", digits)
    if not m:
        raise ValueError("Enter a valid 10-digit Indian mobile number.")
    return "+91" + m.group(1)


class ChallengeRequest(StrictModel):
    phone: str = Field(min_length=10, max_length=18)
    channel: str = "WEB_APP"

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        return normalize_indian_mobile(v)


class ChallengeResponse(BaseModel):
    challenge_id: str
    delivery_mode: str
    expires_in_seconds: int
    message: str
    dev_code: str | None = None  # populated only when ECHO_DEV_OTP is on, for the separate verification panel


class VerifyRequest(StrictModel):
    code: str = Field(min_length=4, max_length=8, pattern=r"^\d+$")


class VerifyResponse(BaseModel):
    authenticated: bool
    customer_display_name: str
    phone_masked: str


class LoginRequest(StrictModel):
    phone: str = Field(min_length=10, max_length=18)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        return normalize_indian_mobile(v)


class LoginResponse(BaseModel):
    authenticated: bool
    customer_display_name: str
    phone_masked: str
    must_change_password: bool  # true: nothing else works until the password is changed


class PasswordChangeRequest(StrictModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


class HandoffRequest(StrictModel):
    reason: str = Field(min_length=3, max_length=60)
    summary: str = Field(default="", max_length=500)


class HandoffResponse(BaseModel):
    handoff_id: str
    status: str
