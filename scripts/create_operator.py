"""Create an operator account for the ops console. Usage: python scripts/create_operator.py <username> [VIEWER|AGENT|ADMIN]

The password is prompted for (never passed on the command line or stored in shell history)."""
import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.banking.operator_service import OperatorService  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.context import AppContext  # noqa: E402
from app.infrastructure.mongodb import create_db, ensure_indexes  # noqa: E402
from app.infrastructure.redis import create_redis  # noqa: E402


async def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    username, role = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "AGENT")
    s = get_settings()
    if s.storage_backend != "mongo":
        print("STORAGE_BACKEND is 'memory': an account created here would vanish when this script exits.\n"
              "Set STORAGE_BACKEND=mongo, or use BOOTSTRAP_OPERATOR_* for local development.")
        return 1
    password = getpass.getpass("Password (min 12 chars): ")
    if password != getpass.getpass("Repeat password: "):
        print("Passwords do not match.")
        return 1
    db = create_db(s)
    await ensure_indexes(db, s.transcript_retention_days)
    try:
        op_id = await OperatorService(AppContext(settings=s, db=db, redis=create_redis(s))).create(username, password, role)
    except ValueError as e:
        print(e)
        return 1
    print(f"Created {role} operator {username} ({op_id}).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
