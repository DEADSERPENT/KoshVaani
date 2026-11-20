import json
from dataclasses import dataclass

from redis.exceptions import RedisError

from app.context import AppContext
from app.core.errors import AppError
from app.core.security import new_id, new_token, sha256
from app.repositories.audit import AuditRepository
from app.repositories.sessions import VoiceSessionRepository


@dataclass
class SessionContext:
    session_id: str
    customer_id: str | None  # always derived server-side, never from client payloads


def require_customer(session: "SessionContext") -> str:
    """The verified customer id for this session, or AUTHENTICATION_REQUIRED."""
    if not session.customer_id:
        raise AppError("AUTHENTICATION_REQUIRED", "Please verify your identity before I access account information.")
    return session.customer_id


async def guard_redis(coro):
    """Redis is required for session/idempotency state: fail closed when it is unreachable."""
    try:
        return await coro
    except (RedisError, OSError) as exc:
        raise AppError("STORE_UNAVAILABLE", "A required service is temporarily unavailable. Please retry.") from exc


class SessionService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.r = ctx.redis
        self.audit = AuditRepository(ctx.db)
        self.repo = VoiceSessionRepository(ctx.db)

    async def create(self, channel: str, language: str, mic_consent: bool = False) -> tuple[str, str]:
        s = self.ctx.settings
        session_id = new_id("VS", 6)
        token = new_token()
        await guard_redis(self.r.set(f"kv:token:{sha256(token)}", session_id, ex=s.session_ttl_seconds))
        await guard_redis(self.r.set(
            f"kv:session:{session_id}", json.dumps({"turns": 0, "channel": channel}), ex=s.session_ttl_seconds))
        if mic_consent:
            await guard_redis(self.r.set(f"kv:micconsent:{session_id}", "1", ex=s.session_ttl_seconds))
        await self.repo.create(session_id, channel, language)
        await self.audit.record(action="SESSION_CREATED", resource_type="SESSION", resource_id=session_id,
                                decision="ALLOWED", reason_code="SESSION_BOOTSTRAP", session_id=session_id)
        return session_id, token

    async def resolve(self, token: str) -> SessionContext:
        sid = await guard_redis(self.r.get(f"kv:token:{sha256(token)}"))
        if not sid or not await guard_redis(self.r.exists(f"kv:session:{sid}")):
            raise AppError("SESSION_EXPIRED", "Your session has expired. Please start a new call.")
        cust = await guard_redis(self.r.get(f"kv:auth:{sid}"))
        return SessionContext(session_id=sid, customer_id=cust)

    async def by_id(self, sid: str) -> SessionContext:
        """For a caller already proven to own the session (e.g. a redeemed stream ticket)."""
        if not await guard_redis(self.r.exists(f"kv:session:{sid}")):
            raise AppError("SESSION_EXPIRED", "Your session has expired. Please start a new call.")
        return SessionContext(session_id=sid, customer_id=await guard_redis(self.r.get(f"kv:auth:{sid}")))

    async def require_mic_consent(self, sid: str) -> None:
        """Audio goes to third-party speech providers, so it is refused unless this session recorded consent."""
        if not await guard_redis(self.r.exists(f"kv:micconsent:{sid}")):
            raise AppError("POLICY_REJECTED", "Microphone consent is required to use voice.",
                           details={"reason_code": "MIC_CONSENT_MISSING"})

    async def rate_limit(self, sid: str, route: str) -> None:
        key = f"kv:rate:{sid}:{route}"
        n = await guard_redis(self.r.incr(key))
        if n == 1:
            await guard_redis(self.r.expire(key, 60))
        if n > self.ctx.settings.rate_limit_per_minute:
            raise AppError("RATE_LIMITED", "Too many requests. Please slow down.")

    async def mark_password_pending(self, sid: str, customer_id: str) -> None:
        """Signed in with a temporary password: the session is NOT authenticated until the password is changed."""
        await guard_redis(self.r.set(f"kv:pwpending:{sid}", customer_id, ex=self.ctx.settings.auth_ttl_seconds))

    async def pending_password_customer(self, sid: str) -> str | None:
        return await guard_redis(self.r.get(f"kv:pwpending:{sid}"))

    async def clear_password_pending(self, sid: str) -> None:
        await guard_redis(self.r.delete(f"kv:pwpending:{sid}"))

    async def mark_authenticated(self, sid: str, customer_id: str) -> None:
        await guard_redis(self.r.set(f"kv:auth:{sid}", customer_id, ex=self.ctx.settings.auth_ttl_seconds))
        await self.repo.set_customer(sid, customer_id)
