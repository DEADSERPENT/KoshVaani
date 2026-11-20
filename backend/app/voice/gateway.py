"""Versioned WebSocket event contract for the browser voice client (see docs/api-contracts.md).

Provider-specific formats never reach the browser. Only a final transcript can trigger the agent.
"""
import asyncio
import logging

from fastapi import WebSocket, WebSocketDisconnect

from app.banking.session_service import SessionContext, SessionService
from app.context import AppContext
from app.core.errors import AppError
from app.core.logging import session_id_var
from app.repositories.sessions import VoiceSessionRepository
from app.voice import pipeline
from app.voice.audio_validation import validate_audio

log = logging.getLogger("gateway")

MAX_FRAME_BYTES = 64 * 1024
IDLE_TIMEOUT_SECONDS = 120
AUDIO_CHUNK_BYTES = 32 * 1024
CONFIRM_TEXT = {True: "Yes, please register the dispute.", False: "No, do not register the dispute."}


class VoiceGateway:
    def __init__(self, ws: WebSocket, ctx: AppContext, session: SessionContext):
        self.ws, self.ctx, self.session = ws, ctx, session
        self.audio = bytearray()
        self.recording = False
        self.content_type = "audio/webm"
        self.seq_expected = 0

    async def send(self, **event) -> None:
        await self.ws.send_json(event)

    async def error(self, code: str, retryable: bool = False, message: str = "") -> None:
        await self.send(type="error", code=code, retryable=retryable, message=message)

    async def run(self) -> None:
        session_id_var.set(self.session.session_id)
        await self.send(type="session.ready", session_id=self.session.session_id,
                        authenticated=self.session.customer_id is not None)
        await self.send(type="assistant.state", state="LISTENING")
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(self.ws.receive(), IDLE_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    await self.error("SESSION_EXPIRED", message="Idle timeout.")
                    break
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    await self.on_frame(msg["bytes"])
                elif msg.get("text") is not None:
                    if await self.on_event(msg["text"]):
                        break
        except WebSocketDisconnect:
            pass
        finally:
            await VoiceSessionRepository(self.ctx.db).end(self.session.session_id)

    # ---- inbound ----

    async def on_frame(self, data: bytes) -> None:
        if not self.recording:
            return await self.error("VALIDATION_ERROR", message="Send audio.start before audio frames.")
        if len(data) > MAX_FRAME_BYTES:
            self.recording = False
            self.audio.clear()
            return await self.error("VALIDATION_ERROR", message="Audio frame too large.")
        if len(self.audio) + len(data) > self.ctx.settings.max_audio_bytes:  # bounded buffer: no unbounded growth
            self.recording = False
            self.audio.clear()
            return await self.error("VALIDATION_ERROR", message="Recording too long.")
        self.audio.extend(data)

    async def on_event(self, raw: str) -> bool:
        """Returns True when the connection should close."""
        import json

        try:
            ev = json.loads(raw)
            kind = ev["type"]
        except (ValueError, KeyError, TypeError):
            await self.error("VALIDATION_ERROR", message="Malformed event.")
            return False
        try:
            return await self.dispatch(kind, ev)
        except AppError as e:
            await self.error(e.code, e.retryable, e.message)
            await self.send(type="assistant.state", state="LISTENING")
            return False

    async def dispatch(self, kind: str, ev: dict) -> bool:
        if kind == "ping":
            await self.send(type="pong")
        elif kind == "session.start":
            await self.send(type="assistant.state", state="LISTENING")
        elif kind == "audio.start":
            await SessionService(self.ctx).require_mic_consent(self.session.session_id)
            self.audio.clear()
            self.recording = True
            self.content_type = str(ev.get("content_type") or "audio/webm")
            await self.send(type="assistant.state", state="LISTENING")
        elif kind == "audio.end":
            await self.finish_audio()
        elif kind == "text.input":
            if not self.ctx.settings.allow_text_fallback:
                raise AppError("FORBIDDEN", "Text input is disabled.")
            text = str(ev.get("text", ""))[:1000]
            if not text.strip():
                raise AppError("VALIDATION_ERROR", "Text is empty.")
            await self.respond(text)
        elif kind == "client.confirmation":
            await self.confirmation(ev)
        elif kind == "session.stop":
            await self.send(type="session.completed", outcome=await self.outcome())
            return True
        else:
            raise AppError("VALIDATION_ERROR", f"Unknown event type {kind!r}.")
        return False

    async def finish_audio(self) -> None:
        if not self.recording:
            raise AppError("VALIDATION_ERROR", "No recording in progress.")
        self.recording = False
        data, self.audio = bytes(self.audio), bytearray()
        ctype = validate_audio(data, self.content_type, self.ctx.settings)
        await self.send(type="assistant.state", state="TRANSCRIBING")
        transcript, asr_ms = await pipeline.transcribe(self.ctx, self.session.session_id, data, ctype)
        await self.send(type="transcript.final", text=transcript.text, provider=transcript.provider)
        await self.respond(transcript.text, asr_ms=asr_ms)

    async def confirmation(self, ev: dict) -> None:
        # UI buttons go through the same deterministic path as speech: the orchestrator + backend re-check everything.
        if not isinstance(ev.get("value"), bool):
            raise AppError("VALIDATION_ERROR", "value must be true or false.")
        raw = await self.ctx.redis.get(f"kv:conv:{self.session.session_id}")
        import json

        conv = json.loads(raw) if raw else {}
        pending = conv.get("pending_prep") or conv.get("pending_card")
        if not pending or ev.get("preparation_id") != pending:
            raise AppError("CONFIRMATION_REQUIRED", "There is nothing pending to confirm.")
        await self.respond(CONFIRM_TEXT[ev["value"]])

    # ---- outbound ----

    async def respond(self, text: str, asr_ms: int = 0) -> None:
        await self.send(type="assistant.state", state="UNDERSTANDING")
        session = await self.refresh_session()
        turn = await pipeline.agent_turn(self.ctx, session, text)
        for t in turn["tool_trace"]:
            await self.send(type="tool.result", tool=t["tool"], status="success" if t["ok"] else "error",
                            error_code=t["error_code"])
        await self.send(type="assistant.message", text=turn["reply"], intent=turn["intent"],
                        state=turn["state"], entities=turn["entities"],
                        preparation_id=turn.get("preparation_id"), pending_action=turn.get("pending_action"))
        if turn.get("dispute_id") and any(t["tool"] == "confirm_dispute" and t["ok"] for t in turn["tool_trace"]):
            await self.send(type="case.created", dispute_id=turn["dispute_id"])
        if turn.get("blocked_card_id") and any(t["tool"] == "confirm_card_block" and t["ok"] for t in turn["tool_trace"]):
            await self.send(type="card.blocked", card_id=turn["blocked_card_id"])
        if turn.get("handoff_id") and any(t["tool"] == "request_human_handoff" for t in turn["tool_trace"]):
            await self.send(type="handoff.created", handoff_id=turn["handoff_id"])
        await self.send(type="assistant.state", state="SPEAKING")
        audio, ctype, tts_error, tts_ms = await pipeline.speak(self.ctx, self.session.session_id, turn["reply"])
        if audio:
            for i in range(0, len(audio), AUDIO_CHUNK_BYTES):
                await self.send(type="audio.chunk", content_type=ctype, seq=i // AUDIO_CHUNK_BYTES,
                                final=i + AUDIO_CHUNK_BYTES >= len(audio), data=pipeline.b64(audio[i:i + AUDIO_CHUNK_BYTES]))
        else:  # transcript already delivered, so the UI can still show the reply
            await self.error(tts_error or "PROVIDER_UNAVAILABLE", True, "Audio could not be generated.")
        total = await pipeline.record_voice_latency(self.ctx, self.session.session_id, turn["turn_index"], asr_ms, tts_ms)
        await self.send(type="turn.completed", latency_ms={"asr": asr_ms, "tts": tts_ms, "total": total})
        await self.send(type="assistant.state", state="HANDOFF" if turn["state"] == "HANDOFF" else "LISTENING")

    async def refresh_session(self) -> SessionContext:
        """Re-derive auth from Redis each turn so an expiry or logout takes effect immediately."""
        raw = await SessionService(self.ctx).r.get(f"kv:auth:{self.session.session_id}")
        self.session = SessionContext(self.session.session_id, raw)
        return self.session

    async def outcome(self) -> str | None:
        doc = await self.ctx.db.voice_sessions.find_one({"session_id": self.session.session_id})
        return (doc or {}).get("outcome") or "ABANDONED"
