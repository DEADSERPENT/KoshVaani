from app.config import Settings
from app.core.errors import AppError

ALLOWED_TYPES = {"audio/webm", "audio/ogg", "audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp4", "audio/flac",
                 "text/plain"}  # text/plain only meaningful for the mock STT


def validate_audio(data: bytes, content_type: str, settings: Settings) -> str:
    base = (content_type or "").split(";")[0].strip().lower()
    if base not in ALLOWED_TYPES or (base == "text/plain" and settings.stt_provider != "mock"):
        raise AppError("VALIDATION_ERROR", "Unsupported audio format.")
    if not data:
        raise AppError("VALIDATION_ERROR", "No audio was received.")
    if len(data) > settings.max_audio_bytes:
        raise AppError("VALIDATION_ERROR", "The recording is too large.")
    return base
