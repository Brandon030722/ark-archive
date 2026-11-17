"""Stateless Responses wire adapter; opaque reasoning stays with its tool calls.

No retries, remote conversation state, inferred game facts or local JSON repair.
"""
from __future__ import annotations

import copy

from .contracts import ProviderError, strict_json_loads


def request(chat, messages, *, reasoning_effort='medium'):
    items = []
    for message in messages:
        role = message["role"]
        native = message.get("response_items")
        if native is not None:
            # Native items are server-owned checkpoint data, never user input.
            # Bind them to the same parsed calls before replaying opaque state.
            if role != "assistant" or not isinstance(native, list):
                raise ProviderError("invalid_request")
            calls = [i for i in native if isinstance(i, dict) and i.get("type") == "function_call"]
            try:
                actual = [(c["call_id"], c["name"], strict_json_loads(c["arguments"])) for c in calls]
                expected = [(c["id"], c["function"]["name"], strict_json_loads(c["function"]["arguments"]))
                            for c in message.get("tool_calls", [])]
                if actual != expected or not actual or any(
                    not isinstance(i, dict) or i.get("type") not in {"reasoning", "function_call", "message"}
                    for i in native
                ):
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                raise ProviderError("invalid_request") from None
            items.extend(copy.deepcopy(native))
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                          "output": message["content"]})
        else:
            if message.get("content") is not None:
                items.append({"role": role, "content": message["content"]})
            for call in message.get("tool_calls", []):
                items.append({"type": "function_call", "call_id": call["id"],
                              "name": call["function"]["name"], "arguments": call["function"]["arguments"]})
    result = {"model": chat["model"], "input": items, "store": False, "stream": False,
              "max_output_tokens": chat["max_tokens"], "include": ["reasoning.encrypted_content"]}
    # Older non-reasoning models must not receive unsupported reasoning options.
    if chat["model"].startswith(("gpt-5", "gpt-6", "o1", "o3", "o4")):
        result["reasoning"] = {"effort": reasoning_effort}
    if chat.get("tools"):
        result["tools"] = [{"type": "function", **copy.deepcopy(t["function"]), "strict": False}
                           for t in chat["tools"]]
        choice = chat["tool_choice"]
        result["tool_choice"] = ({"type": "function", "name": choice["function"]["name"]}
                                 if isinstance(choice, dict) else choice)
    if "response_format" in chat:
        result["text"] = {"format": chat["response_format"]}
    return result


def normalize(body):
    usage = body.get("usage")
    if not isinstance(usage, dict):
        raise ProviderError("usage_missing")
    prompt, output, total = (usage.get(k) for k in ("input_tokens", "output_tokens", "total_tokens"))
    details = usage.get("input_tokens_details") or {}
    if not isinstance(details, dict):
        raise ProviderError("usage_invalid")
    hit = details.get("cached_tokens", 0)
    if (any(type(n) is not int or n < 0 for n in (prompt, output, total, hit))
            or prompt + output != total or hit > prompt):
        raise ProviderError("usage_invalid")
    calls, prose, continuation = [], [], []
    malformed, refused = False, False
    items = body.get("output")
    if not isinstance(items, list):
        malformed, items = True, []
    for item in items:
        if not isinstance(item, dict):
            malformed = True
            continue
        kind = item.get("type")
        if kind == "function_call":
            calls.append({"id": item.get("call_id"), "type": "function", "function": {
                "name": item.get("name"), "arguments": item.get("arguments")}})
            if item.get("status", "completed") != "completed":
                malformed = True
            continuation.append({k: copy.deepcopy(v) for k, v in item.items()
                                 if k in {"type", "id", "call_id", "name", "arguments", "status"}})
        elif kind == "reasoning":
            # Do not expose or store plaintext reasoning/summary. The opaque
            # token is sufficient for stateless continuation of this tool loop.
            encrypted = item.get("encrypted_content")
            if not isinstance(encrypted, str) or not encrypted:
                malformed = True
            continuation.append({"type": "reasoning", "id": item.get("id"),
                                 "summary": [], "encrypted_content": encrypted})
        elif kind == "message":
            blocks = item.get("content")
            if item.get("role") != "assistant" or not isinstance(blocks, list):
                malformed = True
                continue
            clean = []
            for block in blocks:
                if not isinstance(block, dict):
                    malformed = True
                elif block.get("type") == "output_text" and isinstance(block.get("text"), str):
                    prose.append(block["text"])
                    clean.append({"type": "output_text", "text": block["text"], "annotations": []})
                elif block.get("type") == "refusal":
                    refused = True
                else:
                    malformed = True
            if clean:
                replay = {"type": "message", "role": "assistant", "content": clean}
                for key in ("id", "status", "phase"):
                    if key in item:
                        replay[key] = item[key]
                continuation.append(replay)
        else:
            malformed = True
    status = body.get("status")
    finish = "tool_calls" if calls else "stop"
    if status == "incomplete":
        reason = (body.get("incomplete_details") or {}).get("reason")
        finish = "length" if reason == "max_output_tokens" else "content_filter" if reason == "content_filter" else "aborted"
    elif status != "completed":
        finish = "aborted"
    if refused:
        finish = "content_filter"
    # Normalize even malformed outputs so the shared parser retains valid usage
    # receipts. Partial calls never become executable tool calls.
    return {"id": body.get("id"), "model": body["model"],
            "usage": {"prompt_tokens": prompt, "completion_tokens": output, "total_tokens": total,
                      "prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": prompt - hit},
            "choices": [{"index": 0, "finish_reason": finish, "message": {
                "role": "invalid" if malformed else "assistant", "content": "\n".join(prose) or ("" if not calls else None),
                "tool_calls": calls if finish == "tool_calls" else []}}],
            "_response_items": continuation if finish == "tool_calls" and not malformed else []}


def public_wire(payload):
    """Public audit keeps size/hash, not opaque continuation material."""
    import hashlib
    def scrub(value):
        if isinstance(value, list):
            return [scrub(v) for v in value]
        if isinstance(value, dict):
            return {k: ({"bytes": len(v.encode()), "sha256": hashlib.sha256(v.encode()).hexdigest()}
                        if k == "encrypted_content" and isinstance(v, str) else scrub(v))
                    for k, v in value.items()}
        return value
    return scrub(payload)
