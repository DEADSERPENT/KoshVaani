"""Reset and reseed the configured database. Usage: python scripts/reset_demo_data.py --yes

Run from the repo root with the backend venv. Only the database named in MONGODB_DATABASE is touched.
With STORAGE_BACKEND=memory there is nothing persistent to reset (the app reseeds on every start).
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.banking.seed import reset  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core.crypto import FieldCipher  # noqa: E402
from app.infrastructure.mongodb import create_db  # noqa: E402


def cipher_for(s) -> FieldCipher:
    if not s.data_encryption_key:
        sys.exit("Set DATA_ENCRYPTION_KEY first: rows encrypted with a throwaway key could never be read again.")
    return FieldCipher(s.data_encryption_key)


async def main(yes: bool) -> None:
    s = get_settings()
    if s.is_production:
        sys.exit("Refusing to touch fixtures when APP_ENV=production.")
    if not yes and input(f"Drop and reseed '{s.mongodb_database}' ({s.storage_backend})? [y/N] ").lower() != "y":
        return
    await reset(create_db(s), cipher_for(s))
    print("Database reset and seeded.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--yes", action="store_true")
    asyncio.run(main(p.parse_args().yes))
