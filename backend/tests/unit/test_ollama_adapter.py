import json

import httpx
import pytest

from app.config import Settings
from app.core.errors import AppError
from app.providers.ollama_llm import OllamaLLM


def llm_with(handler):
    return OllamaLLM(Settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_parses_tool_calls_and_sends_options():
    seen = {}

    def handler(req: httpx.Request):
        seen["body"] = req.read()
        return httpx.Response(200, json={"message": {"content": "", "tool_calls": [
            {"function": {"name": "search_customer_transactions", "arguments": {"amount": 4250}}},
            {"function": {"name": "get_customer_context", "arguments": "not json"}}]}})

    out = await llm_with(handler).chat([{"role": "user", "content": "hi"}], [])
    assert out.tool_calls[0].name == "search_customer_transactions" and out.tool_calls[0].arguments == {"amount": 4250}
    assert out.tool_calls[1].arguments == {"__invalid__": True}  # later rejected by the strict schema
    body = json.loads(seen["body"])
    assert body["options"]["num_ctx"] == 8192 and body["stream"] is False


async def test_timeout_and_unavailable_map_to_typed_errors():
    def slow(req):
        raise httpx.ReadTimeout("t")

    def down(req):
        return httpx.Response(500)

    with pytest.raises(AppError) as e:
        await llm_with(slow).chat([], [])
    assert e.value.code == "PROVIDER_TIMEOUT"
    with pytest.raises(AppError) as e:
        await llm_with(down).chat([], [])
    assert e.value.code == "PROVIDER_UNAVAILABLE"
