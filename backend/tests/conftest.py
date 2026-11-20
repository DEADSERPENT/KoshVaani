"""Keep the suite hermetic: the developer's real .env (live Groq/Deepgram/Ollama keys) must never leak into tests.

Environment variables outrank the dotenv file in pydantic-settings, so pinning them here is enough. Tests that
want a specific value still pass it explicitly to Settings(...).
"""
import os

_PINNED = {
    "APP_ENV": "test",  # the only environment where the login code may be shown instead of texted
    "STORAGE_BACKEND": "memory",
    "AUTH_MODE": "otp",  # existing tests log in with the on-screen test code; password mode has its own tests
    "OTP_PROVIDER": "dev_screen",
    "ECHO_DEV_OTP": "true",
    "LLM_PROVIDER": "rules",
    "STT_PROVIDER": "mock",
    "TTS_PROVIDER": "mock",
    "GROQ_API_KEY": "",
    "DEEPGRAM_API_KEY": "",
    "DATA_ENCRYPTION_KEY": "",
    "BOOTSTRAP_OPERATOR_USERNAME": "",
    "BOOTSTRAP_OPERATOR_PASSWORD": "",
}
os.environ.update(_PINNED)
