"""Seed the deterministic synthetic fixtures into a fresh database. Usage: python scripts/seed_demo_data.py

Refuses to run on a non-empty database (use scripts/reset_demo_data.py --yes to wipe and reseed).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.banking.seed import seed  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core.crypto import FieldCipher  # noqa: E402
from app.infrastructure.mongodb import create_db  # noqa: E402


def cipher_for(s) -> FieldCipher:
    if not s.data_encryption_key:
        sys.exit("Set DATA_ENCRYPTION_KEY first: rows encrypted with a throwaway key could never be read again.")
    return FieldCipher(s.data_encryption_key)


async def main() -> int:
    s = get_settings()
    if s.is_production:
        sys.exit("Refusing to touch fixtures when APP_ENV=production.")
    db = create_db(s)
    if await db.customers.count_documents({}):
        print(f"'{s.mongodb_database}' already has data. Use reset_demo_data.py --yes to wipe and reseed.")
        return 1
    await seed(db, cipher_for(s))
    print(f"Seeded {s.mongodb_database} ({s.storage_backend}).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
