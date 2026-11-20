import base64
import json
import logging
from datetime import datetime, timezone

import httpx
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.main import create_app
from app.providers.mock_speech import MockSTT

CLOCK = lambda: datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)  # noqa: E731
ADMIN_PW = "correct-horse-battery"
SETTINGS = dict(storage_backend="memory", bootstrap_operator_username="ada", bootstrap_operator_password=ADMIN_PW)


def ops_headers(c, username="ada", password=ADMIN_PW):
    r = c.post("/api/v1/ops/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def tc():
    app = create_app(Settings(**SETTINGS, max_audio_bytes=100_000), clock=CLOCK)
    with TestClient(app) as c:
        yield c


def login(c, customer="CUS-1001"):
    r = c.post("/api/v1/sessions", json={"consent_microphone": True}).json()
    h = {"Authorization": f"Bearer {r['session_token']}"}
    if customer:
        ch = c.post("/api/v1/auth/challenges", json={"phone": "+9199000001" + customer[-2:]}, headers=h).json()
        c.post(f"/api/v1/auth/challenges/{ch['challenge_id']}/verify", json={"code": ch["dev_code"]}, headers=h)
    return r["session_id"], r["session_token"], h


def collect(ws, until):
    """Reads events until one of type `until` arrives; returns all events seen."""
    events = []
    while True:
        e = ws.receive_json()
        events.append(e)
        if e["type"] == until:
            return events


def types(events):
    return [e["type"] for e in events]


def stream_url(tc, sid, token):
    t = tc.post("/api/v1/voice/ws-ticket", headers={"Authorization": f"Bearer {token}"})
    assert t.status_code == 200, t.text
    return f"/api/v1/voice/sessions/{sid}/stream?ticket={t.json()['ticket']}"


def test_ws_rejects_bad_ticket_mismatched_session_and_the_session_token(tc):
    sid, token, _ = login(tc)
    ticket = stream_url(tc, sid, token).split("ticket=")[1]
    for url in (f"/api/v1/voice/sessions/{sid}/stream?ticket=nope",
                f"/api/v1/voice/sessions/VS-OTHER/stream?ticket={ticket}",
                f"/api/v1/voice/sessions/{sid}/stream?token={token}",  # the long-lived token no longer opens a stream
                f"/api/v1/voice/sessions/{sid}/stream"):
        with pytest.raises(WebSocketDisconnect) as e:
            with tc.websocket_connect(url):
                pass
        assert e.value.code == 4401


def test_ws_ticket_is_single_use(tc):
    sid, token, _ = login(tc)
    url = stream_url(tc, sid, token)
    with tc.websocket_connect(url) as ws:
        assert ws.receive_json()["type"] == "session.ready"
    with pytest.raises(WebSocketDisconnect) as e:
        with tc.websocket_connect(url):
            pass
    assert e.value.code == 4401


def test_ws_full_voice_flow(tc):
    sid, token, _ = login(tc)
    with tc.websocket_connect(stream_url(tc, sid, token)) as ws:
        assert ws.receive_json() == {"type": "session.ready", "session_id": sid, "authenticated": True}
        ws.send_json({"type": "audio.start", "content_type": "text/plain"})
        utterance = b"I sent four thousand two hundred and fifty rupees yesterday, the recipient has not received it"
        for i in range(0, len(utterance), 40):
            ws.send_bytes(utterance[i:i + 40])
        ws.send_json({"type": "audio.end"})
        ev = collect(ws, "turn.completed")
        t = types(ev)
        assert "transcript.final" in t and "assistant.message" in t and "audio.chunk" in t
        assert t.index("transcript.final") < t.index("assistant.message") < t.index("audio.chunk")
        msg = next(e for e in ev if e["type"] == "assistant.message")
        assert msg["state"] == "READY_TO_CONFIRM" and "4,250 rupees" in msg["text"]
        audio = b"".join(base64.b64decode(e["data"]) for e in ev if e["type"] == "audio.chunk")
        assert audio[:4] == b"RIFF" and [e for e in ev if e["type"] == "audio.chunk"][-1]["final"] is True
        pending = msg["preparation_id"]
        assert pending.startswith("PREP-")
        ws.send_json({"type": "client.confirmation", "value": True, "preparation_id": pending})
        ev = collect(ws, "turn.completed")
        created = next(e for e in ev if e["type"] == "case.created")
        assert created["dispute_id"].startswith("KV-")
        ws.send_json({"type": "session.stop"})
        done = collect(ws, "session.completed")[-1]
        assert done["outcome"] == "DISPUTE_CREATED"


def test_ws_text_fallback_ping_and_malformed(tc):
    sid, token, _ = login(tc)
    with tc.websocket_connect(stream_url(tc, sid, token)) as ws:
        collect(ws, "assistant.state")
        ws.send_json({"type": "ping"})
        assert collect(ws, "pong")
        ws.send_text("not json")
        assert collect(ws, "error")[-1]["code"] == "VALIDATION_ERROR"
        ws.send_json({"type": "bogus"})
        assert collect(ws, "error")[-1]["code"] == "VALIDATION_ERROR"
        ws.send_bytes(b"audio before start")
        assert collect(ws, "error")[-1]["code"] == "VALIDATION_ERROR"
        ws.send_json({"type": "text.input", "text": "I want to talk to a human agent"})
        ev = collect(ws, "turn.completed")
        assert any(e["type"] == "handoff.created" for e in ev)


def test_ws_unauthenticated_gets_no_data_and_confirmation_needs_pending(tc):
    sid, token, _ = login(tc, customer=None)
    with tc.websocket_connect(stream_url(tc, sid, token)) as ws:
        assert ws.receive_json()["authenticated"] is False
        ws.send_json({"type": "text.input", "text": "I sent 4250 rupees yesterday, failed"})
        ev = collect(ws, "turn.completed")
        assert not any(e["type"] == "tool.result" for e in ev)
        ws.send_json({"type": "client.confirmation", "value": True, "preparation_id": "PREP-1"})
        assert collect(ws, "error")[-1]["code"] == "CONFIRMATION_REQUIRED"


def test_ws_audio_buffer_is_bounded(tc):
    sid, token, _ = login(tc)
    with tc.websocket_connect(stream_url(tc, sid, token)) as ws:
        collect(ws, "assistant.state")
        ws.send_json({"type": "audio.start", "content_type": "text/plain"})
        ws.send_bytes(b"x" * (64 * 1024 + 1))  # over the per-frame limit
        assert "too large" in collect(ws, "error")[-1]["message"]
        ws.send_json({"type": "audio.start", "content_type": "text/plain"})
        for _ in range(2):
            ws.send_bytes(b"x" * 60_000)  # 120 KB total > 100 KB cap
        assert "too long" in collect(ws, "error")[-1]["message"]


def test_end_session_clears_auth_and_blocks_other_sessions(tc):
    sid, _, h = login(tc)
    _, _, h2 = login(tc)
    assert tc.post(f"/api/v1/sessions/{sid}/end", headers=h2).status_code == 403
    assert tc.get(f"/api/v1/sessions/{sid}", headers=h).json()["authenticated"] is True
    assert tc.post(f"/api/v1/sessions/{sid}/end", headers=h).status_code == 200
    assert tc.get("/api/v1/transactions", headers=h).status_code == 401  # session keys are gone


def test_ops_requires_operator_sign_in(tc):
    assert tc.get("/api/v1/ops/overview").status_code == 401
    assert tc.get("/api/v1/ops/overview", headers={"Authorization": "Bearer nope"}).status_code == 401
    _, _, h = login(tc)  # a customer session token is not an operator credential
    assert tc.get("/api/v1/ops/overview", headers=h).status_code == 401


def test_operator_login_rejects_bad_credentials_without_revealing_which(tc):
    wrong_pw = tc.post("/api/v1/ops/auth/login", json={"username": "ada", "password": "x" * 14})
    no_user = tc.post("/api/v1/ops/auth/login", json={"username": "ghost", "password": "x" * 14})
    assert wrong_pw.status_code == no_user.status_code == 401
    assert wrong_pw.json()["error"]["message"] == no_user.json()["error"]["message"]


def test_operator_account_locks_after_repeated_failures(tc):
    for _ in range(5):
        assert tc.post("/api/v1/ops/auth/login", json={"username": "ada", "password": "x" * 14}).status_code == 401
    assert tc.post("/api/v1/ops/auth/login", json={"username": "ada", "password": ADMIN_PW}).status_code == 429


def test_logout_revokes_the_token(tc):
    h = ops_headers(tc)
    assert tc.get("/api/v1/ops/overview", headers=h).status_code == 200
    assert tc.post("/api/v1/ops/auth/logout", headers=h).status_code == 200
    assert tc.get("/api/v1/ops/overview", headers=h).status_code == 401


def test_roles_limit_what_an_operator_can_do(tc):
    from app.banking.operator_service import OperatorService
    svc = OperatorService(tc.app.state.ctx)
    tc.portal.call(svc.create, "viewer1", "another-long-password", "VIEWER")
    v = ops_headers(tc, "viewer1", "another-long-password")
    assert tc.get("/api/v1/ops/overview", headers=v).status_code == 200
    assert tc.get("/api/v1/ops/handoffs", headers=v).status_code == 403
    assert tc.get("/api/v1/ops/sessions/anything", headers=v).status_code == 403


def test_operator_actions_are_audited_with_the_operator_identity(tc):
    sid, _, h = login(tc)
    tc.get(f"/api/v1/ops/sessions/{sid}", headers=ops_headers(tc))
    events = tc.portal.call(lambda: tc.app.state.ctx.db.audit_events.find_one(
        {"action": "OPERATOR_VIEWED_SESSION"}, {"_id": 0}))
    assert events["actor_id"].startswith("OP-") and events["actor_type"] == "OPERATOR"


def test_ops_numbers_derive_from_events_and_detail_is_masked(tc):
    sid, token, h = login(tc)
    # drive a full case through the REST turn endpoint
    tc.post("/api/v1/conversation/turn", headers=h, json={"text": "I sent 4250 rupees yesterday, recipient has not received it"})
    tc.post("/api/v1/conversation/turn", headers=h, json={"text": "My number is 9900000101. Yes please register it."})
    sid2, _, h2 = login(tc, "CUS-1003")
    tc.post("/api/v1/conversation/turn", headers=h2, json={"text": "I paid 999 rupees yesterday and nothing arrived"})
    sid3, _, h3 = login(tc, "CUS-1002")
    tc.post("/api/v1/conversation/turn", headers=h3, json={"text": "I want a human agent"})

    o = tc.get("/api/v1/ops/overview", headers=ops_headers(tc)).json()
    assert o["sessions"] == 3 and o["disputes_created"] == 1 and o["resolved_by_self_service"] == 1
    assert o["human_handoffs"] == 1 and o["blocked_policy_attempts"] == 1
    assert o["sessions_by_outcome"]["DISPUTE_CREATED"] == 1 and o["handoff_reasons"] == {"CUSTOMER_REQUESTED": 1}
    assert o["latency_ms"]["samples"] >= 4 and o["latency_ms"]["p95"] is not None
    assert o["providers"] == {"llm": "rules", "stt": "mock", "tts": "mock"}

    lst = tc.get("/api/v1/ops/sessions", headers=ops_headers(tc), params={"outcome": "DISPUTE_CREATED"}).json()
    assert [s["session_id"] for s in lst["sessions"]] == [sid]

    d = tc.get(f"/api/v1/ops/sessions/{sid}", headers=ops_headers(tc)).json()
    assert "9900000101" not in json.dumps(d)  # redacted before storage
    assert [t["tool"] for t in d["tool_timeline"]] == ["search_customer_transactions", "prepare_dispute", "confirm_dispute"]
    assert d["disputes"][0]["dispute_id"].startswith("KV-") and "idempotency_key_hash" not in d["disputes"][0]
    assert d["matched_transactions"][0]["utr_masked"].startswith("********")
    assert any(a["action"] == "DISPUTE_CREATED" for a in d["audit"])
    again = tc.get(f"/api/v1/ops/sessions/{sid}", headers=ops_headers(tc)).json()
    assert any(a["action"] == "OPERATOR_VIEWED_SESSION" for a in again["audit"])  # reads are audited

    m = tc.get("/api/v1/ops/metrics", headers=ops_headers(tc))
    assert "koshvaani_disputes_created_total 1" in m.text

    q = tc.get("/api/v1/ops/handoffs", headers=ops_headers(tc), params={"status": "QUEUED"}).json()
    hid = q["handoffs"][0]["handoff_id"]
    assert tc.post(f"/api/v1/ops/handoffs/{hid}/status", headers=ops_headers(tc), json={"status": "RESOLVED"}).status_code == 200
    assert tc.get("/api/v1/ops/handoffs", headers=ops_headers(tc), params={"status": "QUEUED"}).json()["count"] == 0


def test_provider_errors_counted_in_ops():
    class Down(MockSTT):
        name = "down"

        async def transcribe(self, *a, **k):
            from app.core.errors import AppError
            raise AppError("PROVIDER_TIMEOUT", "slow")

    app = create_app(Settings(**SETTINGS), clock=CLOCK, stt=Down())
    with TestClient(app) as c:
        _, _, h = login(c)
        r = c.post("/api/v1/voice/transcribe", headers=h, files={"file": ("a.webm", b"zz", "audio/webm")})
        r = c.post("/api/v1/voice/turn", headers=h, files={"file": ("a.txt", b"zz", "text/plain")})
        assert r.status_code == 504
        assert c.get("/api/v1/ops/overview", headers=ops_headers(c)).json()["provider_errors"] == 1
        assert c.get("/readyz").json()["providers"]["stt"] == "down"


def test_request_id_header_and_no_secrets_in_logs(caplog):
    from app.providers.groq_stt import GroqWhisperSTT

    secret = "gsk_TOPSECRET"
    caplog.set_level(logging.DEBUG)
    transport = httpx.MockTransport(lambda r: httpx.Response(401, text=f"invalid key {secret}"))
    stt = GroqWhisperSTT(Settings(groq_api_key=secret), httpx.AsyncClient(transport=transport))
    app = create_app(Settings(storage_backend="memory", groq_api_key=secret), clock=CLOCK, stt=stt)
    with TestClient(app) as c:
        _, _, h = login(c)
        r = c.post("/api/v1/voice/turn", headers=h, files={"file": ("a.webm", b"zz", "audio/webm")})
        assert r.status_code == 503 and r.headers["x-request-id"].startswith("req-")
        assert secret not in r.text
    assert secret not in caplog.text


def test_erasure_removes_personal_data_keeps_banking_records_and_needs_admin(tc):
    sid, _, h = login(tc)
    tc.post("/api/v1/conversation/turn", headers=h, json={"text": "I sent 4250 rupees yesterday, not received"})
    db = tc.app.state.ctx.db

    def count(col, q):
        return tc.portal.call(lambda: db[col].count_documents(q))

    tc.portal.call(lambda: __import__("app.banking.operator_service", fromlist=["x"]).OperatorService(
        tc.app.state.ctx).create("agent1", "agent-password-123", "AGENT"))
    assert tc.post("/api/v1/ops/customers/CUS-1001/erase", headers=ops_headers(tc, "agent1", "agent-password-123")).status_code == 403

    assert count("conversation_turns", {"session_id": sid}) > 0
    r = tc.post("/api/v1/ops/customers/CUS-1001/erase", headers=ops_headers(tc))
    assert r.status_code == 200 and r.json()["turns_deleted"] > 0
    assert count("conversation_turns", {"session_id": sid}) == 0
    assert count("transactions", {"customer_id": "CUS-1001"}) > 0  # regulated records stay
    doc = tc.portal.call(lambda: db.customers.find_one({"customer_id": "CUS-1001"}, {"_id": 0}))
    assert doc["status"] == "ERASED" and not any(k in doc for k in ("name_enc", "phone_enc", "email_enc"))
    # the erased number can no longer log in (indistinguishable from an unregistered number)
    s2 = tc.post("/api/v1/sessions", json={"consent_microphone": True}).json()
    h2 = {"Authorization": f"Bearer {s2['session_token']}"}
    ch = tc.post("/api/v1/auth/challenges", json={"phone": "+919900000101"}, headers=h2).json()
    assert ch["dev_code"] is None
    assert tc.post("/api/v1/ops/customers/NOPE/erase", headers=ops_headers(tc)).status_code == 404
