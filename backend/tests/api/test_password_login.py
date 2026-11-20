import httpx
import pytest

from app.banking.operator_service import hash_password
from app.config import Settings
from app.main import create_app
from app.repositories.customers import CustomerRepository

PHONE = "9900000101"
GOOD = "correct-horse-battery"


@pytest.fixture
async def app():
    app = create_app(Settings(storage_backend="memory", auth_mode="password", password_max_failures=3,
                              otp_requests_per_ip=1000))
    async with app.router.lifespan_context(app):
        ctx = app.state.ctx
        repo = CustomerRepository(ctx.db, ctx.cipher)
        await repo.set_password("CUS-1001", hash_password(GOOD), False)
        await repo.set_password("CUS-1002", hash_password("temp-Passw0rd-xyz"), True)  # must change on first login
        yield app


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def session(client):
    r = await client.post("/api/v1/sessions", json={"consent_microphone": True, "consent_notice": True})
    return {"Authorization": f"Bearer {r.json()['session_token']}"}


async def login(client, phone=PHONE, password=GOOD, h=None):
    h = h or await session(client)
    return h, await client.post("/api/v1/auth/login", json={"phone": phone, "password": password}, headers=h)


async def test_correct_phone_and_password_signs_in_and_opens_the_account(client):
    h, r = await login(client)
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] and not body["must_change_password"] and body["phone_masked"] == "******0101"
    assert (await client.get("/api/v1/accounts", headers=h)).status_code == 200


async def test_no_data_before_login(client):
    h = await session(client)
    assert (await client.get("/api/v1/accounts", headers=h)).status_code == 401


async def test_all_failures_look_identical(client):
    results = []
    for phone, pw in ((PHONE, "wrong-password-1"), ("9123456789", GOOD), ("9900000103", GOOD)):  # wrong pw, unknown, no pw set
        _, r = await login(client, phone, pw)
        results.append((r.status_code, r.json()["error"]["code"], r.json()["error"]["message"]))
    assert len(set(results)) == 1 and results[0][0] == 401


async def test_wrong_passwords_lock_the_number_even_for_the_right_password(client):
    for _ in range(3):
        _, r = await login(client, PHONE, "wrong-password-1")
        assert r.status_code == 401
    _, r = await login(client, PHONE, GOOD)
    assert r.status_code == 429


async def test_lockout_is_per_number(client):
    for _ in range(3):
        await login(client, PHONE, "wrong-password-1")
    h, r = await login(client, "9900000102", "temp-Passw0rd-xyz")
    assert r.status_code == 200


async def test_temporary_password_signs_in_but_opens_nothing_until_changed(client):
    h, r = await login(client, "9900000102", "temp-Passw0rd-xyz")
    assert r.status_code == 200 and r.json()["must_change_password"] and not r.json()["authenticated"]
    assert (await client.get("/api/v1/accounts", headers=h)).status_code == 401
    turn = (await client.post("/api/v1/conversation/turn", json={"text": "what is my balance"}, headers=h)).json()
    assert "verify your identity" in turn["reply"] and turn["tool_trace"] == [] and "rupees" not in turn["reply"]
    ch = await client.post("/api/v1/auth/password", headers=h,
                           json={"current_password": "temp-Passw0rd-xyz", "new_password": "a-much-better-passphrase-7"})
    assert ch.status_code == 200
    assert (await client.get("/api/v1/accounts", headers=h)).status_code == 200
    # the old password is gone and the new one works from a fresh session
    assert (await login(client, "9900000102", "temp-Passw0rd-xyz"))[1].status_code == 401
    assert (await login(client, "9900000102", "a-much-better-passphrase-7"))[1].json()["must_change_password"] is False


@pytest.mark.parametrize("new,fragment", [
    ("short1", "at least"), ("1234567890123", "too easy"), ("aaaaaaaaaaaa", "too easy"),
    ("x9900000101y-long", "phone number"), (GOOD, "different"),
])
async def test_weak_new_passwords_are_rejected(client, new, fragment):
    h, _ = await login(client)
    r = await client.post("/api/v1/auth/password", headers=h, json={"current_password": GOOD, "new_password": new})
    assert r.status_code == 422 and fragment in r.json()["error"]["message"]


async def test_changing_the_password_needs_the_current_one(client):
    h, _ = await login(client)
    r = await client.post("/api/v1/auth/password", headers=h,
                          json={"current_password": "not-it-at-all", "new_password": "a-much-better-passphrase-7"})
    assert r.status_code == 401
    assert (await login(client))[1].status_code == 200  # unchanged


async def test_password_is_stored_only_as_a_hash(app, client):
    doc = await app.state.ctx.db.customers.find_one({"customer_id": "CUS-1001"})
    assert doc["password_hash"].startswith("scrypt$") and GOOD not in str(doc)


async def test_login_and_failures_are_audited_without_the_password(app, client):
    await login(client, PHONE, "wrong-password-1")
    await login(client)
    events = [e async for e in app.state.ctx.db.audit_events.find({"action": "AUTH_LOGIN"})]
    assert {e["reason_code"] for e in events} == {"BAD_CREDENTIALS", "PASSWORD_OK"}
    assert "wrong-password-1" not in str(events) and GOOD not in str(events)


async def test_otp_endpoints_are_disabled_in_password_mode(client):
    h = await session(client)
    r = await client.post("/api/v1/auth/challenges", json={"phone": PHONE}, headers=h)
    assert r.status_code == 403


async def test_password_login_is_disabled_in_otp_mode():
    app = create_app(Settings(storage_backend="memory", auth_mode="otp"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            h = await session(c)
            r = await c.post("/api/v1/auth/login", json={"phone": PHONE, "password": GOOD}, headers=h)
            assert r.status_code == 403
