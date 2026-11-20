import pytest

from app.agents.response_guard import grounded
from app.providers.base import LLMReply
from app.providers.guarded_llm import GuardedLLM

STATE = ("STATE: authenticated=True; today=2026-11-20; pending_preparation=None; pending_card_block=None; "
         "known_transactions=[]; known_cards=[]")


class FakeModel:
    name, model = "ollama", "fake"

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools):
        self.calls += 1
        return LLMReply("model reply")


def msgs(text):
    return [{"role": "system", "content": STATE}, {"role": "user", "content": text}]


@pytest.mark.parametrize("text,tool", [
    ("What is my balance?", "get_account_balance"),
    ("show my mini statement", "get_mini_statement"),
    ("please unblock my card", "request_human_handoff"),
])
async def test_recognised_requests_are_planned_from_database_tools_not_the_model(text, tool):
    model = FakeModel()
    out = await GuardedLLM(model).chat(msgs(text), [])
    assert out.tool_calls[0].name == tool
    assert model.calls == 0


async def test_free_form_text_goes_to_the_model():
    model = FakeModel()
    out = await GuardedLLM(model).chat(msgs("hmm, can you help me with something odd?"), [])
    assert out.content == "model reply" and model.calls == 1


@pytest.mark.parametrize("reply", [
    "Your card is blocked. Would you like me to unblock it or replace it?",
    "I will refund the money shortly.",
    "I can waive the fee for you.",
])
def test_guard_rejects_actions_reserved_for_humans(reply):
    assert not grounded(reply, "", "")


def test_guard_allows_handoff_wording():
    assert grounded("I can transfer you to a human agent for a replacement.", "", "")


async def test_unanswered_which_card_question_is_asked_again_not_guessed():
    model = FakeModel()
    state = STATE.replace("known_cards=[]", "known_cards=['CRD-1001-D', 'CRD-1001-C']")
    messages = [{"role": "system", "content": state},
                {"role": "user", "content": "my card was stolen"},
                {"role": "assistant", "content": "Which card should I block: credit or debit?"},
                {"role": "user", "content": "yes"}]
    out = await GuardedLLM(model).chat(messages, [])
    assert "which card" in out.content.lower() and model.calls == 0


@pytest.mark.parametrize("text", [
    "transfer five thousand rupees to my friend", "please increase my credit limit",
    "I want to close my account", "can you waive my prepayment charges"])
async def test_restricted_requests_go_to_a_human_not_the_model(text):
    model = FakeModel()
    out = await GuardedLLM(model).chat(msgs(text), [])
    assert out.tool_calls[0].name == "request_human_handoff" and model.calls == 0


@pytest.mark.parametrize("text", [
    "I sent 4250 rupees yesterday but the recipient did not get it",
    "my transfer of 5000 rupees failed and money was deducted"])
async def test_payment_problems_are_not_mistaken_for_restricted_requests(text):
    out = await GuardedLLM(FakeModel()).chat(msgs(text), [])
    assert not out.tool_calls or out.tool_calls[0].name != "request_human_handoff"


@pytest.mark.parametrize("text,intent", [
    ("please increase my credit limit to five lakh", "RESTRICTED_REQUEST"),
    ("transfer five thousand rupees to my friend", "RESTRICTED_REQUEST"),
    ("can you waive my prepayment charges", "RESTRICTED_REQUEST"),
    ("my transfer of 5000 rupees failed and money was deducted", "UPI_PAYMENT_ISSUE"),
    ("what is my balance", "BALANCE_ENQUIRY"),
])
def test_restricted_requests_are_labelled_for_the_operator_console(text, intent):
    from app.voice import transcript_normalizer as tn
    assert tn.classify_intent(text) == intent
