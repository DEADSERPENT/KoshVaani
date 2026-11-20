from app.infrastructure.mongodb import utcnow
from app.core.crypto import FieldCipher


class CustomerRepository:
    """Personal fields (name, phone, email) are encrypted at rest. Phone lookup goes through a keyed blind
    index, so the database never holds a searchable plaintext number. Reads return only what callers need:
    the decrypted display name and the masked phone. Full phone/email are decrypted only on explicit request."""

    def __init__(self, db, cipher: FieldCipher):
        self.col = db.customers
        self.cipher = cipher

    async def create(self, *, customer_id: str, display_name: str, phone_e164: str, email: str, status: str,
                     created_at) -> None:
        c = self.cipher
        await self.col.insert_one({
            "customer_id": customer_id, "status": status, "created_at": created_at, "updated_at": created_at,
            "name_enc": c.encrypt(display_name, "customer.name"),
            "phone_enc": c.encrypt(phone_e164, "customer.phone"),
            "phone_hash": c.blind_index(phone_e164, "customer.phone"),
            "phone_masked": "******" + phone_e164[-4:],
            "email_enc": c.encrypt(email, "customer.email"),
        })

    def _view(self, doc: dict | None) -> dict | None:
        if not doc:
            return None
        return {"customer_id": doc["customer_id"], "status": doc["status"], "phone_masked": doc["phone_masked"],
                "display_name": self.cipher.decrypt(doc["name_enc"], "customer.name") if doc.get("name_enc") else ""}

    async def credentials_by_phone(self, phone_e164: str) -> dict | None:
        """Login lookup: id, status and password hash only. The hash never leaves the authentication service."""
        h = self.cipher.blind_index(phone_e164, "customer.phone")
        return await self.col.find_one({"phone_hash": h}, {"_id": 0, "customer_id": 1, "status": 1,
                                                          "password_hash": 1, "must_change_password": 1})

    async def credentials(self, customer_id: str) -> dict | None:
        return await self.col.find_one({"customer_id": customer_id}, {"_id": 0, "customer_id": 1, "status": 1,
                                                                     "password_hash": 1, "must_change_password": 1})

    async def set_password(self, customer_id: str, password_hash: str, must_change: bool) -> bool:
        now = utcnow()
        res = await self.col.update_one({"customer_id": customer_id}, {"$set": {
            "password_hash": password_hash, "must_change_password": must_change,
            "password_set_at": now, "updated_at": now}})
        return res.matched_count == 1

    async def get(self, customer_id: str) -> dict | None:
        return self._view(await self.col.find_one({"customer_id": customer_id}, {"_id": 0}))

    async def get_by_phone(self, phone_e164: str) -> dict | None:
        h = self.cipher.blind_index(phone_e164, "customer.phone")
        return self._view(await self.col.find_one({"phone_hash": h}, {"_id": 0}))

    async def get_phone(self, customer_id: str) -> str | None:
        """Full number, for the OTP sender only."""
        doc = await self.col.find_one({"customer_id": customer_id}, {"_id": 0, "phone_enc": 1})
        return self.cipher.decrypt(doc["phone_enc"], "customer.phone") if doc and doc.get("phone_enc") else None
