import httpx
import pytest

from app.config import Settings
from app.main import create_app


async def make_client(**overrides):
    app = create_app(Settings(storage_backend="memory", **overrides))
    ctx = app.router.lifespan_context(app)
    await ctx.__aenter__()
    return app, ctx, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def new_session(client):
    r = await client.post("/api/v1/sessions", json={"consent_microphone": True, "consent_notice": True})
    return {"Authorization": f"Bearer {r.json()['session_token']}"}


async def test_every_response_is_uncacheable_and_carries_security_headers():
    app, ctx, client = await make_client()
    try:
        for path in ("/healthz", "/readyz"):
            r = await client.get(path)
            assert r.headers["cache-control"] == "no-store"
            assert r.headers["x-content-type-options"] == "nosniff"
            assert r.headers["x-frame-options"] == "DENY"
            assert r.headers["referrer-policy"] == "no-referrer"
            assert "default-src 'none'" in r.headers["content-security-policy"]
        # an error response (no session) is protected too
        assert (await client.get("/api/v1/accounts")).headers["cache-control"] == "no-store"
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)


async def test_cors_is_limited_to_the_frontend_origin_and_needed_methods():
    app, ctx, client = await make_client(frontend_origin="https://bank.example")
    try:
        ok = await client.options("/api/v1/sessions", headers={
            "Origin": "https://bank.example", "Access-Control-Request-Method": "POST"})
        assert ok.headers["access-control-allow-origin"] == "https://bank.example"
        other = await client.options("/api/v1/sessions", headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
        assert "access-control-allow-origin" not in other.headers
        put = await client.options("/api/v1/sessions", headers={
            "Origin": "https://bank.example", "Access-Control-Request-Method": "PUT"})
        assert put.status_code == 400
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)


async def test_production_has_no_docs_and_sends_hsts():
    env = dict(app_env="production", storage_backend="mongo", echo_dev_otp=False, otp_provider="http",
               sms_url="https://sms.example/send", sms_body_template="{}", stt_provider="groq", tts_provider="deepgram",
               llm_provider="ollama", frontend_origin="https://bank.example", data_encryption_key="k" * 43)
    app = create_app(Settings(**env))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await client.get(path)).status_code == 404
        assert "max-age" in (await client.get("/healthz")).headers["strict-transport-security"]


async def test_login_code_requests_are_limited_per_number_and_look_the_same_for_unknown_numbers():
    app, ctx, client = await make_client(otp_requests_per_phone=3, otp_requests_per_ip=100)
    try:
        h = await new_session(client)
        for phone in ("+919900000101", "+919123456789"):  # registered and unregistered behave identically
            codes = []
            for _ in range(5):
                r = await client.post("/api/v1/auth/challenges", json={"phone": phone}, headers=h)
                codes.append(r.status_code)
            assert codes == [200, 200, 200, 429, 429], phone
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)


async def test_login_code_requests_are_limited_per_client_address():
    app, ctx, client = await make_client(otp_requests_per_phone=100, otp_requests_per_ip=2)
    try:
        h = await new_session(client)
        statuses = [(await client.post("/api/v1/auth/challenges", json={"phone": f"+9199000001{i:02d}"}, headers=h)).status_code
                    for i in range(1, 5)]
        assert statuses == [200, 200, 429, 429]
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)
