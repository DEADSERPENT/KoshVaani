import base64
import hashlib
import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PREFIX = "enc1:"


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def generate_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


class FieldCipher:
    """Field-level encryption for personal data at rest (AES-256-GCM) plus a keyed blind index for lookups.

    One master key is split with HKDF into an encryption key and an index key, so a leaked index never helps
    decrypt anything. Each value is bound to its field label (AAD): a name ciphertext cannot be pasted into a
    phone field. The blind index is HMAC-SHA256, so equal inputs match but the number cannot be recovered
    without the key (a bare hash of a 10-digit phone number could be brute-forced in seconds)."""

    def __init__(self, master_key: str):
        raw = _b64d(master_key)
        if len(raw) != 32:
            raise ValueError("DATA_ENCRYPTION_KEY must be 32 random bytes, base64url encoded.")
        self._aead = AESGCM(self._derive(raw, b"kv-field-encryption"))
        self._index_key = self._derive(raw, b"kv-blind-index")

    @staticmethod
    def _derive(raw: bytes, info: bytes) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(raw)

    def encrypt(self, value: str, field: str) -> str:
        nonce = os.urandom(12)
        ct = self._aead.encrypt(nonce, value.encode(), field.encode())
        return PREFIX + base64.urlsafe_b64encode(nonce + ct).decode().rstrip("=")

    def decrypt(self, token: str, field: str) -> str:
        if not token.startswith(PREFIX):
            raise ValueError("Value is not encrypted with this scheme.")
        blob = _b64d(token[len(PREFIX):])
        try:
            return self._aead.decrypt(blob[:12], blob[12:], field.encode()).decode()
        except InvalidTag as exc:
            raise ValueError("Could not decrypt: wrong key or tampered value.") from exc

    def blind_index(self, value: str, field: str) -> str:
        return hmac.new(self._index_key, f"{field}:{value}".encode(), hashlib.sha256).hexdigest()
