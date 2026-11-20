"""Shared audio -> transcript -> agent -> speech steps, used by REST /voice/turn and the WebSocket gateway."""
import base64
import logging
import time

from app.banking.session_service import SessionContext
from app.context import AppContext
from app.core.errors import AppError
from app.providers.speech_base import TranscriptResult
from app.repositories.audit import AuditRepository
from app.voice.conversation_orchestrator import ConversationOrchestrator

log = logging.getLogger("voice")
NO_SPEECH_REPLY = "Sorry, I did not catch that. Could you say it again?"


async def _audit_provider_error(ctx: AppContext, session_id: str, provider: str, code: str) -> None:
    await AuditRepository(ctx.db).record(action="PROVIDER_ERROR", resource_type="PROVIDER", resource_id=provider,
                                         decision="FAILED", reason_code=code, session_id=session_id)


async def transcribe(ctx: AppContext, session_id: str, audio: bytes, content_type: str) -> tuple[TranscriptResult, int]:
    s = time.perf_counter()
    try:
        t = await ctx.stt.transcribe(audio, content_type=content_type, language=ctx.settings.speech_language)
    except AppError as e:
        await _audit_provider_error(ctx, session_id, ctx.stt.name, e.code)
        raise  # no business action runs on an unknown transcript
    return t, round((time.perf_counter() - s) * 1000)


async def speak(ctx: AppContext, session_id: str, text: str) -> tuple[bytes | None, str | None, str | None, int]:
    """Returns (audio, content_type, error_code, ms). Never raises: the text reply must survive TTS failure."""
    s = time.perf_counter()
    try:
        out = await ctx.tts.synthesize(text)
        return out.audio, out.content_type, None, round((time.perf_counter() - s) * 1000)
    except AppError as e:
        log.warning("tts_failed", extra={"session_id": session_id, "outcome": e.code})
        await _audit_provider_error(ctx, session_id, ctx.tts.name, e.code)
        return None, None, e.code, round((time.perf_counter() - s) * 1000)


async def agent_turn(ctx: AppContext, session: SessionContext, text: str) -> dict:
    if not text.strip():
        return {"turn_index": None, "reply": NO_SPEECH_REPLY, "intent": "UNKNOWN", "state": "LISTENING",
                "entities": {}, "tool_trace": [], "preparation_id": None, "dispute_id": None,
                "handoff_id": None}
    return await ConversationOrchestrator(ctx).handle_turn(session, text)


async def record_voice_latency(ctx: AppContext, session_id: str, turn_index: int | None, asr_ms: int, tts_ms: int) -> int:
    """Adds ASR/TTS timings to the persisted customer turn and returns the end-to-end total."""
    if turn_index is None:
        return asr_ms + tts_ms
    doc = await ctx.db.conversation_turns.find_one({"session_id": session_id, "turn_index": turn_index})
    base = (doc or {}).get("latency_ms", {}).get("total", 0)
    total = base + asr_ms + tts_ms
    await ctx.db.conversation_turns.update_one(
        {"session_id": session_id, "turn_index": turn_index},
        {"$set": {"latency_ms.asr": asr_ms, "latency_ms.tts_first_audio": tts_ms, "latency_ms.total": total}})
    return total


def b64(audio: bytes | None) -> str | None:
    return base64.b64encode(audio).decode() if audio else None
