"""Balance, mini statement, card block and loan enquiries: same safety rules as disputes."""
import pytest

from app.agents.response_guard import grounded
from app.core.money import spoken_amount
from app.providers.base import LLMReply, ToolCall
from tests.api.test_agent_flow import ScriptedLLM, boot, env, say, session  # noqa: F401
from tests.api.test_guardrails import SAFE, run


async def card_status(app, card_id):
    doc = await app.state.ctx.db.cards.find_one({"card_id": card_id}, {"_id": 0})
    return doc["status"], doc.get("blocked_reason")


# ---------- balance ----------

async def test_balance_enquiry_is_grounded_and_audited(env):
    app, client = env
    h = await session(client)
    t = await say(client, h, "What is my account balance?")
    assert t["intent"] == "BALANCE_ENQUIRY" and [x["tool"] for x in t["tool_trace"]] == ["get_account_balance"]
    assert "ending 4821" in t["reply"] and "85,234 rupees 50 paise" in t["reply"]
    assert "84,334 rupees 50 paise is available" in t["reply"]  # card authorisation on hold
    assert await app.state.ctx.db.audit_events.count_documents({"action": "BALANCE_VIEWED"}) == 1
    assert await app.state.ctx.db.voice_sessions.count_documents({"outcome": "INFORMATION_PROVIDED"}) == 1


async def test_balance_needs_verification_and_calls_no_tool(env):
    _, client = env
    h = await session(client, customer=None)
    t = await say(client, h, "What is my balance?")
    assert t["tool_trace"] == [] and "verify" in t["reply"] and "rupees" not in t["reply"]


async def test_each_customer_only_ever_hears_their_own_balance(env):
    _, client = env
    a = await say(client, await session(client, "CUS-1001"), "balance please")
    b = await say(client, await session(client, "CUS-1004"), "balance please")
    assert "ending 4821" in a["reply"] and "ending 7754" not in a["reply"]
    assert "965 rupees" in b["reply"] and "85,234" not in b["reply"]


def test_indian_digit_grouping():
    assert spoken_amount(425000) == "4,250 rupees"
    assert spoken_amount(12345678) == "1,23,456 rupees 78 paise"
    assert spoken_amount(100000000) == "10,00,000 rupees"
    assert spoken_amount(50) == "0 rupees 50 paise"


# ---------- mini statement ----------

async def test_mini_statement_lists_all_channels_newest_first(env):
    _, client = env
    h = await session(client)
    t = await say(client, h, "Can you give me my mini statement?")
    assert t["intent"] == "MINI_STATEMENT" and [x["tool"] for x in t["tool_trace"]] == ["get_mini_statement"]
    r = t["reply"]
    assert t["state"] == "LISTENING"  # a listing is not an ambiguous search
    for label in ("Rohan Stores", "Metro Mart", "ATM Withdrawal", "Salary Credit"):
        assert label in r
    assert r.index("Rohan Stores") < r.index("Metro Mart") < r.index("ATM Withdrawal") < r.index("Salary Credit")
    assert "a credit of 62,000 rupees" in r and "a debit of 5,000 rupees" in r


async def test_listed_transactions_can_still_be_looked_up_but_invented_ids_cannot(env):
    app, client = env
    llm = ScriptedLLM([LLMReply(tool_calls=[ToolCall("get_mini_statement", {"count": 5})]), LLMReply("Here you go."),
                       LLMReply(tool_calls=[ToolCall("get_transaction_status", {"transaction_id": "TXN-UPI-1001"})]),
                       LLMReply("Done."),
                       LLMReply(tool_calls=[ToolCall("get_transaction_status", {"transaction_id": "TXN-UPI-9999"})]),
                       LLMReply("Done.")])
    app, client, cm = await boot(llm)
    try:
        h = await session(client)
        await say(client, h, "show my last transactions")
        ok = await say(client, h, "check the first")
        bad = await say(client, h, "check another")
        assert ok["tool_trace"][0]["ok"] is True
        assert bad["tool_trace"][0] == {"tool": "get_transaction_status", "ok": False, "error_code": "LLM_INVALID_TOOL_CALL"}
    finally:
        await client.aclose()
        await cm.__aexit__(None, None, None)


# ---------- card block ----------

