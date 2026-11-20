import hashlib
import hmac
import re
import secrets

_PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)")
_LONG_DIGITS = re.compile(r"(?<!\d)\d{9,18}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_SECRET = re.compile(r"\b(pin|cvv|otp|password)\b[^.!?]*?\b\d{3,8}\b", re.I)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_id(prefix: str, nbytes: int = 4) -> str:
    return f"{prefix}-{secrets.token_hex(nbytes).upper()}"


def sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def new_otp() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def hash_secret(value: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", value.encode(), salt.encode(), 10_000).hex()


def safe_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def redact(text: str) -> str:
    """Redact spoken secrets, emails, phone numbers and long digit runs (UTR/account)."""
    text = _SECRET.sub("[REDACTED-SECRET]", text)
    text = _EMAIL.sub("[EMAIL]", text)
    text = _PHONE.sub("[PHONE]", text)
    text = _LONG_DIGITS.sub("[NUMBER]", text)
    return text


def mask_tail(value: str, keep: int = 4) -> str:
    return "*" * max(len(value) - keep, 0) + value[-keep:]
