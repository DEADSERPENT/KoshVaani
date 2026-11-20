"""Give customers a password to sign in with (AUTH_MODE=password). Passwords are stored only as scrypt hashes.

    # one customer, you choose the password (typed hidden, asked twice):
    backend\\.venv\\Scripts\\python.exe scripts\\set_customer_password.py --customer CUS-0001

    # every customer that has no password yet: random temporary passwords, written to a file for you to hand out;
    # each customer must choose their own password at first sign-in
    backend\\.venv\\Scripts\\python.exe scripts\\set_customer_password.py --all-missing --out data-import\\temp-passwords.csv

    # one customer, random temporary password printed once:
    backend\\.venv\\Scripts\\python.exe scripts\\set_customer_password.py --customer CUS-0001 --generate

The temporary-password file holds working credentials until each customer changes theirs: hand them out over a channel you
trust and delete the file. data-import\\*.csv is git-ignored.
"""
import argparse
import asyncio
import csv
import getpass
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.banking.operator_service import hash_password  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core.crypto import FieldCipher  # noqa: E402
from app.infrastructure.mongodb import create_db  # noqa: E402
from app.repositories.customers import CustomerRepository  # noqa: E402

ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no look-alikes (0/O, 1/l/I)


def temp_password() -> str:
    while True:
        groups = ["".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(3)]
        p = "-".join(groups)
        if any(c.isdigit() for c in p) and any(c.isalpha() for c in p):
            return p


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--customer", help="customer id, e.g. CUS-0001")
    ap.add_argument("--all-missing", action="store_true", help="every customer without a password")
    ap.add_argument("--generate", action="store_true", help="random temporary password (customer must change it)")
    ap.add_argument("--out", help="CSV for --all-missing / --generate: customer_id, phone, temporary_password")
    args = ap.parse_args()
    if bool(args.customer) == bool(args.all_missing):
        sys.exit("Choose exactly one of --customer or --all-missing.")
    s = get_settings()
    if s.storage_backend != "mongo":
        sys.exit("STORAGE_BACKEND must be 'mongo': this changes the real database.")
    if not s.data_encryption_key:
        sys.exit("Set DATA_ENCRYPTION_KEY first (needed to read phone numbers for the output file).")
    db = create_db(s)
    repo = CustomerRepository(db, FieldCipher(s.data_encryption_key))
    if args.customer:
        ids = [args.customer]
        if not await db.customers.find_one({"customer_id": args.customer}, {"_id": 1}):
            sys.exit(f"No customer {args.customer} in '{s.mongodb_database}'.")
    else:
        ids = [d["customer_id"] async for d in db.customers.find(
            {"status": "ACTIVE", "password_hash": {"$exists": False}}, {"customer_id": 1, "_id": 0}).sort("customer_id", 1)]
    if not ids:
        print("Nothing to do: every active customer already has a password.")
        return 0
    generate = args.generate or args.all_missing
    rows = []
    for cid in ids:
        if generate:
            pw, must_change = temp_password(), True
        else:
            pw = getpass.getpass(f"New password for {cid}: ")
            if pw != getpass.getpass("Repeat it: "):
                sys.exit("The two entries differ. Nothing changed.")
            if len(pw) < s.password_min_length or pw.isdigit():
                sys.exit(f"Use at least {s.password_min_length} characters, not only digits. Nothing changed.")
            must_change = False
        await repo.set_password(cid, await asyncio.to_thread(hash_password, pw), must_change)
        rows.append({"customer_id": cid, "phone": await repo.get_phone(cid) or "", "temporary_password": pw if generate else ""})
    print(f"Set a password for {len(rows)} customer(s) in '{s.mongodb_database}'.")
    if generate and args.out:
        with open(args.out, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["customer_id", "phone", "temporary_password"])
            w.writeheader()
            w.writerows(rows)
        print(f"Temporary passwords written to {args.out}. Hand them out securely, then delete the file.")
    elif generate:
        for r in rows:
            print(f"{r['customer_id']}  {r['phone']}  {r['temporary_password']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
