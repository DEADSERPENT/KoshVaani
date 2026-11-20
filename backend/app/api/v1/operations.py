import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Response, Request

from app.banking.privacy_service import PrivacyService
from app.banking.transaction_service import to_view
from app.context import AppContext
from app.core.errors import AppError
from app.banking.operator_service import OperatorContext, OperatorService
from app.dependencies import get_ctx
from app.infrastructure.metrics import percentile
from app.repositories.audit import AuditRepository
from pydantic import Field

from app.schemas.common import StrictModel

STAGES = ("asr", "llm", "tools", "tts_first_audio", "total")


async def current_operator(authorization: str | None = Header(default=None),
                           ctx: AppContext = Depends(get_ctx)) -> OperatorContext:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AppError("AUTHENTICATION_REQUIRED", "Operator sign-in required.")
    return await OperatorService(ctx).resolve(authorization[7:].strip())


def require_role(minimum: str):
    async def dep(op: OperatorContext = Depends(current_operator)) -> OperatorContext:
        if not op.allows(minimum):
            raise AppError("FORBIDDEN", "Your role does not allow this action.")
        return op
    return dep


router = APIRouter(prefix="/api/v1/ops")
auth_router = APIRouter(prefix="/api/v1/ops/auth")


class LoginRequest(StrictModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@auth_router.post("/login")
async def operator_login(body: LoginRequest, ctx: AppContext = Depends(get_ctx)):
    token, info = await OperatorService(ctx).login(body.username, body.password)
    return {"token": token, **info}


@auth_router.post("/logout")
async def operator_logout(authorization: str | None = Header(default=None), ctx: AppContext = Depends(get_ctx),
                          _: OperatorContext = Depends(current_operator)):
    await OperatorService(ctx).logout(authorization[7:].strip())
    return {"signed_out": True}


@auth_router.get("/me")
async def operator_me(op: OperatorContext = Depends(current_operator)):
    return {"operator_id": op.operator_id, "username": op.username, "role": op.role}


def _day_start(day: str | None = None) -> datetime:
    if day:
        try:
            return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise AppError("VALIDATION_ERROR", "date must be YYYY-MM-DD.") from exc
    n = datetime.now(timezone.utc)
    return n.replace(hour=0, minute=0, second=0, microsecond=0)


async def _all(cursor) -> list[dict]:
    return [d async for d in cursor]


async def compute_overview(ctx: AppContext, since: datetime) -> dict:
    db = ctx.db
    sessions = await _all(db.voice_sessions.find({"started_at": {"$gte": since}}, {"_id": 0}))
    audit = await _all(db.audit_events.find({"created_at": {"$gte": since}}, {"_id": 0}))
    handoffs = await _all(db.handoffs.find({"created_at": {"$gte": since}}, {"_id": 0}))
    turns = await _all(db.conversation_turns.find({"created_at": {"$gte": since}, "speaker": "CUSTOMER"}, {"_id": 0}))
    totals = [t["latency_ms"]["total"] for t in turns if t.get("latency_ms", {}).get("total") is not None]
    stage_avg = {}
    for st in STAGES:
        vals = [t["latency_ms"][st] for t in turns if st in t.get("latency_ms", {})]
        stage_avg[st] = round(sum(vals) / len(vals)) if vals else None
    outcomes = Counter(s.get("outcome") or "IN_PROGRESS" for s in sessions)
    act = Counter(a["action"] for a in audit if a["decision"] != "DENIED")
    return {
        "since": since,
        "sessions": len(sessions),
        "disputes_created": act["DISPUTE_CREATED"],
        "cards_blocked": act["CARD_BLOCKED"],
        "resolved_by_self_service": (outcomes["DISPUTE_CREATED"] + outcomes["CARD_BLOCKED"]
                                     + outcomes["INFORMATION_PROVIDED"]),
        "human_handoffs": len(handoffs),
        "provider_errors": sum(1 for a in audit if a["action"] == "PROVIDER_ERROR"),
        "blocked_policy_attempts": sum(1 for a in audit if a["action"] == "DISPUTE_PREPARED" and a["decision"] == "DENIED"),
        "ambiguous_matches": sum(1 for a in audit if a["action"] == "TRANSACTION_SEARCH"
                                 and a["reason_code"].startswith("RESULTS_") and int(a["reason_code"][8:] or 0) > 1),
        "latency_ms": {"p50": percentile(totals, 50), "p90": percentile(totals, 90), "p95": percentile(totals, 95),
                       "samples": len(totals), "stage_avg": stage_avg},
        "sessions_by_outcome": dict(outcomes),
        "handoff_reasons": dict(Counter(h["reason"] for h in handoffs)),
        "intents": dict(Counter(t["intent"] for t in turns if t.get("intent"))),
        "providers": {"llm": ctx.llm.name, "stt": ctx.stt.name, "tts": ctx.tts.name},
    }


@router.get("/overview")
async def overview(date: str | None = None, ctx: AppContext = Depends(get_ctx),
                   _: OperatorContext = Depends(require_role("VIEWER"))):
    return await compute_overview(ctx, _day_start(date))


@router.get("/metrics")
async def metrics(ctx: AppContext = Depends(get_ctx), _: OperatorContext = Depends(require_role("VIEWER"))):
    o = await compute_overview(ctx, _day_start())
    lines = [f"koshvaani_sessions_total {o['sessions']}", f"koshvaani_disputes_created_total {o['disputes_created']}",
             f"koshvaani_cards_blocked_total {o['cards_blocked']}",
             f"koshvaani_handoffs_total {o['human_handoffs']}", f"koshvaani_provider_errors_total {o['provider_errors']}",
             f"koshvaani_blocked_policy_attempts_total {o['blocked_policy_attempts']}"]
    for q in ("p50", "p90", "p95"):
        if o["latency_ms"][q] is not None:
            lines.append(f'koshvaani_turn_latency_ms{{quantile="{q}"}} {o["latency_ms"][q]}')
    for k, v in o["sessions_by_outcome"].items():
        lines.append(f'koshvaani_sessions_by_outcome{{outcome="{k}"}} {v}')
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@router.get("/sessions")
async def list_sessions(outcome: str | None = None, status: str | None = None, date: str | None = None,
                        limit: int = Query(default=50, ge=1, le=200), ctx: AppContext = Depends(get_ctx),
                        _: OperatorContext = Depends(require_role("VIEWER"))):
    filt: dict = {}
    if outcome:
        filt["outcome"] = outcome
    if status:
        filt["status"] = status
    if date:
        day = _day_start(date)
        filt["started_at"] = {"$gte": day, "$lt": day + timedelta(days=1)}
    rows = await _all(ctx.db.voice_sessions.find(filt, {"_id": 0}).sort("started_at", -1).limit(limit))
    for r in rows:
        r["turn_count"] = await ctx.db.conversation_turns.count_documents({"session_id": r["session_id"], "speaker": "CUSTOMER"})
    return {"sessions": rows, "count": len(rows)}


@router.get("/sessions/{session_id}")
async def session_detail(session_id: str, ctx: AppContext = Depends(get_ctx),
                         op: OperatorContext = Depends(require_role("AGENT"))):
    db = ctx.db
    session = await db.voice_sessions.find_one({"session_id": session_id}, {"_id": 0})
    if not session:
        raise AppError("TRANSACTION_NOT_FOUND", "Session not found.")
    turns = await _all(db.conversation_turns.find({"session_id": session_id}, {"_id": 0}).sort("turn_index", 1))
    audit = await _all(db.audit_events.find({"session_id": session_id}, {"_id": 0}).sort("created_at", 1))
    handoffs = await _all(db.handoffs.find({"session_id": session_id}, {"_id": 0}))
    dispute_ids = [a["resource_id"] for a in audit if a["action"] == "DISPUTE_CREATED"]
    disputes = await _all(db.disputes.find({"dispute_id": {"$in": dispute_ids}}, {"_id": 0, "idempotency_key_hash": 0}))
    raw = await ctx.redis.get(f"kv:conv:{session_id}")
    known = (json.loads(raw) if raw else {}).get("known_txns", [])
    txns = [to_view(t).model_dump(mode="json") async for t in db.transactions.find({"transaction_id": {"$in": known}}, {"_id": 0})]
    await AuditRepository(db).record(action="OPERATOR_VIEWED_SESSION", resource_type="SESSION", resource_id=session_id,
                                     decision="ALLOWED", reason_code="OPERATOR_READ", actor_type="OPERATOR", session_id=session_id,
                                     actor_id=op.operator_id)
    return {
        "session": session, "turns": turns, "disputes": disputes, "handoffs": handoffs, "matched_transactions": txns,
        "tool_timeline": [{"turn_index": t["turn_index"], **x} for t in turns for x in t.get("tools", [])],
        "latency_by_turn": [{"turn_index": t["turn_index"], **t["latency_ms"]} for t in turns if t.get("latency_ms")],
        "audit": audit,
    }


@router.get("/handoffs")
async def list_handoffs(status: str | None = None, limit: int = Query(default=50, ge=1, le=200),
                        ctx: AppContext = Depends(get_ctx), _: OperatorContext = Depends(require_role("AGENT"))):
    filt = {"status": status} if status else {}
    rows = await _all(ctx.db.handoffs.find(filt, {"_id": 0}).sort("created_at", -1).limit(limit))
    return {"handoffs": rows, "count": len(rows)}


class HandoffStatusUpdate(StrictModel):
    status: Literal["IN_PROGRESS", "RESOLVED"]


@router.post("/handoffs/{handoff_id}/status")
async def update_handoff(handoff_id: str, body: HandoffStatusUpdate, ctx: AppContext = Depends(get_ctx),
                         op: OperatorContext = Depends(require_role("AGENT"))):
    res = await ctx.db.handoffs.update_one({"handoff_id": handoff_id}, {"$set": {"status": body.status}})
    if res.matched_count == 0:
        raise AppError("TRANSACTION_NOT_FOUND", "Handoff not found.")
    await AuditRepository(ctx.db).record(action="HANDOFF_STATUS", resource_type="HANDOFF", resource_id=handoff_id,
                                         decision="ALLOWED", reason_code=body.status, actor_type="OPERATOR",
                                         actor_id=op.operator_id)
    return {"handoff_id": handoff_id, "status": body.status}


@router.post("/customers/{customer_id}/erase")
async def erase_customer(customer_id: str, ctx: AppContext = Depends(get_ctx),
                         op: OperatorContext = Depends(require_role("ADMIN"))):
    """Right to erasure. ADMIN only; keeps regulated banking records, removes personal data and call content."""
    return await PrivacyService(ctx).erase_customer(customer_id, op.operator_id)
