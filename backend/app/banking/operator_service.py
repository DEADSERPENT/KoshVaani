import hashlib
import json
import secrets

from app.banking.session_service import guard_redis
from app.context import AppContext
from app.core.errors import AppError
from app.core.security import new_id, new_token, safe_equal
from app.infrastructure.mongodb import utcnow

ROLES = ("VIEWER", "AGENT", "ADMIN")  # ascending privilege
MIN_PASSWORD_LENGTH = 12
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 900
_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)
_DUMMY_HASH = None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT)
    except ValueError:
        return False
    return safe_equal(got.hex(), digest)


def check_password_policy(username: str, password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if password.lower() == username.lower():
        raise ValueError("Password must not equal the username.")


def _token_key(token: str) -> str:
    return "kv:operator:" + hashlib.sha256(token.encode()).hexdigest()


class OperatorContext:
    def __init__(self, operator_id: str, username: str, role: str):
        self.operator_id, self.username, self.role = operator_id, username, role

    def allows(self, minimum: str) -> bool:
        return ROLES.index(self.role) >= ROLES.index(minimum)


class OperatorService:
    """Named operator accounts with salted scrypt hashes, lockout and revocable server-side tokens.

    The token is random and opaque; only its SHA-256 is used as the Redis key, so a Redis dump cannot be replayed."""

    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.col = ctx.db.operators
        self.r = ctx.redis

    async def create(self, username: str, password: str, role: str) -> str:
        username = username.strip().lower()
        if role not in ROLES:
            raise ValueError(f"Role must be one of {', '.join(ROLES)}.")
        if len(username) < 3:
            raise ValueError("Username must be at least 3 characters.")
        check_password_policy(username, password)
        if await self.col.find_one({"username": username}):
            raise ValueError("That username already exists.")
        operator_id = new_id("OP")
        await self.col.insert_one({"operator_id": operator_id, "username": username,
                                   "password_hash": hash_password(password), "role": role, "active": True,
                                   "created_at": utcnow(), "last_login_at": None})
        return operator_id

    async def has_any(self) -> bool:
        return await self.col.count_documents({}) > 0

    async def login(self, username: str, password: str) -> tuple[str, dict]:
        global _DUMMY_HASH
        username = username.strip().lower()
        lock_key = f"kv:oplock:{username}"
        fails = int(await guard_redis(self.r.get(lock_key)) or 0)
        if fails >= MAX_FAILED_LOGINS:
            raise AppError("RATE_LIMITED", "Too many failed sign-ins. Try again in a few minutes.")
        op = await self.col.find_one({"username": username}, {"_id": 0})
        if _DUMMY_HASH is None:
            _DUMMY_HASH = hash_password("not-a-real-password")
        # Always run one scrypt so an unknown username costs the same as a wrong password.
        ok = verify_password(password, op["password_hash"] if op else _DUMMY_HASH)
        if not (op and ok and op.get("active")):
            await guard_redis(self.r.incr(lock_key))
            await guard_redis(self.r.expire(lock_key, LOCKOUT_SECONDS))
            raise AppError("AUTHENTICATION_FAILED", "Invalid username or password.")
        await guard_redis(self.r.delete(lock_key))
        token = new_token()
        ttl = self.ctx.settings.operator_session_seconds
        await guard_redis(self.r.set(_token_key(token), json.dumps(
            {"operator_id": op["operator_id"], "username": op["username"], "role": op["role"]}), ex=ttl))
        await self.col.update_one({"operator_id": op["operator_id"]}, {"$set": {"last_login_at": utcnow()}})
        return token, {"operator_id": op["operator_id"], "username": op["username"], "role": op["role"],
                       "expires_in_seconds": ttl}

    async def resolve(self, token: str) -> OperatorContext:
        raw = await guard_redis(self.r.get(_token_key(token)))
        if not raw:
            raise AppError("AUTHENTICATION_REQUIRED", "Operator sign-in required.")
        d = json.loads(raw)
        # Re-check the account each time so deactivating an operator takes effect immediately.
        op = await self.col.find_one({"operator_id": d["operator_id"]}, {"_id": 0})
        if not op or not op.get("active"):
            await guard_redis(self.r.delete(_token_key(token)))
            raise AppError("AUTHENTICATION_REQUIRED", "Operator sign-in required.")
        return OperatorContext(op["operator_id"], op["username"], op["role"])

    async def logout(self, token: str) -> None:
        await guard_redis(self.r.delete(_token_key(token)))
