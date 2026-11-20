import httpx

from app.config import Settings
from app.core.errors import AppError
from app.providers.base import LLMReply, ToolCall


class OllamaLLM:
    name = "ollama"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.s = settings
        self.model = settings.ollama_model
        self.client = client or httpx.AsyncClient(timeout=settings.llm_timeout_seconds)

    async def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        payload = {
            "model": self.model, "messages": messages, "tools": tools, "stream": False,
            # Qwen "thinking" burns the whole token budget on hidden reasoning: empty replies and 5-20 s turns.
            "think": False, "keep_alive": self.s.ollama_keep_alive,
            "options": {"temperature": self.s.llm_temperature, "num_ctx": self.s.ollama_num_ctx,
                        "num_predict": self.s.llm_max_output_tokens},
        }
        try:
            r = await self.client.post(f"{self.s.ollama_base_url}/api/chat", json=payload)
            r.raise_for_status()
            msg = r.json().get("message", {})
        except httpx.TimeoutException as exc:
            raise AppError("PROVIDER_TIMEOUT", "The language model timed out.") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise AppError("PROVIDER_UNAVAILABLE", "The language model is unavailable.") from exc
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):  # some builds return JSON text
                import json
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"__invalid__": True}
            calls.append(ToolCall(fn.get("name", ""), args if isinstance(args, dict) else {"__invalid__": True}))
        return LLMReply(content=(msg.get("content") or "").strip(), tool_calls=calls)
