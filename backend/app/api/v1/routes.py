from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field

from app.core.errors import AppError
from app.repositories.audit import AuditRepository
from app.repositories.sessions import VoiceSessionRepository
from app.schemas.common import StrictModel
from app.voice.conversation_orchestrator import ConversationOrchestrator

from app.banking.account_service import AccountService
from app.banking.authentication_service import AuthenticationService
from app.banking.card_service import CardService
from app.banking.dispute_service import DisputeService
from app.banking.handoff_service import HandoffService
from app.banking.loan_service import LoanService
from app.banking.session_service import SessionContext, SessionService
from app.banking.transaction_service import TransactionService
from app.context import AppContext
from app.dependencies import get_authenticated, get_ctx, get_session, get_session_service
from app.schemas.auth import (
    ChallengeRequest, ChallengeResponse, HandoffRequest, HandoffResponse, LoginRequest, LoginResponse,
    PasswordChangeRequest, SessionCreateRequest, SessionCreateResponse, VerifyRequest, VerifyResponse,
)
from app.schemas.banking import (
    AccountView, CardBlockConfirmRequest, CardBlockConfirmResponse, CardBlockPrepareRequest,
    CardBlockPrepareResponse, CardView, LoanView,
)
from app.schemas.dispute import (
    DisputeConfirmRequest, DisputeConfirmResponse, DisputePrepareRequest, DisputePrepareResponse, DisputeView,
)
from app.schemas.transaction import TransactionSearch, TransactionSearchResult, TransactionView

router = APIRouter(prefix="/api/v1")


@router.post("/sessions", response_model=SessionCreateResponse, status_code=201)
async def create_session(body: SessionCreateRequest, ctx: AppContext = Depends(get_ctx),
                         sessions: SessionService = Depends(get_session_service)):
    if not body.consent_notice:
        raise AppError("POLICY_REJECTED", "Consent to the privacy notice is required to start a call.",
                       details={"reason_code": "NOTICE_CONSENT_MISSING"})
    sid, token = await sessions.create(body.channel, body.language, body.consent_microphone)
    return SessionCreateResponse(session_id=sid, session_token=token, status="CREATED",
                                 language=body.language,
                                 simulated=ctx.settings.storage_backend == "memory")


@router.post("/auth/challenges", response_model=ChallengeResponse)
async def start_challenge(body: ChallengeRequest, request: Request, session: SessionContext = Depends(get_session),
                          ctx: AppContext = Depends(get_ctx),
                          sessions: SessionService = Depends(get_session_service)):
    client_ip = request.client.host if request.client else "unknown"
    return await AuthenticationService(ctx, sessions).start(session.session_id, body.phone, client_ip)


@router.post("/auth/login", response_model=LoginResponse)
async def password_login(body: LoginRequest, request: Request, session: SessionContext = Depends(get_session),
                         ctx: AppContext = Depends(get_ctx),
                         sessions: SessionService = Depends(get_session_service)):
    client_ip = request.client.host if request.client else "unknown"
    return await AuthenticationService(ctx, sessions).login(session.session_id, body.phone, body.password, client_ip)


@router.post("/auth/password")
async def change_password(body: PasswordChangeRequest, session: SessionContext = Depends(get_session),
                          ctx: AppContext = Depends(get_ctx),
                          sessions: SessionService = Depends(get_session_service)):
    return await AuthenticationService(ctx, sessions).change_password(session, body.current_password, body.new_password)


@router.post("/auth/challenges/{challenge_id}/verify", response_model=VerifyResponse)
async def verify_challenge(challenge_id: str, body: VerifyRequest, session: SessionContext = Depends(get_session),
                           ctx: AppContext = Depends(get_ctx),
                           sessions: SessionService = Depends(get_session_service)):
    return await AuthenticationService(ctx, sessions).verify(session.session_id, challenge_id, body.code)


@router.get("/transactions", response_model=TransactionSearchResult)
async def search_transactions(
    amount_paise: int | None = Query(default=None, gt=0), date_from: date | None = None,
    date_to: date | None = None, transaction_type: str | None = Query(default=None, pattern="^(UPI|DEBIT|CREDIT)$"),
    limit: int | None = Query(default=None, ge=1, le=20),
    session: SessionContext = Depends(get_authenticated), ctx: AppContext = Depends(get_ctx),
):
    q = TransactionSearch(amount_paise=amount_paise, date_from=date_from, date_to=date_to,
                          transaction_type=transaction_type, limit=limit)
    return await TransactionService(ctx.db).search(session.customer_id, session.session_id, q)


@router.get("/transactions/{transaction_id}", response_model=TransactionView)
async def get_transaction(transaction_id: str, session: SessionContext = Depends(get_authenticated),
                          ctx: AppContext = Depends(get_ctx)):
    return await TransactionService(ctx.db).get(session.customer_id, transaction_id)