async def test_card_block_two_cards_choose_confirm(env):
    app, client = env
    h = await session(client)
    t1 = await say(client, h, "My card was stolen, please block it")
    assert t1["intent"] == "CARD_BLOCK" and "Which card" in t1["reply"]
    assert "debit card ending 4821" in t1["reply"] and "credit card ending 7316" in t1["reply"]
    t2 = await say(client, h, "the debit one")
    assert t2["state"] == "READY_TO_CONFIRM" and t2["pending_action"] == "CARD_BLOCK" and t2["preparation_id"]
    assert await card_status(app, "CRD-1001-D") == ("ACTIVE", None)  # preparing blocks nothing
    t3 = await say(client, h, "Yes, block it")
    assert t3["blocked_card_id"] == "CRD-1001-D" and "now blocked" in t3["reply"] and t3["pending_action"] is None
    assert await card_status(app, "CRD-1001-D") == ("BLOCKED", "STOLEN")
    assert await card_status(app, "CRD-1001-C") == ("ACTIVE", None)  # the other card is untouched
    audit = await app.state.ctx.db.audit_events.find_one({"action": "CARD_BLOCKED", "decision": "ALLOWED"})
    assert audit["resource_id"] == "CRD-1001-D" and audit["reason_code"] == "CUSTOMER_CONFIRMED_STOLEN"
    again = await say(client, h, "Yes please")  # a stray repeat cannot block the credit card
    assert await card_status(app, "CRD-1001-C") == ("ACTIVE", None)
    assert again["pending_action"] is None


async def test_single_card_goes_straight_to_confirmation_and_no_blocks_nothing(env):
    app, client = env
    h = await session(client, "CUS-1002")
    t1 = await say(client, h, "I lost my card")
    assert t1["state"] == "READY_TO_CONFIRM" and "debit card ending 5532" in t1["reply"]
    t2 = await say(client, h, "No, wait. Don't do it")
    assert t2["pending_action"] is None and "will not block" in t2["reply"]
    assert await card_status(app, "CRD-1002-D") == ("ACTIVE", None)
    t3 = await say(client, h, "yes")  # nothing is pending any more
    assert await card_status(app, "CRD-1002-D") == ("ACTIVE", None) and t3["blocked_card_id"] is None


async def test_card_type_in_the_first_sentence_skips_the_question(env):
    _, client = env
    t = await say(client, await session(client), "Someone is misusing my credit card, block it now")
    assert t["state"] == "READY_TO_CONFIRM" and "credit card ending 7316" in t["reply"]


async def test_fraud_reason_is_recorded(env):
    app, client = env
    h = await session(client, "CUS-1002")
    await say(client, h, "There are unauthorized charges, block my card")
    await say(client, h, "yes")
    assert await card_status(app, "CRD-1002-D") == ("BLOCKED", "SUSPECTED_FRAUD")


async def test_already_blocked_card_is_not_prepared(env):
    app, client = env
    h = await session(client, "CUS-1004")
    t = await say(client, h, "Block my card")
    assert "already blocked" in t["reply"] and t["state"] != "READY_TO_CONFIRM" and t["pending_action"] is None
    status = await say(client, h, "Is my card blocked?")
    assert "debit card ending 7754 is blocked" in status["reply"]


async def test_unblock_and_replacement_go_to_a_human_and_change_nothing(env):
    app, client = env
    h = await session(client, "CUS-1004")
    t = await say(client, h, "Please unblock my card")
    assert t["intent"] == "CARD_UNBLOCK" and t["state"] == "HANDOFF" and t["handoff_id"].startswith("HO-")
    assert [x["tool"] for x in t["tool_trace"]] == ["request_human_handoff"]
    assert await card_status(app, "CRD-1004-D") == ("BLOCKED", "LOST")
    h2 = await session(client, "CUS-1002")
    t2 = await say(client, h2, "I need a replacement card")
    assert t2["intent"] == "CARD_UNBLOCK" and t2["state"] == "HANDOFF"
    assert await card_status(app, "CRD-1002-D") == ("ACTIVE", None)


async def test_card_status_enquiry_does_not_start_a_block(env):
    app, client = env
    t = await say(client, await session(client), "Is my credit card active?")
    assert t["intent"] == "CARD_STATUS" and "credit card ending 7316 is active" in t["reply"]
    assert t["pending_action"] is None and await card_status(app, "CRD-1001-C") == ("ACTIVE", None)


