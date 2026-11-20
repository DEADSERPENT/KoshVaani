import asyncio
import json
import logging

from app.banking.operator_service import hash_password, verify_password
from app.banking.session_service import SessionContext, SessionService, guard_redis
from app.context import AppContext
from app.core.errors import AppError
from app.core.security import hash_secret, new_id, new_otp, safe_equal
from app.repositories.audit import AuditRepository
from app.repositories.customers import CustomerRepository


log = logging.getLogger("auth")
_pending: set = set()  # keeps background sends referenced until they finish
_DUMMY_HASH: str | None = None  # verified against when the phone is unknown, so timing does not reveal it


class AuthenticationService:
    """Simulated OTP only. The code is never logged and only a salted hash is kept in Redis."""

    def __init__(self, ctx: AppContext, sessions: SessionService):
        self.ctx = ctx
        self.r = ctx.redis
        self.sessions = sessions
        self.customers = CustomerRepository(ctx.db, ctx.cipher)
        self.audit = AuditRepository(ctx.db)

    def _require_mode(self, mode: str) -> None:
        if self.ctx.settings.auth_mode != mode:
            raise AppError("POLICY_REJECTED", "This sign-in method is not enabled.",
                           details={"reason_code": "AUTH_MODE_DISABLED"})

    async def _failures(self, key: str) -> int:
        return int(await guard_redis(self.r.get(key)) or 0)

    async def _count_failure(self, key: str) -> None:
        await guard_redis(self.r.incr(key))
        await guard_redis(self.r.expire(key, self.ctx.settings.password_lockout_seconds))

    async def login(self, session_id: str, phone_e164: str, password: str, client_ip: str = "unknown") -> dict:
        """Phone number + password. Every failure looks the same (unknown number, wrong password, closed account, no
        password set), costs the same time, and counts toward a lockout per number and per client address."""
        global _DUMMY_HASH
        s = self.ctx.settings
        self._require_mode("password")
        masked = "******" + phone_e164[-4:]
        phone_key = "kv:pwfail:phone:" + self.ctx.cipher.blind_index(phone_e164, "customer.phone")  # keyed: no number in Redis
        ip_key = f"kv:pwfail:ip:{client_ip}"
        if await self._failures(phone_key) >= s.password_max_failures or await self._failures(ip_key) >= s.otp_requests_per_ip:
            await self.audit.record(action="AUTH_LOGIN", resource_type="CUSTOMER", resource_id=masked, decision="DENIED",
                                    reason_code="LOGIN_LOCKED", session_id=session_id)
            raise AppError("RATE_LIMITED", "Too many failed sign-ins. Please wait a few minutes and try again.")
        creds = await self.customers.credentials_by_phone(phone_e164)
        stored = creds.get("password_hash") if creds and creds.get("status") == "ACTIVE" else None
        if _DUMMY_HASH is None:
            _DUMMY_HASH = await asyncio.to_thread(hash_password, "not-a-real-password")
        ok = await asyncio.to_thread(verify_password, password, stored or _DUMMY_HASH)  # always exactly one scrypt
        if not (stored and ok):
            await self._count_failure(phone_key)
            await self._count_failure(ip_key)
            await self.audit.record(action="AUTH_LOGIN", resource_type="CUSTOMER", resource_id=masked, decision="DENIED",
                                    reason_code="BAD_CREDENTIALS", session_id=session_id)
            raise AppError("AUTHENTICATION_FAILED", "The phone number or password is not correct.")
        await guard_redis(self.r.delete(phone_key))
        customer_id = creds["customer_id"]
        must_change = bool(creds.get("must_change_password"))
        if must_change:
            await self.sessions.mark_password_pending(session_id, customer_id)
        else:
            await self.sessions.mark_authenticated(session_id, customer_id)
        await self.audit.record(action="AUTH_LOGIN", resource_type="CUSTOMER", resource_id=customer_id, decision="ALLOWED",
                                reason_code="PASSWORD_CHANGE_REQUIRED" if must_change else "PASSWORD_OK", session_id=session_id)
        customer = await self.customers.get(customer_id)
        return {"authenticated": not must_change, "customer_display_name": customer["display_name"],
                "phone_masked": customer["phone_masked"], "must_change_password": must_change}

    def _password_problem(self, new: str, phone_e164: str | None) -> str | None:
        n = self.ctx.settings.password_min_length
        if len(new) < n:
            return f"The new password must be at least {n} characters."
        if new.isdigit() or len(set(new)) < 4:
            return "The new password is too easy to guess. Mix letters and digits."
        if phone_e164 and phone_e164[-10:] in new:
            return "The new password must not contain your phone number."
        return None

    async def change_password(self, session: SessionContext, current: str, new: str) -> dict:
        """Works for a signed-in customer and for one who signed in with a temporary password."""
        s = self.ctx.settings
        self._require_mode("password")
        customer_id = session.customer_id or await self.sessions.pending_password_customer(session.session_id)
        if not customer_id:
            raise AppError("AUTHENTICATION_REQUIRED", "Please sign in first.")
        key = f"kv:pwfail:change:{customer_id}"
        if await self._failures(key) >= s.password_max_failures:
            raise AppError("RATE_LIMITED", "Too many failed attempts. Please wait a few minutes and try again.")
        creds = await self.customers.credentials(customer_id)
        stored = creds.get("password_hash") if creds else None
        if not stored or not await asyncio.to_thread(verify_password, current, stored):
            await self._count_failure(key)
            await self.audit.record(action="PASSWORD_CHANGE", resource_type="CUSTOMER", resource_id=customer_id,
                                    decision="DENIED", reason_code="WRONG_CURRENT_PASSWORD", session_id=session.session_id)
            raise AppError("AUTHENTICATION_FAILED", "The current password is not correct.")
        problem = self._password_problem(new, await self.customers.get_phone(customer_id))
        if new == current:
            problem = "The new password must be different from the current one."
        if problem:
            raise AppError("VALIDATION_ERROR", problem, details={"reason_code": "WEAK_PASSWORD"})
        await self.customers.set_password(customer_id, await asyncio.to_thread(hash_password, new), False)
        await guard_redis(self.r.delete(key))
        await self.sessions.mark_authenticated(session.session_id, customer_id)
        await self.sessions.clear_password_pending(session.session_id)
        await self.audit.record(action="PASSWORD_CHANGE", resource_type="CUSTOMER", resource_id=customer_id,
                                decision="ALLOWED", reason_code="PASSWORD_CHANGED", session_id=session.session_id)
        return {"changed": True}

    async def _throttle(self, phone_e164: str, client_ip: str) -> None:
        s = self.ctx.settings
        phone_key = self.ctx.cipher.blind_index(phone_e164, "customer.phone")  # keyed hash: no number in Redis
        for key, limit in ((f"kv:otpreq:phone:{phone_key}", s.otp_requests_per_phone),
                           (f"kv:otpreq:ip:{client_ip}", s.otp_requests_per_ip)):
            count = await guard_redis(self.r.incr(key))
            if count == 1:
                await guard_redis(self.r.expire(key, s.otp_window_seconds))
            if count > limit:
                await self.audit.record(action="AUTH_CHALLENGE", resource_type="CUSTOMER",
                                        resource_id="******" + phone_e164[-4:], decision="DENIED",
                                        reason_code="OTP_REQUEST_LIMIT")
                raise AppError("RATE_LIMITED", "Too many verification requests. Please wait a few minutes and try again.")

    async def start(self, session_id: str, phone_e164: str, client_ip: str = "unknown") -> dict:
        """Phone-number login. The response is identical whether or not the number is registered, so the
        endpoint cannot be used to discover customers. An unknown number gets a challenge nobody can pass."""
        s = self.ctx.settings
        self._require_mode("otp")
        await self._throttle(phone_e164, client_ip)
        customer = await self.customers.get_by_phone(phone_e164)
        known = bool(customer and customer.get("status") == "ACTIVE")
        masked = "******" + phone_e164[-4:]
        if not known:
            await self.audit.record(action="AUTH_CHALLENGE", resource_type="CUSTOMER", resource_id=masked,
                                    decision="DENIED", reason_code="PHONE_NOT_REGISTERED", session_id=session_id)
        code = new_otp()
        challenge_id = new_id("CH", 6)
        await guard_redis(self.r.set(
            f"kv:challenge:{session_id}",
            json.dumps({"challenge_id": challenge_id, "customer_id": customer["customer_id"] if known else None,
                        "hash": hash_secret(code, challenge_id), "attempts": 0}),
            ex=s.otp_ttl_seconds))
        if known:
            self._deliver_in_background(session_id, phone_e164, code)
        return {
            "challenge_id": challenge_id, "delivery_mode": self.ctx.otp_sender.delivery_mode,
            "expires_in_seconds": s.otp_ttl_seconds,
            "message": f"If {masked} is registered with us, a verification code has been sent to it.",
            "dev_code": code if (s.echo_dev_otp and known) else None,
        }

    def _deliver_in_background(self, session_id: str, phone_e164: str, code: str) -> None:
        """Sent off the request path so response time is the same whether or not the number is registered,
        and a gateway failure cannot reveal registration either. The customer can simply request a new code."""
        async def send():
            try:
                await self.ctx.otp_sender.send(phone_e164, code)
            except Exception as exc:  # noqa: BLE001
                log.warning("otp_delivery_failed", extra={"outcome": type(exc).__name__})
                await self.audit.record(action="PROVIDER_ERROR", resource_type="PROVIDER", resource_id="sms",
                                        decision="FAILED", reason_code="OTP_DELIVERY_FAILED", session_id=session_id)
        task = asyncio.create_task(send())
        _pending.add(task)
        task.add_done_callback(_pending.discard)

    async def verify(self, session_id: str, challenge_id: str, code: str) -> dict:
        self._require_mode("otp")
        s = self.ctx.settings
        key = f"kv:challenge:{session_id}"
        raw = await guard_redis(self.r.get(key))
        if not raw:
            raise AppError("AUTHENTICATION_FAILED", "That verification request has expired. Please start again.")
        ch = json.loads(raw)
        if ch["challenge_id"] != challenge_id:
            raise AppError("AUTHENTICATION_FAILED", "That verification request is not valid.")
        if not ch["customer_id"] or not safe_equal(hash_secret(code, challenge_id), ch["hash"]):
            ch["attempts"] += 1
            await self.audit.record(action="AUTH_VERIFY", resource_type="CUSTOMER",
                                    resource_id=ch["customer_id"] or "UNREGISTERED", decision="DENIED",
                                    reason_code="WRONG_CODE", session_id=session_id)
            if ch["attempts"] >= s.otp_max_attempts:
                await guard_redis(self.r.delete(key))
                raise AppError("AUTHENTICATION_FAILED",
                               "Too many incorrect attempts. I can transfer you to a human agent.",
                               details={"handoff_recommended": True})
            ttl = await guard_redis(self.r.ttl(key))
            await guard_redis(self.r.set(key, json.dumps(ch), ex=max(ttl, 1)))
            raise AppError("AUTHENTICATION_FAILED", "That code is not correct.")
        await guard_redis(self.r.delete(key))
        await self.sessions.mark_authenticated(session_id, ch["customer_id"])
        await self.audit.record(action="AUTH_VERIFY", resource_type="CUSTOMER", resource_id=ch["customer_id"],
                                decision="ALLOWED", reason_code="OTP_VERIFIED", session_id=session_id)
        customer = await self.customers.get(ch["customer_id"])
        return {"authenticated": True, "customer_display_name": customer["display_name"],
                "phone_masked": customer["phone_masked"]}
