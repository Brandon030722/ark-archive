"""Existing four-turn evidence/revision regression over each native wire protocol."""

import json

import httpx
import pytest
import test_stage52_semantic_review as flow
from four_turn_fixture import check_graph_checkpoints, exercise_dialogue
from responses_fixture import chat_view, native_reply
from test_recruitment import synthetic_sources as synthetic_sources
from test_runtime_tools import tool_context as tool_context
from test_stage15_roguelike_qa import make_knowledge as make_knowledge
from test_stage65_analysis_runtime import knowledge as knowledge

from arknights_agent.runtime.multi_provider import MultiProvider

__all__ = ["knowledge", "make_knowledge", "tool_context", "synthetic_sources"]


def openai_view(native):
    messages = [{"role": "system", "content": native["system"]}]
    for m in native["messages"]:
        for b in m["content"]:
            if b["type"] == "tool_result":
                messages.append(
                    {"role": "tool", "tool_call_id": b["tool_use_id"], "content": b["content"]}
                )
            elif b["type"] == "text":
                messages.append({"role": m["role"], "content": b["text"]})
            elif b["type"] == "tool_use":
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": b["id"],
                                "type": "function",
                                "function": {
                                    "name": b["name"],
                                    "arguments": json.dumps(b["input"]),
                                },
                            }
                        ],
                    }
                )
    return {
        "model": native["model"],
        "messages": messages,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t["input_schema"],
                },
            }
            for t in native.get("tools", [])
        ],
    }


@pytest.mark.parametrize("protocol", ["deepseek", "openai", "compatible", "anthropic"])
def test_complete_four_turn_native_flow(knowledge, tmp_path, monkeypatch, protocol):
    class NativeHarness(MultiProvider):
        def __init__(self, settings, transport, simulation=False):
            def handle(request):
                if protocol == "openai":
                    assert request.url.path == "/v1/responses"
                    body = json.loads(request.content)
                    assert body["store"] is False
                    original = transport.handler(httpx.Request("POST", "https://fixture.invalid", json=chat_view(body)))
                    return httpx.Response(200, json=native_reply(original.json()))
                if protocol != "anthropic":
                    return transport.handler(request)
                body = json.loads(request.content)
                original = transport.handler(
                    httpx.Request("POST", "https://fixture.invalid", json=openai_view(body))
                )
                data = original.json()
                msg = data["choices"][0]["message"]
                u = data["usage"]
                blocks = [
                    {
                        "type": "tool_use",
                        "id": c["id"],
                        "name": c["function"]["name"],
                        "input": json.loads(c["function"]["arguments"]),
                    }
                    for c in msg.get("tool_calls", [])
                ]
                if msg.get("content"):
                    blocks.insert(0, {"type": "text", "text": msg["content"]})
                return httpx.Response(
                    200,
                    json={
                        "id": data.get("id", "msg_test"),
                        "model": data["model"],
                        "content": blocks,
                        "stop_reason": "tool_use" if blocks else "end_turn",
                        "usage": {
                            "input_tokens": u["prompt_tokens"],
                            "output_tokens": u["completion_tokens"],
                        },
                    },
                )

            base = {
                "deepseek": "https://api.deepseek.com",
                "openai": "https://api.openai.com/v1",
                "anthropic": "https://api.anthropic.com/v1",
                "compatible": "https://fixture.invalid/v1",
            }[protocol]
            super().__init__(
                settings.model_copy(update={"api_protocol": protocol, "api_base_url": base}),
                httpx.MockTransport(handle),
                simulation=simulation,
            )

    monkeypatch.setattr(flow, "DeepSeekProvider", NativeHarness)
    def graph_invoke(*args, **kwargs):
        return flow.invoke(*args, **{**kwargs, "agent_execution_engine": "langgraph"})

    exercise_dialogue(knowledge, tmp_path, monkeypatch, graph_invoke)
    check_graph_checkpoints(tmp_path)
