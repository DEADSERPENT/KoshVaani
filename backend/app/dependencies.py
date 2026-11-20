from fastapi import Depends, Header, Request

from app.banking.session_service import SessionContext, SessionService
from app.context import AppContext
from app.core.errors import AppError


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def get_session_service(ctx: AppContext = Depends(get_ctx)) -> SessionService:
    return SessionService(ctx)


async def get_session(
    request: Request,
    authorization: str | None = Header(default=None),
    sessions: SessionService = Depends(get_session_service),
) -> SessionContext:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AppError("SESSION_EXPIRED", "Missing session token.")
    session = await sessions.resolve(authorization[7:].strip())
    await sessions.rate_limit(session.session_id, request.url.path)
    return session


async def get_authenticated(session: SessionContext = Depends(get_session)) -> SessionContext:
    if not session.customer_id:
        raise AppError("AUTHENTICATION_REQUIRED",
                       "Please verify your identity before I access transaction information.")
    return session
