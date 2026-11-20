"""Verify the tamper-evident audit trail of the configured database. Usage (repo root, backend venv):

    backend\\.venv\\Scripts\\python.exe scripts\\verify_audit_chain.py

Recomputes every event hash from the first chained event. Exits 1 and names the first altered, missing or reordered
event if the chain does not verify. Run it on a schedule and after any incident; it only reads.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import get_settings  # noqa: E402
from app.infrastructure.mongodb import create_db  # noqa: E402
from app.repositories.audit import AuditRepository  # noqa: E402


async def main() -> int:
    s = get_settings()
    result = await AuditRepository(create_db(s)).verify_chain()
    if result["ok"]:
        print(f"Audit chain OK: {result['checked']} events verified in '{s.mongodb_database}'.")
        return 0
    print(f"AUDIT CHAIN BROKEN after {result['checked']} verified events: {result['problem']}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
