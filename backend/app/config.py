from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    app_name: str = "KoshVaani"
    app_env: Literal["development", "test", "production"] = "development"
    echo_dev_otp: bool = True  # returns the OTP in the API response for local development only
    # How customers prove who they are: "password" = phone number + password (no SMS needed), "otp" = a texted code
    # through your SMS gateway. The on-screen code of otp_provider="dev_screen" exists only for the automated tests.
    auth_mode: Literal["password", "otp"] = "password"
    password_min_length: int = 10
    password_max_failures: int = 5
    password_lockout_seconds: int = 900
    otp_provider: Literal["dev_screen", "http"] = "dev_screen"  # "http" posts to your SMS gateway
    sms_url: str = ""
    sms_auth_header_name: str = "Authorization"
    sms_auth_header_value: str = ""  # e.g. "Bearer <key>"; never logged
    sms_body_template: str = ""  # JSON with {phone} and {code}
    log_level: str = "INFO"

    frontend_origin: str = "http://localhost:5173"

    # "memory" uses mongomock + fakeredis so the app runs with no external services.
    storage_backend: Literal["memory", "mongo"] = "memory"
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_database: str = "koshvaani"
    mongodb_min_pool: int = 20
    mongodb_max_pool: int = 100
    redis_url: str = "redis://localhost:6379/0"

    llm_provider: Literal["ollama", "rules"] = "rules"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3.5:4b"
    ollama_num_ctx: int = 8192
    ollama_keep_alive: str = "30m"  # keeps the model in memory between calls so a turn never pays the load time
    llm_temperature: float = 0.1
    llm_max_output_tokens: int = 256
    llm_timeout_seconds: float = 30.0
    max_tool_iterations: int = 4

    # Speech providers: "mock" needs no key. Groq (STT) and Deepgram (TTS) both have free keys.
    stt_provider: Literal["mock", "groq"] = "mock"
    tts_provider: Literal["mock", "deepgram"] = "mock"
    speech_language: str = "en-IN"
    speech_timeout_seconds: float = 20.0
    groq_api_key: str = ""
    groq_stt_url: str = "https://api.groq.com/openai/v1/audio/transcriptions"
    groq_stt_model: str = "whisper-large-v3-turbo"
    deepgram_api_key: str = ""
    deepgram_tts_url: str = "https://api.deepgram.com/v1/speak"
    deepgram_tts_model: str = "aura-asteria-en"  # ~1 s; aura-2 voices measured 3-9 s per sentence

    session_ttl_seconds: int = 1800
    auth_ttl_seconds: int = 600
    idempotency_ttl_seconds: int = 86400
    preparation_ttl_seconds: int = 300
    otp_ttl_seconds: int = 180
    otp_max_attempts: int = 3
    # Login-code requests are counted per phone number and per client address, whether or not the number is a
    # customer, so the limit cannot be used to learn who is registered and caps SMS cost abuse.
    otp_requests_per_phone: int = 5
    otp_requests_per_ip: int = 30
    otp_window_seconds: int = 900
    ws_ticket_seconds: int = 30
    max_audio_seconds: int = 20
    max_audio_bytes: int = 10_000_000
    rate_limit_per_minute: int = 120

    # 32 random bytes, base64url. Generate: python -c "from app.core.crypto import generate_key; print(generate_key())"
    # Empty in development gives a throwaway key per process (the in-memory store is wiped anyway).
    data_encryption_key: str = ""

    allow_text_fallback: bool = True
    store_raw_audio: bool = False
    transcript_retention_days: int = 7
    session_retention_days: int = 90  # call metadata; audit events and banking records are kept for regulatory reasons

    operator_session_seconds: int = 3600
    # Optional first-admin bootstrap, applied only while no operator exists. Remove it after first start.
    bootstrap_operator_username: str = ""
    bootstrap_operator_password: str = ""

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    def otp_violations(self, require_https: bool) -> list[str]:
        """Login settings that must never run for real customers. Showing a login code on screen is allowed only in
        the automated tests; in otp mode the code must travel by SMS."""
        v: list[str] = []
        if self.echo_dev_otp:
            v.append("ECHO_DEV_OTP must be false (it returns the login code in the API response)")
        if self.auth_mode != "otp":
            return v
        if self.otp_provider != "http":
            v.append("OTP_PROVIDER must be 'http' with a real SMS gateway (dev_screen delivers nothing)")
        else:
            scheme = "https://" if require_https else "http"
            if not (self.sms_url.startswith(scheme) and self.sms_body_template):
                v.append("SMS_URL" + (" (https)" if require_https else "") + " and SMS_BODY_TEMPLATE must be set for OTP_PROVIDER=http")
        return v

    def production_violations(self) -> list[str]:
        """Everything that must never run against real customers. Empty list means safe to start."""
        v: list[str] = []
        if self.storage_backend != "mongo":
            v.append("STORAGE_BACKEND must be 'mongo' (the in-memory store is fake and loses data)")
        v += self.otp_violations(require_https=True)
        if self.stt_provider == "mock":
            v.append("STT_PROVIDER must be a real speech-to-text provider, not 'mock'")
        if self.tts_provider == "mock":
            v.append("TTS_PROVIDER must be a real text-to-speech provider, not 'mock'")
        if self.llm_provider == "rules":
            v.append("LLM_PROVIDER must be a real model, not the 'rules' stand-in")
        if not self.frontend_origin.startswith("https://"):
            v.append("FRONTEND_ORIGIN must be an https:// origin")
        if not self.data_encryption_key:
            v.append("DATA_ENCRYPTION_KEY must be set (personal data is encrypted at rest with it)")
        if self.store_raw_audio:
            v.append("STORE_RAW_AUDIO must be false (voice recordings are personal data)")
        return v


def assert_safe_for_environment(settings: Settings) -> None:
    """Fail fast at startup: a production process may not run with any development shortcut enabled."""
    if settings.app_env != "test" and not settings.is_production:
        problems = settings.otp_violations(require_https=False)  # a real run never skips the SMS
        if problems:
            lines = "".join(f"\n - {p}" for p in problems)
            raise RuntimeError(f"Refusing to start: unsafe login settings.{lines}")
    if not settings.is_production:
        return
    problems = settings.production_violations()
    if problems:
        lines = "".join(f"\n - {p}" for p in problems)
        raise RuntimeError(f"Refusing to start in production:{lines}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
