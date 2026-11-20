import httpx
import pytest

from app.config import Settings
from app.core.errors import AppError
from app.providers.deepgram_tts import DeepgramTTS
from app.providers.groq_stt import GroqWhisperSTT
from app.providers.resilience import CircuitBreaker
from app.providers.mock_speech import MockSTT, MockTTS

S = Settings(groq_api_key="gsk_SECRET", deepgram_api_key="dg_SECRET")


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_groq_request_shape_and_normalized_result():
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers["authorization"]
        seen["body"] = req.read()
        return httpx.Response(200, json={"text": " I sent four thousand two hundred and fifty rupees. ",
                                         "language": "english", "duration": 3.2},
                              headers={"x-request-id": "req_abc"})

    stt = GroqWhisperSTT(S, client(handler))
    r = await stt.transcribe(b"RIFFdata", content_type="audio/webm;codecs=opus", language="en-IN")
    assert r.text == "I sent four thousand two hundred and fifty rupees."
    assert r.provider == "groq_whisper" and r.confidence is None and r.request_id == "req_abc"
    assert seen["auth"] == "Bearer gsk_SECRET"
    for part in (b'name="model"', b"whisper-large-v3-turbo", b'name="language"', b"verbose_json", b"utterance.webm"):
        assert part in seen["body"]
    assert b'\r\n\r\nen\r\n' in seen["body"]  # en-IN is sent as ISO-639-1 "en"


async def test_groq_rejects_unsupported_format_and_missing_key():
    stt = GroqWhisperSTT(S, client(lambda r: httpx.Response(200, json={"text": "x"})))
    with pytest.raises(AppError) as e:
        await stt.transcribe(b"x", content_type="application/pdf")
    assert e.value.code == "VALIDATION_ERROR"
    with pytest.raises(AppError):
        GroqWhisperSTT(Settings(groq_api_key=""))


async def test_deepgram_request_shape():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers["authorization"], req.read()
        return httpx.Response(200, content=b"ID3audio", headers={"content-type": "audio/mpeg", "dg-request-id": "dg1"})

    tts = DeepgramTTS(S, client(handler))
    out = await tts.synthesize("Your complaint is registered.")
    assert out.audio == b"ID3audio" and out.request_id == "dg1" and out.content_type == "audio/mpeg"
    assert "model=aura-asteria-en" in seen["url"] and "encoding=mp3" in seen["url"]
    assert seen["auth"] == "Token dg_SECRET" and b"Your complaint is registered." in seen["body"]
    with pytest.raises(AppError):
        await tts.synthesize("   ")


async def test_5xx_retried_once_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json={"text": "hello"})

    stt = GroqWhisperSTT(S, client(handler))
    assert (await stt.transcribe(b"a", content_type="audio/wav")).text == "hello"
    assert len(calls) == 2


@pytest.mark.parametrize("status,code", [(401, "PROVIDER_UNAVAILABLE"), (429, "RATE_LIMITED"), (400, "PROVIDER_UNAVAILABLE")])
async def test_client_errors_not_retried_and_do_not_leak_key(status, code):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(status, text="bad key gsk_SECRET")

    stt = GroqWhisperSTT(S, client(handler))
    with pytest.raises(AppError) as e:
        await stt.transcribe(b"a", content_type="audio/wav")
    assert e.value.code == code and len(calls) == 1 and "gsk_SECRET" not in e.value.message


async def test_timeout_maps_and_circuit_opens_then_recovers():
    now = [0.0]
    calls = []

    def handler(req):
        calls.append(1)
        raise httpx.ReadTimeout("slow")

    tts = DeepgramTTS(S, client(handler))
    tts.breaker = CircuitBreaker(threshold=2, cooldown=30, clock=lambda: now[0])
    for _ in range(2):
        with pytest.raises(AppError) as e:
            await tts.synthesize("hi")
        assert e.value.code == "PROVIDER_TIMEOUT"
    n = len(calls)
    with pytest.raises(AppError) as e:  # open: fails fast, provider not called
        await tts.synthesize("hi")
    assert e.value.code == "PROVIDER_UNAVAILABLE" and len(calls) == n
    now[0] = 31.0  # cool-down elapsed: a probe is allowed again
    with pytest.raises(AppError) as e:
        await tts.synthesize("hi")
    assert e.value.code == "PROVIDER_TIMEOUT" and len(calls) > n


async def test_mock_providers():
    assert (await MockSTT().transcribe(b"hello there", content_type="text/plain")).text == "hello there"
    out = await MockTTS().synthesize("hi")
    assert out.audio[:4] == b"RIFF" and out.content_type == "audio/wav"