async def test_one_pending_action_at_a_time(env):
    """Preparing a card block must drop a pending dispute so a bare 'yes' can never mean the wrong thing."""
    app, client = env
    h = await session(client)
    t1 = await say(client, h, "I sent 4250 rupees yesterday, deducted but the recipient has not received it")
    assert t1["pending_action"] == "DISPUTE"
    t2 = await say(client, h, "Also my debit card was stolen")
    assert t2["pending_action"] == "CARD_BLOCK"
    t3 = await say(client, h, "yes")
    assert t3["blocked_card_id"] == "CRD-1001-D" and t3["dispute_id"] is None
    assert await app.state.ctx.db.disputes.count_documents({"customer_id": "CUS-1001"}) == 0  # the 'yes' was for the card


# ---------- the model cannot bypass the rules ----------

async def test_model_cannot_confirm_a_block_the_customer_did_not_confirm():
    replies = [LLMReply(tool_calls=[ToolCall("list_cards", {})]),
               LLMReply(tool_calls=[ToolCall("prepare_card_block", {"card_id": "CRD-1002-D", "reason": "LOST"})]),
               LLMReply("ok"),
               # the 'model' now tries to confirm on the customer's behalf; the customer only asked a question
               LLMReply(tool_calls=[ToolCall("confirm_card_block", {"preparation_id": "CPREP-ANY",
                                                                    "customer_confirmation": True})]),
               LLMReply("Your card is blocked.")]
    llm = ScriptedLLM(replies)
    app, client, cm = await boot(llm)
    try:
        h = await session(client, "CUS-1002")
        await say(client, h, "I lost my card")
        t = await say(client, h, "how long will that take?")
        assert t["tool_trace"][0]["error_code"] == "CONFIRMATION_REQUIRED"
        assert await card_status(app, "CRD-1002-D") == ("ACTIVE", None)
    finally:
        await client.aclose()
        await cm.__aexit__(None, None, None)


async def test_model_cannot_block_cards_it_invented_or_that_belong_to_someone_else():
    replies = [LLMReply(tool_calls=[ToolCall("prepare_card_block", {"card_id": "CRD-1002-D", "reason": "LOST"})]),
               LLMReply("ok")]
    llm = ScriptedLLM(replies)
    app, client, cm = await boot(llm)
    try:
        h = await session(client, "CUS-1001")
        t = await say(client, h, "block a card")  # never listed this conversation
        assert t["tool_trace"][0]["error_code"] == "LLM_INVALID_TOOL_CALL"
        assert await card_status(app, "CRD-1002-D") == ("ACTIVE", None)
    finally:
        await client.aclose()
        await cm.__aexit__(None, None, None)


@pytest.mark.parametrize("bad_reply", [
    "Your debit card ending 9999 is active.",                                  # last-four not from any tool
    "Your account ending in 0000 has 5,00,000 rupees.",                         # invented account and amount
])
async def test_ungrounded_card_and_account_claims_are_replaced(bad_reply):
    (turn,), _ = await run([LLMReply(bad_reply)], "what is going on")
    assert turn["reply"] == SAFE


def test_guard_accepts_tail_present_in_tool_output():
    assert grounded("Your card ending 4821 is active.", '{"last4": "4821"}', "")
    assert not grounded("Your card ending 4822 is active.", '{"last4": "4821"}', "")


# ---------- loans ----------

async def test_loan_enquiry(env):
    _, client = env
    t = await say(client, await session(client, "CUS-1003"), "When is my next EMI due?")
    assert t["intent"] == "LOAN_ENQUIRY" and [x["tool"] for x in t["tool_trace"]] == ["get_loan_summary"]
    assert "personal loan" in t["reply"] and "1,84,000 rupees outstanding" in t["reply"]
    assert "EMI of 9,200 rupees is due on 15 November" in t["reply"]  # clock is 12 Nov; seed is +3 days
    none = await say(client, await session(client, "CUS-1002"), "what is my loan outstanding?")
    assert "do not see any loans" in none["reply"]


# ---------- REST surface ----------

async def test_rest_accounts_cards_loans_and_limit(env):
    _, client = env
    h = await session(client)
    acc = (await client.get("/api/v1/accounts", headers=h)).json()
    assert acc[0]["account_number_masked"] == "XXXXXX4821" and acc[0]["balance_paise"] == 8523450
    assert acc[0]["available_paise"] == 8433450 and "account_id" in acc[0]
    cards = (await client.get("/api/v1/cards", headers=h)).json()
    assert {c["card_id"] for c in cards} == {"CRD-1001-D", "CRD-1001-C"}
    assert all(set(c) == {"card_id", "card_type", "network", "last4", "status", "blocked_reason"} for c in cards)
    loans = (await client.get("/api/v1/loans", headers=h)).json()
    assert loans[0]["loan_id"] == "LN-1001" and loans[0]["emi_paise"] == 1475000
    two = (await client.get("/api/v1/transactions", params={"limit": 2}, headers=h)).json()
    assert two["count"] == 2 and two["transactions"][0]["transaction_id"] == "TXN-UPI-1001"
    assert (await client.get("/api/v1/transactions", params={"limit": 99}, headers=h)).status_code == 422


