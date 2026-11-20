import json
import logging
import time
from datetime import datetime, timezone

from app.agents import prompts
from app.agents.response_guard import SAFE_FALLBACK, grounded
from app.agents.tool_registry import ToolRegistry
from app.agents.tool_schemas import ollama_tools
from app.banking.session_service import SessionContext, guard_redis
from app.context import AppContext
from app.core.errors import AppError
from app.core.security import redact
from app.infrastructure.mongodb import utcnow
from app.repositories.audit import AuditRepository
from app.voice import transcript_normalizer as tn

log = logging.getLogger("orchestrator")
MAX_CALLS_PER_ITERATION = 2
HISTORY_TURNS = 8


class ConversationOrchestrator:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.llm = ctx.llm
        self.r = ctx.redis

    def _today(self):
        return (self.ctx.clock or (lambda: datetime.now(timezone.utc)))().date()

    async def _load(self, sid: str) -> dict:
        raw = await guard_redis(self.r.get(f"kv:conv:{sid}"))
        return json.loads(raw) if raw else {"turn": 0, "history": []}

    async def _save(self, sid: str, state: dict) -> None:
        await guard_redis(self.r.set(f"kv:conv:{sid}", json.dumps(state), ex=self.ctx.settings.session_ttl_seconds))

    async def handle_turn(self, session: SessionContext, text: str) -> dict:
        t0 = time.perf_counter()
        sid = session.session_id
        state = await self._load(sid)
        intent = tn.classify_intent(text)
        # Parse from the redacted text: a spoken OTP/PIN must never survive as a "parsed amount" in stored turns.
        safe = redact(text)
        entities = {"amount_paise": tn.parse_amount_paise(safe),
                    "date": (d.isoformat() if (d := tn.parse_date(safe, self._today())) else None),
                    "dispute_reference": tn.extract_dispute_reference(safe)}
        trace: list[dict] = []
        registry = ToolRegistry(self.ctx, session, state, text)
        outputs: list[str] = []
        latency = {"llm": 0, "tools": 0}

        async def run_tool(name: str, args: dict) -> dict:
            s = time.perf_counter()
            res = await registry.call(name, args)
            latency["tools"] += round((time.perf_counter() - s) * 1000)
            trace.append({"tool": name, "ok": res["ok"], "error_code": None if res["ok"] else res["error"]["code"]})
            outputs.append(json.dumps(res))
            return res

        reply: str | None = None
        if tn.wants_human(text):  # deterministic: never left to the model
            res = await run_tool("request_human_handoff", {"reason": "CUSTOMER_REQUESTED",
                                                           "summary": "Customer asked for a human agent."})
            reply = (f"I have transferred you to a human agent. Your handoff reference is {res['data']['handoff_id']}."
                     if res["ok"] else SAFE_FALLBACK)
        elif not session.customer_id:
            reply = "Please verify your identity in the secure verification step before I look at any account details."
        else:
            reply = await self._agent_loop(text, state, run_tool, latency, outputs, sid)

        if reply is None or not grounded(reply, " ".join(outputs), text):
            reply = SAFE_FALLBACK
        if tn.is_negative(text) and not tn.is_affirmative(text):
            state.pop("pending_prep", None)
            state.pop("pending_card", None)
        pending_id = state.get("pending_prep") or state.get("pending_card")
        pending_action = ("DISPUTE" if state.get("pending_prep") else "CARD_BLOCK" if state.get("pending_card") else None)

        status = ("HANDOFF" if state.get("handoff_id") else "READY_TO_CONFIRM" if pending_id
                  else "NEEDS_CLARIFICATION" if len(state.get("known_txns", [])) > 1 else
                  "NEEDS_AUTH" if not session.customer_id else "LISTENING")
        idx = state["turn"]
        state["turn"] = idx + 1
        state["history"] = (state["history"] + [{"role": "user", "content": redact(text)},
                                                 {"role": "assistant", "content": reply}])[-HISTORY_TURNS:]
        await self._save(sid, state)
        total = round((time.perf_counter() - t0) * 1000)
        await self._persist_turns(sid, idx, text, reply, intent, entities, {**latency, "total": total}, trace)
        return {"turn_index": idx * 2, "reply": reply, "intent": intent, "state": status, "entities": entities, "tool_trace": trace,
                "preparation_id": pending_id, "pending_action": pending_action,
                "dispute_id": state.get("last_dispute"), "blocked_card_id": state.get("last_card_block"),
                "handoff_id": state.get("handoff_id"),
                "llm": {"provider": self.llm.name, "model": self.llm.model}, "latency_ms": total}

    async def _agent_loop(self, text, state, run_tool, latency, outputs, session_id) -> str | None:
        messages = [{"role": "system", "content": prompts.SYSTEM_PROMPT},
                    {"role": "system", "content": prompts.state_message(state, True, self._today().isoformat())},
                    *state["history"], {"role": "user", "content": redact(text)}]  # model never sees phone/account/OTP digits
        tools = ollama_tools()
        for _ in range(self.ctx.settings.max_tool_iterations):
            s = time.perf_counter()
            try:
                out = await self.llm.chat(messages, tools)
            except AppError as e:  # provider failure: safe message, no business action
                log.warning("llm_failed", extra={"outcome": e.code})
                await AuditRepository(self.ctx.db).record(
                    action="PROVIDER_ERROR", resource_type="PROVIDER", resource_id="llm", decision="FAILED",
                    reason_code=e.code, session_id=session_id)
                return "I'm having trouble right now. I can transfer you to a human agent if you would like."
            latency["llm"] += round((time.perf_counter() - s) * 1000)
            if not out.tool_calls:
                return out.content or None
            calls = out.tool_calls[:MAX_CALLS_PER_ITERATION]
            messages.append({"role": "assistant", "content": out.content or "",
                             "tool_calls": [{"function": {"name": c.name, "arguments": c.arguments}} for c in calls]})
            for c in calls:
                res = await run_tool(c.name, c.arguments)
                messages.append({"role": "tool", "name": c.name, "content": json.dumps(res)})
        return None  # iteration cap hit: guard turns this into the safe fallback

    async def _persist_turns(self, sid, idx, text, reply, intent, entities, latency, trace) -> None:
        now = utcnow()
        col = self.ctx.db.conversation_turns
        await col.insert_one({"session_id": sid, "turn_index": idx * 2, "speaker": "CUSTOMER",
                              "transcript_redacted": redact(text), "intent": intent, "entities": entities,
                              "created_at": now, "latency_ms": latency, "tools": trace})
        await col.insert_one({"session_id": sid, "turn_index": idx * 2 + 1, "speaker": "ASSISTANT",
                              "transcript_redacted": reply, "intent": None, "entities": {}, "created_at": now})
