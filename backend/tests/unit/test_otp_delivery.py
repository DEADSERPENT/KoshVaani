import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.providers.otp import HttpSmsSender
from app.core.errors import AppError

SETTINGS = dict(otp_provider="http", sms_url="https://sms.example/send", sms_auth_header_value="Bearer k",
                sms_body_template='{"to": "{phone}", "message": "Your KoshVaani code is {code}."}')


async def test_http_sender_posts_templated_body_with_auth(monkeypatch):
    seen = {}

    def handler(req: httpx.Request):
        seen.update(url=str(req.url), auth=req.headers["authorization"], body=json.loads(req.content))
        return httpx.Response(200, json={"ok": True})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    await HttpSmsSender(Settings(**SETTINGS)).send("+919900000101", "123456")
    assert seen["url"] == "https://sms.example/send" and seen["auth"] == "Bearer k"
    assert seen["body"] == {"to": "+919900000101", "message": "Your KoshVaani code is 123456."}


async def test_http_sender_failure_is_a_typed_error_without_leaking_the_gateway_response(monkeypatch):
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(
        transport=httpx.MockTransport(lambda r: httpx.Response(500, text="secret upstream detail")), **kw))
    with pytest.raises(AppError) as e:
        await HttpSmsSender(Settings(**SETTINGS)).send("+919900000101", "123456")
    assert "secret" not in e.value.message


def test_http_sender_requires_configuration():
    with pytest.raises(ValueError):
        HttpSmsSender(Settings(otp_provider="http"))


class Recorder:
    name, delivery_mode = "recorder", "SMS"

    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send(self, phone, code):
        if self.fail:
            raise AppError("PROVIDER_UNAVAILABLE", "down")
        self.sent.append((phone, code))


async def _challenge(sender, phone):
    app = create_app(Settings(storage_backend="memory", echo_dev_otp=False), otp_sender=sender)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            s = (await c.post("/api/v1/sessions", json={})).json()
            r = await c.post("/api/v1/auth/challenges", json={"phone": phone},
                             headers={"Authorization": f"Bearer {s['session_token']}"})
            await asyncio.sleep(0.05)  # let the background delivery run
            return r


async def test_code_is_sent_by_sms_only_for_registered_numbers_and_never_returned():
    rec = Recorder()
    known = await _challenge(rec, "9900000101")
    assert known.json()["dev_code"] is None and known.json()["delivery_mode"] == "SMS"
    assert len(rec.sent) == 1 and rec.sent[0][0] == "+919900000101" and len(rec.sent[0][1]) == 6
    rec2 = Recorder()
    unknown = await _challenge(rec2, "9876543210")
    assert rec2.sent == [] and set(unknown.json()) == set(known.json())


async def test_gateway_failure_looks_identical_to_the_caller():
    r = await _challenge(Recorder(fail=True), "9900000101")
    assert r.status_code == 200 and r.json()["dev_code"] is None
