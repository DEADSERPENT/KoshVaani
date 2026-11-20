"""Database-first planner with a language model behind it.

Every request the deterministic planner recognises (balance, statement, loans, card status/block, dispute
preparation and confirmation, unblock -> human) is planned AND worded by the planner from backend tool output, so the
model never gets to improvise on money, cards or state changes, and the turn costs no model latency. Only free-form
text the planner does not understand goes to the language model, whose reply still has to pass the response guard.
"""
from app.providers.base import LLMReply
from app.providers.ollama_llm import OllamaLLM
from app.providers.rules_llm import RulesLLM


class GuardedLLM:
    def __init__(self, model: OllamaLLM, planner: RulesLLM | None = None):
        self.model_llm = model
        self.planner = planner or RulesLLM()
        self.name = model.name
        self.model = model.model

    def _planner_owns_turn(self, messages: list[dict]) -> bool:
        last_user = next((m for m in reversed(messages) if m["role"] == "user"), None)
        if last_user is None:
            return False
        plan = self.planner._plan(last_user["content"], self.planner._state(messages))
        return bool(plan.tool_calls)

    def _unanswered_card_question(self, messages: list[dict]) -> bool:
        """The agent asked which card and the customer did not name one: ask again, never let the model pick."""
        st = self.planner._state(messages)
        return len(st["cards"]) > 1 and "which card" in st["last_assistant"].lower()

    async def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        if (messages[-1]["role"] == "user" and not self._planner_owns_turn(messages)
                and self._unanswered_card_question(messages)):
            return LLMReply("Sorry, I need to know which card. Please say credit or debit, or the last four digits.")
        if self._planner_owns_turn(messages):
            return await self.planner.chat(messages, tools)
        return await self.model_llm.chat(messages, tools)
