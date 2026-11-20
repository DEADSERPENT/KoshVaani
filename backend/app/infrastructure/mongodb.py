from datetime import datetime, timezone

from pymongo import ASCENDING, DESCENDING

from app.config import Settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_db(settings: Settings):
    if settings.storage_backend == "mongo":
        from motor.motor_asyncio import AsyncIOMotorClient

        # A warm pool: opening many connections at once during the first burst of callers measured about 2 s per request.
        client = AsyncIOMotorClient(settings.mongodb_uri, tz_aware=True, serverSelectionTimeoutMS=3000,
                                    minPoolSize=settings.mongodb_min_pool, maxPoolSize=settings.mongodb_max_pool)
        return client[settings.mongodb_database]
    from mongomock_motor import AsyncMongoMockClient

    return AsyncMongoMockClient()[settings.mongodb_database]


async def ensure_indexes(db, retention_days: int = 7, session_retention_days: int = 90) -> None:
    await db.customers.create_index("customer_id", unique=True)
    await db.customers.create_index("phone_hash", unique=True)
    await db.accounts.create_index("account_id", unique=True)
    await db.accounts.create_index("customer_id")
    await db.transactions.create_index("transaction_id", unique=True)
    await db.transactions.create_index([("customer_id", ASCENDING), ("transaction_at", DESCENDING)])
    await db.transactions.create_index([("customer_id", ASCENDING), ("amount_paise", ASCENDING)])
    await db.cards.create_index("card_id", unique=True)
    await db.cards.create_index("customer_id")
    await db.loans.create_index("loan_id", unique=True)
    await db.loans.create_index("customer_id")
    await db.disputes.create_index("dispute_id", unique=True)
    await db.disputes.create_index([("customer_id", ASCENDING), ("created_at", DESCENDING)])
    # Final guard against duplicate active disputes for the same transaction + category.
    await db.disputes.create_index(
        [("transaction_id", ASCENDING), ("category", ASCENDING)],
        unique=True,
        partialFilterExpression={"active": True},
    )
    await db.voice_sessions.create_index("session_id", unique=True)
    await db.voice_sessions.create_index("started_at", expireAfterSeconds=session_retention_days * 86400)
    await db.conversation_turns.create_index(
        [("session_id", ASCENDING), ("turn_index", ASCENDING)], unique=True
    )
    # Transcript retention: Mongo expires turns automatically (audit events are kept).
    await db.conversation_turns.create_index("created_at", expireAfterSeconds=retention_days * 86400)
    await db.audit_events.create_index([("resource_id", ASCENDING), ("created_at", DESCENDING)])
    await db.audit_events.create_index("seq", unique=True, partialFilterExpression={"seq": {"$exists": True}})
    await db.handoffs.create_index("handoff_id", unique=True)
    await db.operators.create_index("username", unique=True)
