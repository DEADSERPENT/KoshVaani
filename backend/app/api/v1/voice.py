import logging

from fastapi import APIRouter, Depends, Response, UploadFile, WebSocket
from pydantic import BaseModel, Field

from app.banking.session_service import SessionContext, SessionService, guard_redis
from app.core.errors import AppError
from app.core.security import new_token
from app.voice.gateway import VoiceGateway
from app.context import AppContext
from app.dependencies import get_ctx, get_session
from app.schemas.common import StrictModel
from app.voice.audio_validation import validate_audio
from app.voice import pipeline

router = APIRouter(prefix="/api/v1/voice")
log = logging.getLogger("voice")


class TranscribeResponse(BaseModel):
    text: str
    language: str
    provider: str
    is_final: bool
    confidence: float | None
    request_id: str | None


class SynthesizeRequest(StrictModel):
    text: str = Field(min_length=1, max_length=1000)


async def _read_audio(file: UploadFile, ctx: AppContext, session: SessionContext) -> tuple[bytes, str]:
    await SessionService(ctx).require_mic_consent(session.session_id)
    ctype = validate_audio(b"x", file.content_type or "", ctx.settings)  # type check before reading the body
    data = await file.read(ctx.settings.max_audio_bytes + 1)
    return data, validate_audio(data, ctype, ctx.settings)


@router.post("/transcribe", response_model=TranscribeResponse)
async def transcribe(file: UploadFile, session: SessionContext = Depends(get_session),
                     ctx: AppContext = Depends(get_ctx)):
    data, ctype = await _read_audio(file, ctx, session)
    t = await ctx.stt.transcribe(data, content_type=ctype, language=ctx.settings.speech_language)
    return TranscribeResponse(**t.__dict__ | {"is_final": t.is_final})


@router.post("/synthesize")
async def synthesize(body: SynthesizeRequest, session: SessionContext = Depends(get_session),
                     ctx: AppContext = Depends(get_ctx)):
    out = await ctx.tts.synthesize(body.text)
    return Response(content=out.audio, media_type=out.content_type,
                    headers={"X-Provider": out.provider, "X-Provider-Request-ID": out.request_id or ""})


@router.post("/turn")
async def voice_turn(file: UploadFile, session: SessionContext = Depends(get_session),
                     ctx: AppContext = Depends(get_ctx)):
    """One utterance in -> transcript, agent reply and spoken audio out.

    STT failure raises (no business action runs). TTS failure still returns the text reply so the
    transcript stays visible when audio cannot be generated.
    """
    data, ctype = await _read_audio(file, ctx, session)
    transcript, asr_ms = await pipeline.transcribe(ctx, session.session_id, data, ctype)
    turn = await pipeline.agent_turn(ctx, session, transcript.text)
    audio, content_type, tts_error, tts_ms = await pipeline.speak(ctx, session.session_id, turn["reply"])
    total = await pipeline.record_voice_latency(ctx, session.session_id, turn["turn_index"], asr_ms, tts_ms)
    return {**turn, "transcript": transcript.text,
            "audio": {"base64": pipeline.b64(audio), "content_type": content_type, "error": tts_error},
            "providers": {"stt": transcript.provider, "tts": ctx.tts.name},
            "latency_ms": {"asr": asr_ms, "tts": tts_ms, "total": total}}


@router.post("/ws-ticket")
async def ws_ticket(session: SessionContext = Depends(get_session), ctx: AppContext = Depends(get_ctx)):
    """Browsers cannot set headers on WebSockets, so the stream is opened with a short-lived single-use ticket.
    The long-lived session token never appears in a URL (and so never in proxy or server logs)."""
    await SessionService(ctx).require_mic_consent(session.session_id)
    ticket = new_token()
    ttl = ctx.settings.ws_ticket_seconds
    await guard_redis(ctx.redis.set(f"kv:wsticket:{ticket}", session.session_id, ex=ttl))
    return {"ticket": ticket, "expires_in_seconds": ttl}


@router.websocket("/sessions/{session_id}/stream")
async def stream(ws: WebSocket, session_id: str, ticket: str = ""):
    ctx: AppContext = ws.app.state.ctx
    try:
        owner = await guard_redis(ctx.redis.getdel(f"kv:wsticket:{ticket}")) if ticket else None  # single use
        if not owner or owner != session_id:
            raise AppError("FORBIDDEN", "Invalid or expired stream ticket.")
        session = await SessionService(ctx).by_id(session_id)
    except AppError:
        await ws.close(code=4401)  # rejected before accept: no events are exchanged
        return
    await ws.accept()
    await VoiceGateway(ws, ctx, session).run()
