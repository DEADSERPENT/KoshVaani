import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.config import Settings
from app.main import create_app


@pytest.fixture
async def app():
    app = create_app(Settings(storage_backend="memory", otp_max_attempts=3))
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def login(client, customer="CUS-1001"):
    r = await client.post("/api/v1/sessions", json={"consent_microphone": True})
    h = {"Authorization": f"Bearer {r.json()['session_token']}"}
    ch = (await client.post("/api/v1/auth/challenges", json={"phone": "+9199000001" + customer[-2:]}, headers=h)).json()
    v = await client.post(f"/api/v1/auth/challenges/{ch['challenge_id']}/verify",
                          json={"code": ch["dev_code"]}, headers=h)
    assert v.status_code == 200
    return h


async def prepare(client, h, txn="TXN-UPI-1001", cat="UPI_P2P_BENEFICIARY_NOT_CREDITED"):
    return await client.post("/api/v1/disputes/prepare", headers=h,
                             json={"transaction_id": txn, "category": cat, "customer_explanation": "not received"})


async def test_health(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    assert (await client.get("/readyz")).status_code == 200


async def test_unauthenticated_transaction_query_rejected(client):
    r = await client.post("/api/v1/sessions", json={"consent_microphone": True})
    h = {"Authorization": f"Bearer {r.json()['session_token']}"}
    res = await client.get("/api/v1/transactions", headers=h)
    assert res.status_code == 401 and res.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert (await client.get("/api/v1/transactions")).status_code == 401


async def test_wrong_otp_does_not_authenticate_and_locks_out(client):
    r = await client.post("/api/v1/sessions", json={"consent_microphone": True})
    h = {"Authorization": f"Bearer {r.json()['session_token']}"}
    ch = (await client.post("/api/v1/auth/challenges", json={"phone": "+919900000101"}, headers=h)).json()
    wrong = "000000" if ch["dev_code"] != "000000" else "111111"
    for _ in range(3):
        v = await client.post(f"/api/v1/auth/challenges/{ch['challenge_id']}/verify", json={"code": wrong}, headers=h)
        assert v.status_code == 401
    assert (await client.get("/api/v1/transactions", headers=h)).status_code == 401
    # challenge is burned: even the right code now fails
    v = await client.post(f"/api/v1/auth/challenges/{ch['challenge_id']}/verify",
                          json={"code": ch["dev_code"]}, headers=h)
    assert v.status_code == 401


async def test_customer_sees_only_own_transactions(client):
    h = await login(client, "CUS-1001")
    res = (await client.get("/api/v1/transactions", headers=h)).json()
    ids = {t["transaction_id"] for t in res["transactions"]}
    assert "TXN-UPI-1001" in ids and ids == {"TXN-UPI-1001", "TXN-NEFT-1001", "TXN-ATM-1001", "TXN-CARD-1001"}
    assert all(t["utr_masked"].startswith("********") for t in res["transactions"])
    other = await client.get("/api/v1/transactions/TXN-UPI-2001", headers=h)
    assert other.status_code == 404 and other.json()["error"]["code"] == "TRANSACTION_NOT_FOUND"


async def test_multiple_matches_flagged_ambiguous(client):
    h = await login(client, "CUS-1002")
    res = (await client.get("/api/v1/transactions", params={"amount_paise": 150000}, headers=h)).json()
    assert res["count"] == 2 and res["ambiguous"] is True


async def test_cross_customer_dispute_and_prepare_blocked(client):
    h_diya = await login(client, "CUS-1002")
    r = await prepare(client, h_diya, txn="TXN-UPI-1001")
    assert r.status_code == 404
    h_aarav = await login(client, "CUS-1001")
    r = await client.get("/api/v1/disputes/KV-20261110-000001", headers=h_aarav)  # belongs to CUS-1004
    unknown = await client.get("/api/v1/disputes/KV-20990101-000009", headers=h_aarav)
    assert r.status_code == 404 and r.json() == {**unknown.json(), "error": {**unknown.json()["error"],
                                                  "request_id": r.json()["error"]["request_id"]}}


async def test_dispute_created_once_with_idempotency(client, app):
    h = await login(client)
    prep = (await prepare(client, h)).json()
    assert prep["eligible"] and "4,250 rupees" in prep["confirmation_text"]
    body = {"preparation_id": prep["preparation_id"], "customer_confirmation": True, "idempotency_key": "key-12345678"}
    first = (await client.post("/api/v1/disputes/confirm", json=body, headers=h)).json()
    assert first["created"] is True and first["dispute_id"].startswith("KV-")
    again = (await client.post("/api/v1/disputes/confirm", json=body, headers=h)).json()
    assert again["created"] is False and again["dispute_id"] == first["dispute_id"]
    # new key + new preparation for same txn: policy blocks a second case at prepare time
    blocked = await prepare(client, h)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "DISPUTE_NOT_ELIGIBLE"
    assert await app.state.ctx.db.disputes.count_documents({"transaction_id": "TXN-UPI-1001"}) == 1
    tracked = (await client.get(f"/api/v1/disputes/{first['dispute_id']}", headers=h)).json()
    assert tracked["status"] == "OPEN"


async def test_same_key_other_preparation_conflicts(client):
    h = await login(client, "CUS-1002")
    p1 = (await prepare(client, h, txn="TXN-UPI-2001")).json()
    p2 = (await prepare(client, h, txn="TXN-UPI-2002")).json()
    key = "shared-key-123"
    ok = await client.post("/api/v1/disputes/confirm", headers=h,
                           json={"preparation_id": p1["preparation_id"], "customer_confirmation": True,
                                 "idempotency_key": key})
    assert ok.status_code == 200
    bad = await client.post("/api/v1/disputes/confirm", headers=h,
                            json={"preparation_id": p2["preparation_id"], "customer_confirmation": True,
                                  "idempotency_key": key})
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


async def test_declined_confirmation_creates_nothing(client, app):
    h = await login(client)
    prep = (await prepare(client, h)).json()
    r = await client.post("/api/v1/disputes/confirm", headers=h,
                          json={"preparation_id": prep["preparation_id"], "customer_confirmation": False,
                                "idempotency_key": "key-declined-1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert await app.state.ctx.db.disputes.count_documents({"customer_id": "CUS-1001"}) == 0


async def test_preparation_cannot_be_used_from_another_session(client):
    h1 = await login(client)
    prep = (await prepare(client, h1)).json()
    h2 = await login(client)  # same customer, different session
    r = await client.post("/api/v1/disputes/confirm", headers=h2,
                          json={"preparation_id": prep["preparation_id"], "customer_confirmation": True,
                                "idempotency_key": "key-othersess-1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "CONFIRMATION_REQUIRED"


async def test_ineligible_successful_transaction_blocked(client):
    h = await login(client, "CUS-1003")
    r = await prepare(client, h, txn="TXN-UPI-3001")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "DISPUTE_NOT_ELIGIBLE"


async def test_existing_dispute_tracked_by_owner(client):
    h = await login(client, "CUS-1004")
    r = (await client.get("/api/v1/disputes/KV-20261110-000001", headers=h)).json()
    assert r["status"] == "UNDER_REVIEW" and "no refund date" in r["timeline_message"]
    assert (await prepare(client, h, txn="TXN-UPI-4001")).status_code == 409


async def test_expired_session_rejected(client, app):
    h = await login(client)
    await app.state.ctx.redis.flushall()
    r = await client.get("/api/v1/transactions", headers=h)
    assert r.status_code == 401 and r.json()["error"]["code"] == "SESSION_EXPIRED"


async def test_redis_down_fails_closed_for_confirm(client, app, monkeypatch):
    h = await login(client)
    prep = (await prepare(client, h)).json()

    async def boom(*a, **k):
        raise RedisConnectionError("down")

    monkeypatch.setattr(app.state.ctx.redis, "get", boom)
    r = await client.post("/api/v1/disputes/confirm", headers=h,
                          json={"preparation_id": prep["preparation_id"], "customer_confirmation": True,
                                "idempotency_key": "key-redisdown-1"})
    assert r.status_code in (401, 503)
    monkeypatch.undo()
    assert await app.state.ctx.db.disputes.count_documents({"customer_id": "CUS-1001"}) == 0


async def test_unexpected_fields_rejected(client):
    h = await login(client)
    r = await client.post("/api/v1/disputes/prepare", headers=h,
                          json={"transaction_id": "TXN-UPI-1001", "category": "OTHER_UPI_ISSUE", "customer_id": "CUS-1002"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_handoff_created(client):
    h = await login(client)
    r = await client.post("/api/v1/handoffs", headers=h, json={"reason": "CUSTOMER_REQUESTED", "summary": "call 9900000101"})
    assert r.status_code == 201 and r.json()["handoff_id"].startswith("HO-")


async def test_error_envelope_carries_safe_details(client):
    h = await login(client, "CUS-1003")
    r = await prepare(client, h, txn="TXN-UPI-3001")
    assert r.json()["error"]["details"] == {"reason_code": "STATUS_NOT_ELIGIBLE", "existing_dispute_id": None}


async def test_phone_login_does_not_reveal_whether_number_is_registered(client):
    async def start(phone):
        s = (await client.post("/api/v1/sessions", json={"consent_microphone": True})).json()
        h = {"Authorization": f"Bearer {s['session_token']}"}
        r = await client.post("/api/v1/auth/challenges", json={"phone": phone}, headers=h)
        return r, h

    known, _ = await start("99000 00101")
    unknown, h = await start("9876543210")
    assert known.status_code == unknown.status_code == 200
    assert set(known.json()) == set(unknown.json())
    bad = await client.post(f"/api/v1/auth/challenges/{unknown.json()['challenge_id']}/verify",
                            json={"code": "123456"}, headers=h)
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "AUTHENTICATION_FAILED"


async def test_phone_login_rejects_malformed_numbers(client):
    s = (await client.post("/api/v1/sessions", json={"consent_microphone": True})).json()
    h = {"Authorization": f"Bearer {s['session_token']}"}
    r = await client.post("/api/v1/auth/challenges", json={"phone": "12345abcde"}, headers=h)
    assert r.status_code == 422


async def test_personal_fields_are_encrypted_at_rest(client):
    doc = await client._transport.app.state.ctx.db.customers.find_one({"customer_id": "CUS-1001"}, {"_id": 0})
    blob = str(doc)
    for plain in ("Aarav", "9900000101", "aarav.test@example.test"):
        assert plain not in blob
    assert doc["name_enc"].startswith("enc1:") and doc["phone_masked"] == "******0101"
