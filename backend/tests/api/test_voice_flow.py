import base64
from datetime import datetime, timezone

import httpx
import pytest

from app.config import Settings
from app.core.errors import AppError
from app.main import create_app
from app.providers.mock_speech import MockSTT, MockTTS

CLOCK = lambda: datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)  # noqa: E731


class FailingSTT:
    name = "failing"

    async def transcribe(self, *a, **k):
        raise AppError("PROVIDER_TIMEOUT", "slow")


class FailingTTS:
    name = "failing"

    async def synthesize(self, *a, **k):
        raise AppError("PROVIDER_UNAVAILABLE", "down")


async def boot(**kw):
    app = create_app(Settings(storage_backend="memory", max_audio_bytes=2000), clock=CLOCK, **kw)
    cm = app.router.lifespan_context(app)
    await cm.__aenter__()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return app, client, cm


async def login(client, customer="CUS-1001"):
    r = await client.post("/api/v1/sessions", json={"consent_microphone": True})
    h = {"Authorization": f"Bearer {r.json()['session_token']}"}
    ch = (await client.post("/api/v1/auth/challenges", json={"phone": "+9199000001" + customer[-2:]}, headers=h)).json()
    await client.post(f"/api/v1/auth/challenges/{ch['challenge_id']}/verify", json={"code": ch["dev_code"]}, headers=h)
    return h


def utter(text, ctype="text/plain"):
    return {"file": ("u.txt", text.encode(), ctype)}


@pytest.fixture
async def env():
    app, client, cm = await boot()
    yield app, client
    await client.aclose()
    await cm.__aexit__(None, None, None)


async def test_voice_turn_end_to_end_with_mock_speech(env):
    app, client = env
    h = await login(client)
    r = await client.post("/api/v1/voice/turn", headers=h, files=utter(
        "I sent four thousand two hundred and fifty rupees yesterday and the recipient has not received it"))
    j = r.json()
    assert r.status_code == 200 and j["transcript"].startswith("I sent four thousand")
    assert j["state"] == "READY_TO_CONFIRM" and "4,250 rupees" in j["reply"]
    assert base64.b64decode(j["audio"]["base64"])[:4] == b"RIFF" and j["audio"]["error"] is None
    assert j["providers"] == {"stt": "mock", "tts": "mock"} and set(j["latency_ms"]) == {"asr", "tts", "total"}
    j2 = (await client.post("/api/v1/voice/turn", headers=h, files=utter("Yes please register it"))).json()
    assert j2["dispute_id"] in j2["reply"]
    assert await app.state.ctx.db.disputes.count_documents({"customer_id": "CUS-1001"}) == 1


async def test_empty_transcript_asks_to_repeat_without_agent(env):
    app, client = env
    h = await login(client)
    j = (await client.post("/api/v1/voice/turn", headers=h, files=utter("   "))).json()
    assert "did not catch" in j["reply"] and j["tool_trace"] == []
    assert await app.state.ctx.db.conversation_turns.count_documents({}) == 0


async def test_transcribe_and_synthesize_endpoints(env):
    _, client = env
    h = await login(client)
    t = (await client.post("/api/v1/voice/transcribe", headers=h, files=utter("hello bank"))).json()
    assert t["text"] == "hello bank" and t["confidence"] is None and t["provider"] == "mock"
    s = await client.post("/api/v1/voice/synthesize", headers=h, json={"text": "Hello"})
    assert s.status_code == 200 and s.headers["content-type"] == "audio/wav" and s.content[:4] == b"RIFF"


async def test_audio_validation(env):
    _, client = env
    h = await login(client)
    bad_type = await client.post("/api/v1/voice/transcribe", headers=h, files=utter("x", "application/pdf"))
    assert bad_type.status_code == 422
    too_big = await client.post("/api/v1/voice/transcribe", headers=h, files=utter("a" * 3000))
    assert too_big.status_code == 422 and too_big.json()["error"]["code"] == "VALIDATION_ERROR"
    empty = await client.post("/api/v1/voice/transcribe", headers=h, files={"file": ("u.txt", b"", "text/plain")})
    assert empty.status_code == 422


async def test_voice_requires_session(env):
    _, client = env
    assert (await client.post("/api/v1/voice/turn", files=utter("hi"))).status_code == 401


async def test_stt_failure_is_safe_no_business_action():
    app, client, cm = await boot(stt=FailingSTT())
    try:
        h = await login(client)
        r = await client.post("/api/v1/voice/turn", headers=h, files=utter("yes confirm the dispute", "audio/webm"))
        assert r.status_code == 504 and r.json()["error"]["code"] == "PROVIDER_TIMEOUT"
        assert r.json()["error"]["retryable"] is True
        assert await app.state.ctx.db.disputes.count_documents({"customer_id": "CUS-1001"}) == 0
    finally:
        await client.aclose()
        await cm.__aexit__(None, None, None)


async def test_tts_failure_keeps_text_reply():
    app, client, cm = await boot(stt=MockSTT(), tts=FailingTTS())
    try:
        h = await login(client)
        j = (await client.post("/api/v1/voice/turn", headers=h, files=utter("I sent 4250 rupees yesterday, not received"))).json()
        assert "4,250 rupees" in j["reply"] and j["audio"] == {"base64": None, "content_type": None,
                                                               "error": "PROVIDER_UNAVAILABLE"}
    finally:
        await client.aclose()
        await cm.__aexit__(None, None, None)


async def test_voice_is_refused_without_microphone_consent(env):
    _, client = env
    s = (await client.post("/api/v1/sessions", json={"consent_microphone": False})).json()
    h = {"Authorization": f"Bearer {s['session_token']}"}
    r = await client.post("/api/v1/voice/transcribe", headers=h, files={"file": ("a.txt", b"hello", "text/plain")})
    assert r.status_code == 403 and r.json()["error"]["details"]["reason_code"] == "MIC_CONSENT_MISSING"
    typed = await client.post("/api/v1/conversation/turn", headers=h, json={"text": "hello"})
    assert typed.status_code == 200  # typing needs no microphone consent


async def test_session_requires_privacy_notice_consent(env):
    _, client = env
    r = await client.post("/api/v1/sessions", json={"consent_notice": False})
    assert r.status_code == 403 and r.json()["error"]["details"]["reason_code"] == "NOTICE_CONSENT_MISSING"
