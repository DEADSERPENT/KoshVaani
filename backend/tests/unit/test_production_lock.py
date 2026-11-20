import pytest

from app.config import Settings, assert_safe_for_environment
from app.main import create_app

from app.core.crypto import generate_key

GOOD = dict(app_env="production", data_encryption_key=generate_key(), storage_backend="mongo", echo_dev_otp=False, stt_provider="groq",
            tts_provider="deepgram", llm_provider="ollama",
            frontend_origin="https://bank.example", store_raw_audio=False)


SMS_DEV = dict(otp_provider="http", sms_url="http://localhost:9000/send", sms_body_template='{"to":"{phone}","m":"{code}"}',
               echo_dev_otp=False)


def test_a_real_run_never_skips_the_sms():
    for env in ("development", "production"):
        with pytest.raises(RuntimeError) as e:
            assert_safe_for_environment(Settings(app_env=env, otp_provider="dev_screen", echo_dev_otp=True))
        assert "ECHO_DEV_OTP" in str(e.value) and "OTP_PROVIDER" in str(e.value)


def test_development_starts_with_a_configured_sms_gateway():
    assert_safe_for_environment(Settings(app_env="development", **SMS_DEV))


def test_development_with_http_provider_still_needs_the_gateway_url():
    with pytest.raises(RuntimeError) as e:
        assert_safe_for_environment(Settings(app_env="development", otp_provider="http", echo_dev_otp=False))
    assert "SMS_URL" in str(e.value)


def test_only_the_test_environment_may_show_the_code_on_screen():
    assert_safe_for_environment(Settings(app_env="test", otp_provider="dev_screen", echo_dev_otp=True))


def test_production_refuses_every_development_shortcut():
    with pytest.raises(RuntimeError) as e:
        assert_safe_for_environment(Settings(app_env="production"))
    msg = str(e.value)
    for needle in ("STORAGE_BACKEND", "ECHO_DEV_OTP", "OTP_PROVIDER", "STT_PROVIDER", "TTS_PROVIDER",
                   "LLM_PROVIDER", "FRONTEND_ORIGIN"):
        assert needle in msg


@pytest.mark.parametrize("field,value,needle", [
    ("echo_dev_otp", True, "ECHO_DEV_OTP"), ("storage_backend", "memory", "STORAGE_BACKEND"),
    ("stt_provider", "mock", "STT_PROVIDER"), ("llm_provider", "rules", "LLM_PROVIDER"),
    ("frontend_origin", "http://bank.example", "FRONTEND_ORIGIN"), ("store_raw_audio", True, "STORE_RAW_AUDIO"),
])
def test_each_single_violation_is_caught(field, value, needle):
    problems = Settings(**{**GOOD, field: value}).production_violations()
    assert any(needle in p for p in problems)


SMS = dict(otp_provider="http", sms_url="https://sms.example/send", sms_body_template='{"to":"{phone}","m":"{code}"}')


def test_fully_configured_production_settings_pass():
    assert Settings(**GOOD, **SMS).production_violations() == []


def test_production_needs_a_real_sms_gateway():
    assert any("OTP_PROVIDER" in p for p in Settings(**GOOD, otp_provider="dev_screen").production_violations())
    assert any("SMS_URL" in p for p in Settings(**GOOD, otp_provider="http", sms_url="http://insecure").production_violations())


def test_missing_encryption_key_blocks_production():
    assert any("DATA_ENCRYPTION_KEY" in p for p in Settings(**{**GOOD, "data_encryption_key": ""}).production_violations())


def test_app_will_not_boot_in_production_with_dev_config():
    with pytest.raises(RuntimeError):
        create_app(Settings(app_env="production"))