@router.get("/accounts", response_model=list[AccountView])
async def list_accounts(session: SessionContext = Depends(get_authenticated), ctx: AppContext = Depends(get_ctx)):
    return await AccountService(ctx).balances(session)


@router.get("/cards", response_model=list[CardView])
async def list_cards(session: SessionContext = Depends(get_authenticated), ctx: AppContext = Depends(get_ctx)):
    return await CardService(ctx).list(session)


@router.post("/cards/block/prepare", response_model=CardBlockPrepareResponse)
async def prepare_card_block(body: CardBlockPrepareRequest, session: SessionContext = Depends(get_authenticated),
                             ctx: AppContext = Depends(get_ctx)):
    return await CardService(ctx).prepare_block(session, body.card_id, body.reason)


@router.post("/cards/block/confirm", response_model=CardBlockConfirmResponse)
async def confirm_card_block(body: CardBlockConfirmRequest, session: SessionContext = Depends(get_authenticated),
                             ctx: AppContext = Depends(get_ctx)):
    return await CardService(ctx).confirm_block(session, body.preparation_id, body.customer_confirmation)


@router.get("/loans", response_model=list[LoanView])
async def list_loans(session: SessionContext = Depends(get_authenticated), ctx: AppContext = Depends(get_ctx)):
    return await LoanService(ctx).summary(session)


@router.post("/disputes/prepare", response_model=DisputePrepareResponse)
async def prepare_dispute(body: DisputePrepareRequest, session: SessionContext = Depends(get_authenticated),
                          ctx: AppContext = Depends(get_ctx)):
    return await DisputeService(ctx).prepare(session, body)


@router.post("/disputes/confirm", response_model=DisputeConfirmResponse)
async def confirm_dispute(body: DisputeConfirmRequest, session: SessionContext = Depends(get_authenticated),
                          ctx: AppContext = Depends(get_ctx)):
    return await DisputeService(ctx).confirm(session, body)


@router.get("/disputes/{dispute_id}", response_model=DisputeView)
async def track_dispute(dispute_id: str, session: SessionContext = Depends(get_authenticated),
                        ctx: AppContext = Depends(get_ctx)):
    return await DisputeService(ctx).track(session, dispute_id)


@router.post("/handoffs", response_model=HandoffResponse, status_code=201)
async def create_handoff(body: HandoffRequest, session: SessionContext = Depends(get_session),
                         ctx: AppContext = Depends(get_ctx)):
    hid = await HandoffService(ctx).create(session, body.reason, body.summary)
    return HandoffResponse(handoff_id=hid, status="QUEUED")


class TurnRequest(StrictModel):
    text: str = Field(min_length=1, max_length=1000)


@router.post("/conversation/turn")
async def conversation_turn(body: TurnRequest, session: SessionContext = Depends(get_session),
                            ctx: AppContext = Depends(get_ctx)):
    """Text-fallback entry to the same orchestrator the voice pipeline will use."""
    return await ConversationOrchestrator(ctx).handle_turn(session, body.text)


@router.get("/sessions/{session_id}")
async def get_session_summary(session_id: str, session: SessionContext = Depends(get_session),
                              ctx: AppContext = Depends(get_ctx)):
    """A session token only ever reads its own session."""
    if session_id != session.session_id:
        raise AppError("FORBIDDEN", "That session does not belong to you.")
    doc = await ctx.db.voice_sessions.find_one({"session_id": session_id}, {"_id": 0})
    return {"session_id": session_id, "status": doc["status"], "outcome": doc["outcome"],
            "authenticated": session.customer_id is not None, "language": doc["language"]}


@router.post("/sessions/{session_id}/end")
async def end_session(session_id: str, session: SessionContext = Depends(get_session),
                      ctx: AppContext = Depends(get_ctx)):
    """Logout / End Call: clears auth state and tokens' session keys, marks the session completed."""
    if session_id != session.session_id:
        raise AppError("FORBIDDEN", "That session does not belong to you.")
    from app.banking.session_service import guard_redis

    await VoiceSessionRepository(ctx.db).end(session_id)
    await guard_redis(ctx.redis.delete(f"kv:auth:{session_id}", f"kv:session:{session_id}",
                                       f"kv:conv:{session_id}", f"kv:challenge:{session_id}"))
    await AuditRepository(ctx.db).record(action="SESSION_CLOSED", resource_type="SESSION", resource_id=session_id,
                                         decision="ALLOWED", reason_code="CUSTOMER_ENDED", session_id=session_id)
    return {"session_id": session_id, "status": "COMPLETED"}
