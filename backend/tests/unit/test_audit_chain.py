import asyncio

import pytest
from mongomock_motor import AsyncMongoMockClient

from app.repositories.audit import AuditRepository


@pytest.fixture
def db():
    return AsyncMongoMockClient()["t"]


async def add(repo, n, prefix="A"):
    for i in range(n):
        await repo.record(action=f"{prefix}{i}", resource_type="X", resource_id=f"R{i}", decision="ALLOWED", reason_code="OK")


async def test_clean_chain_verifies(db):
    repo = AuditRepository(db)
    await add(repo, 5)
    assert await repo.verify_chain() == {"ok": True, "checked": 5, "problem": None}


async def test_empty_chain_verifies(db):
    assert (await AuditRepository(db).verify_chain())["ok"]


async def test_editing_an_event_is_detected(db):
    repo = AuditRepository(db)
    await add(repo, 4)
    await db.audit_events.update_one({"seq": 2}, {"$set": {"decision": "DENIED"}})
    r = await repo.verify_chain()
    assert not r["ok"] and "seq 2" in r["problem"] and "altered" in r["problem"]


async def test_deleting_a_middle_event_is_detected(db):
    repo = AuditRepository(db)
    await add(repo, 4)
    await db.audit_events.delete_one({"seq": 3})
    r = await repo.verify_chain()
    assert not r["ok"] and "seq 3" in r["problem"]


async def test_deleting_the_last_event_is_detected(db):
    repo = AuditRepository(db)
    await add(repo, 4)
    await db.audit_events.delete_one({"seq": 4})
    r = await repo.verify_chain()
    assert not r["ok"] and "removed from the end" in r["problem"]


async def test_concurrent_writers_do_not_fork_the_chain(db):
    repo = AuditRepository(db)
    await asyncio.gather(*(add(repo, 10, prefix=f"W{w}-") for w in range(8)))
    r = await repo.verify_chain()
    assert r == {"ok": True, "checked": 80, "problem": None}
