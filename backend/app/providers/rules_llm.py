"""Deterministic stand-in for an LLM: same chat/tool-call interface, no model required.

Used for tests and as a local fallback. It is not a language model and is labelled as such in traces.
"""
import json
import re
from datetime import date

from app.providers.base import LLMReply, ToolCall
from app.voice import transcript_normalizer as tn

_P2P, _MERCHANT, _FAILED = ("UPI_P2P_BENEFICIARY_NOT_CREDITED", "UPI_MERCHANT_PAYMENT_PENDING",
                            "UPI_DEBIT_WITH_FAILED_STATUS")


def _words(status: str) -> str:
    return {"DEBITED_CREDIT_UNCONFIRMED": "debited with credit not confirmed"}.get(status, status.lower().replace("_", " "))


class RulesLLM:
    name = "rules"
    model = "rules-v1"

    async def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        state = self._state(messages)
        last = messages[-1]
        if last["role"] == "tool":
            return self._compose(messages, json.loads(last["content"]), last.get("name", ""))
        return self._plan(last["content"], state)

    @staticmethod
    def _state(messages) -> dict:
        history = [m for m in messages if m["role"] in ("user", "assistant")]
        base = {"pending": None, "pending_card": None, "known": [], "cards": [], "today": date.today(),
                "user_history": " ".join(m["content"] for m in history if m["role"] == "user"),
                "last_assistant": next((m["content"] for m in reversed(history) if m["role"] == "assistant"), "")}
        for m in messages:
            if m["role"] == "system" and m["content"].startswith("STATE:"):
                c = m["content"]

                def ref(key: str) -> str | None:
                    hit = re.search(rf"{key}=(\S+?);", c)
                    return None if not hit or hit.group(1) == "None" else hit.group(1)

                def items(key: str) -> list:
                    hit = re.search(rf"{key}=(\[[^\]]*\])", c)
                    return json.loads(hit.group(1).replace("'", '"')) if hit else []

                today = re.search(r"today=(\d{4}-\d{2}-\d{2})", c)
                return {**base, "pending": ref("pending_preparation"), "pending_card": ref("pending_card_block"),
                        "known": items("known_transactions"), "cards": items("known_cards"),
                        "today": date.fromisoformat(today.group(1)) if today else date.today()}
        return base

    def _plan(self, text: str, st: dict) -> LLMReply:
        if st["pending_card"] and tn.is_affirmative(text):
            return LLMReply(tool_calls=[ToolCall("confirm_card_block", {"preparation_id": st["pending_card"],
                                                                         "customer_confirmation": True})])
        if st["pending_card"] and tn.is_negative(text):
            return LLMReply("Okay, I will not block your card. Is there anything else I can help with?")
        if st["pending"] and tn.is_affirmative(text):
            return LLMReply(tool_calls=[ToolCall("confirm_dispute", {"preparation_id": st["pending"],
                                                                      "customer_confirmation": True})])
        if st["pending"] and tn.is_negative(text):
            return LLMReply("Okay, I will not register a dispute. Is there anything else I can help with?")
        ref = tn.extract_dispute_reference(text)
        if ref:
            return LLMReply(tool_calls=[ToolCall("get_dispute_status", {"dispute_id": ref})])
        if len(st["cards"]) > 1 and "which card" in st["last_assistant"].lower():
            chosen = tn.pick_card(text, st["cards"])
            if chosen:
                return LLMReply(tool_calls=[ToolCall("prepare_card_block", {
                    "card_id": chosen, "reason": tn.classify_block_reason(st["user_history"])})])
        intent = tn.classify_intent(text)
        if intent == "RESTRICTED_REQUEST":
            return LLMReply(tool_calls=[ToolCall("request_human_handoff", {
                "reason": "RESTRICTED_REQUEST", "summary": "Customer asked for an action reserved for a human agent."})])
        simple = {"BALANCE_ENQUIRY": "get_account_balance", "MINI_STATEMENT": "get_mini_statement",
                  "LOAN_ENQUIRY": "get_loan_summary", "CARD_BLOCK": "list_cards", "CARD_STATUS": "list_cards"}
        if intent in simple:
            return LLMReply(tool_calls=[ToolCall(simple[intent], {})])
        if intent == "CARD_UNBLOCK":  # unblock / replacement is a human-only action by policy
            return LLMReply(tool_calls=[ToolCall("request_human_handoff", {
                "reason": "CARD_UNBLOCK_REQUEST", "summary": "Customer asked to unblock or replace a card."})])
        if len(st["known"]) > 1 and not st["pending"]:
            pick = tn.parse_choice(text, len(st["known"]))
            if pick is not None:
                return LLMReply(tool_calls=[ToolCall("prepare_dispute", {
                    "transaction_id": st["known"][pick], "issue_code": _P2P,
                    "customer_explanation": "Customer reports the payment did not reach the recipient."})])
        if intent in ("UPI_PAYMENT_ISSUE", "TRANSACTION_STATUS"):
            amount = tn.parse_amount_paise(text)
            when = tn.parse_date(text, st["today"])
            if amount is None and when is None:
                return LLMReply("Could you tell me the amount or the date of the payment?")
            args: dict = {"transaction_type": "UPI"}
            if amount is not None:
                args["amount"] = amount / 100
            if when is not None:
                args.update(date_from=when.isoformat(), date_to=when.isoformat())
            return LLMReply(tool_calls=[ToolCall("search_customer_transactions", args)])
        return LLMReply("I can help with your balance, recent transactions, UPI payment problems, blocking a card, or loan EMIs. What would you like to do?")

    def _compose(self, messages: list[dict], result: dict, tool: str) -> LLMReply:
        if not result["ok"]:
            err = result["error"]
            if err["code"] == "DISPUTE_NOT_ELIGIBLE":
                existing = err["details"].get("existing_dispute_id")
                if existing:
                    return LLMReply(f"A complaint already exists for this transaction. The reference is {existing}.")
                return LLMReply("This transaction is not eligible for a dispute under the current policy. "
                                "Would you like me to transfer you to a human agent?")
            if err["code"] == "CONFIRMATION_REQUIRED":
                return LLMReply("I need a clear yes before I register it. Would you like me to register the dispute?")
            return LLMReply(err["message"])
        data = result["data"]
        user_text = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        if tool == "search_customer_transactions":
            txns = data["transactions"]
            if not txns:
                return LLMReply("I could not find a matching UPI transaction. Could you check the amount or date?")
            if data["ambiguous"]:
                parts = [f"{i}: {t['amount_text']} to {t['payee_label']} on {t['date_text']}"
                         for i, t in enumerate(txns, 1)]
                return LLMReply("I found more than one match. " + "; ".join(parts) + ". Which one do you mean?")
            t = txns[0]
            if tn.classify_intent(user_text) == "UPI_PAYMENT_ISSUE":
                code = {"PENDING": _MERCHANT, "FAILED": _FAILED}.get(t["status"], _P2P)
                return LLMReply(tool_calls=[ToolCall("prepare_dispute", {
                    "transaction_id": t["transaction_id"], "issue_code": code,
                    "customer_explanation": "Customer reports a problem with this UPI payment."})])
            return LLMReply(self._status_sentence(t))
        if tool == "get_account_balance":
            return LLMReply(self._balance_sentence(data))
        if tool == "get_mini_statement":
            return LLMReply(self._statement_sentence(data))
        if tool == "get_loan_summary":
            return LLMReply(self._loan_sentence(data))
        if tool == "list_cards":
            return self._compose_cards(messages, data, user_text)
        if tool in ("prepare_card_block", "prepare_dispute"):
            return LLMReply(data["confirmation_text"])
        if tool == "confirm_card_block":
            return LLMReply(data["message"])
        if tool == "confirm_dispute":
            if data["created"]:
                return LLMReply(f"Your complaint is registered. The reference is {data['dispute_id']}. "
                                "This records the complaint only; it is not a refund.")
            return LLMReply(f"A complaint already exists for this transaction. The reference is {data['dispute_id']}.")
        if tool == "get_dispute_status":
            return LLMReply(f"Complaint {data['dispute_id']} is {data['status'].lower().replace('_', ' ')}. "
                            f"{data['timeline_message']}")
        if tool == "get_transaction_status":
            return LLMReply(self._status_sentence(data))
        if tool == "request_human_handoff":
            return LLMReply(f"I have transferred you to a human agent. Your handoff reference is {data['handoff_id']}.")
        return LLMReply("Done.")

    @staticmethod
    def _balance_sentence(data: dict) -> str:
        if not data["accounts"]:
            return "I could not find an account on your profile."
        parts = []
        for a in data["accounts"]:
            s = f"Your {a['account_type'].lower()} account ending {a['last4']} has a balance of {a['balance_text']}."
            if a["has_hold"]:
                s += f" {a['available_text']} is available right now."
            parts.append(s)
        return " ".join(parts)

    @staticmethod
    def _statement_sentence(data: dict) -> str:
        if not data["transactions"]:
            return "I could not find any recent transactions on your account."
        items = [f"a {t['direction'].lower()} of {t['amount_text']} for {t['payee_label']} on {t['date_text']}"
                 for t in data["transactions"]]
        return "Your latest transactions are: " + "; ".join(items) + ". Tell me if any of them looks wrong."

    @staticmethod
    def _loan_sentence(data: dict) -> str:
        if not data["loans"]:
            return "I do not see any loans on your profile."
        return " ".join(
            f"Your {l['loan_type'].lower()} loan has {l['outstanding_text']} outstanding. "
            f"The next EMI of {l['emi_text']} is due on {l['due_text']}." for l in data["loans"][:2])

    @staticmethod
    def _card_phrase(c: dict) -> str:
        return f"{c['card_type'].lower()} card ending {c['last4']}"

    def _compose_cards(self, messages: list[dict], data: dict, user_text: str) -> LLMReply:
        cards = data["cards"]
        if not cards:
            return LLMReply("I do not see any cards on your profile.")
        if tn.classify_intent(user_text) == "CARD_STATUS":
            return LLMReply(" ".join(f"Your {self._card_phrase(c)} is {c['status'].lower()}." for c in cards))
        active = [c for c in cards if c["status"] == "ACTIVE"]
        if not active:
            return LLMReply("All your cards are already blocked. I can transfer you to a human agent for a replacement.")
        history = " ".join(m["content"] for m in messages if m["role"] == "user")
        chosen = active[0]["card_id"] if len(active) == 1 else tn.pick_card(user_text, [c["card_id"] for c in active])
        if chosen:
            return LLMReply(tool_calls=[ToolCall("prepare_card_block", {
                "card_id": chosen, "reason": tn.classify_block_reason(history)})])
        names = " or ".join(f"your {self._card_phrase(c)}" for c in active)
        return LLMReply(f"Which card should I block: {names}?")

    @staticmethod
    def _status_sentence(t: dict) -> str:
        s = (f"I found a transaction marked as {_words(t['status'])} for {t['amount_text']} "
             f"on {t['date_text']} to {t['payee_label']}.")
        if t["recipient_status"] == "CREDITED":
            s += " The recipient is recorded as credited."
        return s
