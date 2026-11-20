import asyncio
import hashlib
import json
import weakref
from datetime import timezone

from pymongo.errors import DuplicateKeyError

from app.core.security import new_id
from app.infrastructure.mongodb import utcnow

GENESIS = "0" * 64
# One writer at a time per process: without it, concurrent writers race to advance the chain head and retry against
# each other (measured: session creation took 2+ s at 25 callers). The compare-and-set below still protects against
# several processes writing at once.
_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    if loop not in _locks:
        _locks[loop] = asyncio.Lock()
    return _locks[loop]
_FIELDS = ("event_id", "session_id", "actor_type", "actor_id", "action", "resource_type", "resource_id",
           "decision", "reason_code", "policy_id")


def _ts(dt) -> str:
    """Canonical UTC timestamp to the millisecond: MongoDB stores milliseconds and may return naive datetimes,
    so the hash must not depend on microseconds or tzinfo or it would never verify after a round trip."""
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def event_hash(prev_hash: str, seq: int, event: dict) -> str:
    """SHA-256 over the previous hash, the sequence number and the event's content, so changing, deleting or
    re-ordering any past event breaks every hash after it (verified by scripts/verify_audit_chain.py)."""
    body = {k: event.get(k) for k in _FIELDS} | {"created_at": _ts(event["created_at"])}
    return hashlib.sha256((prev_hash + str(seq) + json.dumps(body, sort_keys=True, default=str)).encode()).hexdigest()


class AuditRepository:
    """Append-only and hash-chained: no update/delete methods are exposed, and tampering is detectable."""

    def __init__(self, db):
        self.col = db.audit_events
        self.head = db.audit_chain

    async def record(self, *, action: str, resource_type: str, resource_id: str, decision: str,
                     reason_code: str, session_id: str | None = None, actor_type: str = "AI_AGENT",
                     actor_id: str = "KOSHVAANI_ORCHESTRATOR", policy_id: str | None = None) -> str:
        event_id = new_id("AUD")
        event = {
            "event_id": event_id, "session_id": session_id, "actor_type": actor_type,
            "actor_id": actor_id, "action": action, "resource_type": resource_type,
            "resource_id": resource_id, "decision": decision, "reason_code": reason_code,
            "policy_id": policy_id, "created_at": utcnow(),
        }
        async with _lock():
            return await self._append(event, event_id)

    async def _append(self, event: dict, event_id: str) -> str:
        for _ in range(200):  # compare-and-set on the chain head: concurrent writers retry instead of forking the chain
            head = await self.head.find_one({"_id": "head"})
            seq, prev = (head["seq"] + 1, head["hash"]) if head else (1, GENESIS)
            digest = event_hash(prev, seq, event)
            if head:
                won = (await self.head.update_one({"_id": "head", "seq": head["seq"]},
                                                  {"$set": {"seq": seq, "hash": digest}})).matched_count == 1
            else:
                try:
                    await self.head.insert_one({"_id": "head", "seq": seq, "hash": digest})
                    won = True
                except DuplicateKeyError:
                    won = False
            if won:
                await self.col.insert_one({**event, "seq": seq, "prev_hash": prev, "hash": digest})
                return event_id
        raise RuntimeError("audit chain contention")

    async def for_resource(self, resource_id: str) -> list[dict]:
        return [d async for d in self.col.find({"resource_id": resource_id}, {"_id": 0}).sort("created_at", -1)]

    async def verify_chain(self) -> dict:
        """Recompute every hash from the first chained event. Returns the first problem found, if any."""
        prev, expected, checked = GENESIS, 1, 0
        async for e in self.col.find({"seq": {"$exists": True}}, {"_id": 0}).sort("seq", 1):
            if e["seq"] != expected:
                return {"ok": False, "checked": checked, "problem": f"missing or reordered event at seq {expected}"}
            if e["prev_hash"] != prev or e["hash"] != event_hash(prev, e["seq"], e):
                return {"ok": False, "checked": checked, "problem": f"event {e['event_id']} (seq {e['seq']}) was altered"}
            prev, expected, checked = e["hash"], expected + 1, checked + 1
        head = await self.head.find_one({"_id": "head"})
        if head and (head["seq"] != checked or head["hash"] != prev):
            return {"ok": False, "checked": checked, "problem": "events were removed from the end of the chain"}
        return {"ok": True, "checked": checked, "problem": None}