async def test_rest_requires_authentication(env):
    _, client = env
    h = await session(client, customer=None)
    for path in ("/api/v1/accounts", "/api/v1/cards", "/api/v1/loans"):
        r = await client.get(path, headers=h)
        assert r.status_code == 401 and r.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


async def test_rest_card_block_prepare_confirm_and_ownership(env):
    app, client = env
    h = await session(client, "CUS-1002")
    other = await client.post("/api/v1/cards/block/prepare", json={"card_id": "CRD-1001-D", "reason": "LOST"}, headers=h)
    assert other.status_code == 404 and other.json()["error"]["code"] == "CARD_NOT_FOUND"
    prep = await client.post("/api/v1/cards/block/prepare", json={"card_id": "CRD-1002-D", "reason": "LOST"}, headers=h)
    assert prep.status_code == 200 and "unblocking it needs a human agent" in prep.json()["confirmation_text"]
    pid = prep.json()["preparation_id"]
    assert await card_status(app, "CRD-1002-D") == ("ACTIVE", None)

    no = await client.post("/api/v1/cards/block/confirm", json={"preparation_id": pid, "customer_confirmation": False}, headers=h)
    assert no.status_code == 409 and no.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert await card_status(app, "CRD-1002-D") == ("ACTIVE", None)

    # another customer's session cannot redeem this preparation
    h_other = await session(client, "CUS-1001")
    stolen = await client.post("/api/v1/cards/block/confirm", json={"preparation_id": pid, "customer_confirmation": True}, headers=h_other)
    assert stolen.status_code == 409 and await card_status(app, "CRD-1002-D") == ("ACTIVE", None)

    ok = await client.post("/api/v1/cards/block/confirm", json={"preparation_id": pid, "customer_confirmation": True}, headers=h)
    assert ok.status_code == 200 and ok.json()["blocked"] is True and ok.json()["card"]["status"] == "BLOCKED"
    assert await card_status(app, "CRD-1002-D") == ("BLOCKED", "LOST")
    replay = await client.post("/api/v1/cards/block/confirm", json={"preparation_id": pid, "customer_confirmation": True}, headers=h)
    assert replay.status_code == 409  # preparation is single-use
    blocked = await client.post("/api/v1/cards/block/prepare", json={"card_id": "CRD-1002-D", "reason": "LOST"}, headers=h)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "CARD_NOT_ELIGIBLE"


async def test_concurrent_confirmations_block_once(env):
    """Two confirmations racing on one card: exactly one transition, one audit event."""
    import asyncio

    app, client = env
    h = await session(client, "CUS-1002")
    pids = []
    for _ in range(2):
        r = await client.post("/api/v1/cards/block/prepare", json={"card_id": "CRD-1002-D", "reason": "LOST"}, headers=h)
        pids.append(r.json()["preparation_id"])
    res = await asyncio.gather(*[client.post("/api/v1/cards/block/confirm",
                                             json={"preparation_id": p, "customer_confirmation": True}, headers=h) for p in pids])
    assert sorted(r.json()["blocked"] for r in res) == [False, True]
    assert await app.state.ctx.db.audit_events.count_documents({"action": "CARD_BLOCKED", "decision": "ALLOWED"}) == 1


# ---------- privacy regression found by scripts/e2e_scenarios.py ----------

async def test_spoken_secret_digits_do_not_survive_as_parsed_entities(env):
    """'my OTP is 123456' must not be stored (or returned) as an amount of 12345600 paise."""
    import json

    app, client = env
    h = await session(client)
    t = await say(client, h, "My OTP is 123456, what is my balance")
    assert t["intent"] == "BALANCE_ENQUIRY" and t["entities"]["amount_paise"] is None
    stored = json.dumps([d async for d in app.state.ctx.db.conversation_turns.find({}, {"_id": 0})], default=str)
    assert "12345600" not in stored and "123456" not in stored
