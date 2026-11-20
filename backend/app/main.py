import logging
import uuid
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.routes import router
from app.api.v1.operations import auth_router as ops_auth_router, router as ops_router
from app.api.v1.voice import router as voice_router
from app.banking.operator_service import OperatorService
from app.banking.seed import seed
from app.config import Settings, assert_safe_for_environment, get_settings
from app.context import AppContext
from app.core.crypto import FieldCipher, generate_key
from app.core.errors import AppError, app_error_handler
from app.core.logging import configure_logging, request_id_var
from app.infrastructure.mongodb import create_db, ensure_indexes
from app.repositories.audit import AuditRepository
from app.infrastructure.redis import create_redis
from app.providers import create_llm, create_stt, create_tts
from app.providers.otp import create_otp_sender

log = logging.getLogger("app")


def create_app(settings: Settings | None = None, llm=None, clock=None, stt=None, tts=None, otp_sender=None) -> FastAPI:
    settings = settings or get_settings()
    assert_safe_for_environment(settings)
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = create_db(settings)
        app.state.ctx = AppContext(settings=settings, db=db, redis=create_redis(settings),
                                   cipher=FieldCipher(settings.data_encryption_key or generate_key()),
                                   llm=llm or create_llm(settings), clock=clock,
                                   stt=stt or create_stt(settings), tts=tts or create_tts(settings),
                                   otp_sender=otp_sender or create_otp_sender(settings))
        if settings.storage_backend == "memory" and not settings.is_production:
            now = (clock or (lambda: datetime.now(timezone.utc)))()
            await seed(db, app.state.ctx.cipher, now.date())  # in-memory store starts empty; dates follow the app clock
        else:
            await ensure_indexes(db, settings.transcript_retention_days, settings.session_retention_days)
        try:  # open the Mongo/Redis connections and warm the audit path now, so the first callers do not pay for it
            await AuditRepository(db).record(action="SERVICE_STARTED", resource_type="SERVICE", resource_id=settings.app_name,
                                             decision="ALLOWED", reason_code="STARTUP", actor_type="SYSTEM", actor_id="KOSHVAANI_API")
            await app.state.ctx.redis.ping()
        except Exception:  # noqa: BLE001  readiness endpoint reports the real state; startup must not crash on a slow dependency
            log.warning("startup_warmup_failed")
        ops = OperatorService(app.state.ctx)
        if settings.bootstrap_operator_username and settings.bootstrap_operator_password and not await ops.has_any():
            await ops.create(settings.bootstrap_operator_username, settings.bootstrap_operator_password, "ADMIN")
            log.warning("Bootstrap admin created; remove BOOTSTRAP_OPERATOR_* from the environment.")
        yield

    # Interactive API docs are a development aid; production exposes no schema or docs pages.
    docs = {"docs_url": None, "redoc_url": None, "openapi_url": None} if settings.is_production else {}
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan, **docs)
    app.add_middleware(CORSMiddleware, allow_origins=[settings.frontend_origin],
                       allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
                       allow_headers=["Authorization", "Content-Type"], max_age=600)
    app.add_exception_handler(AppError, app_error_handler)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        body = {"error": {"code": "VALIDATION_ERROR", "message": "The request was not valid.",
                          "request_id": getattr(request.state, "request_id", None), "retryable": False}}
        return JSONResponse(status_code=422, content=body)

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = f"req-{uuid.uuid4().hex[:12]}"
        request_id_var.set(request.state.request_id)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        # Every response can carry personal or account data: never cacheable, never framed, never sniffed.
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), geolocation=(), payment=()")
        if not request.url.path.startswith(("/docs", "/redoc")):  # Swagger UI needs scripts; the API itself needs none
            response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        if settings.is_production:
            response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        return response

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request):
        ctx: AppContext = request.app.state.ctx
        status = {}
        try:
            await ctx.db.command("ping") if settings.storage_backend == "mongo" else await ctx.db.list_collection_names()
            status["mongodb"] = "ok"
        except Exception:
            status["mongodb"] = "down"
        try:
            await ctx.redis.ping()
            status["redis"] = "ok"
        except Exception:
            status["redis"] = "down"
        ready = all(v == "ok" for v in status.values())
        # LLM/speech providers are reported, never required: the API stays up without them.
        providers = {"llm": ctx.llm.name, "stt": ctx.stt.name, "tts": ctx.tts.name}
        return JSONResponse(status_code=200 if ready else 503, content={"ready": ready, **status, "providers": providers})

    app.include_router(router)
    app.include_router(voice_router)
    app.include_router(ops_auth_router)
    app.include_router(ops_router)
    return app


app = create_app()
