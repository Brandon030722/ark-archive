"""Native Responses, compatible Chat and Anthropic Messages; no SDK retries."""

from __future__ import annotations

import copy
import json
import re
from urllib.parse import urlsplit

from .contracts import ProviderError, strict_json_loads
from .provider_registry import PROVIDERS
from .providers import DeepSeekProvider

PROTOCOLS = {key: value["base_url"] for key, value in PROVIDERS.items() if value["base_url"]}


def validate_base_url(protocol, value):
    if protocol in PROTOCOLS:
        if value and value.rstrip("/") != PROTOCOLS[protocol]:
            raise ValueError("provider_endpoint_mismatch")
        return PROTOCOLS[protocol]
    if protocol != "compatible" or not isinstance(value, str):
        raise ValueError("provider_protocol_invalid")
    parsed = urlsplit(value)
    _ = parsed.port
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or len(value) > 500
        or any(c.isspace() for c in value)
        or parsed.path.endswith(("/chat/completions", "/messages"))
    ):
        raise ValueError("provider_endpoint_invalid")
    return value.rstrip("/")


class MultiProvider(DeepSeekProvider):
    def with_reasoning_effort(self, effort):
        if effort not in {'low', 'medium', 'high'}:
            raise ProviderError('invalid_request')
        # Each graph request owns its copy; concurrent tasks cannot alter the
        # reasoning policy of another request or the saved provider profile.
        scoped = copy.copy(self)
        scoped.reasoning_effort = effort
        return scoped

    def _preflight(self):
        super()._preflight()
        try:
            validate_base_url(self._settings.api_protocol, self._settings.api_base_url)
        except ValueError:
            raise ProviderError("invalid_configuration") from None

    def model_allowed(self):
        return self._settings.api_protocol in PROVIDERS

    def endpoint(self):
        base = validate_base_url(self._settings.api_protocol, self._settings.api_base_url)
        return base + {"anthropic": "/messages", "openai": "/responses"}.get(
            self._settings.api_protocol, "/chat/completions")

    def headers(self, key):
        if self._settings.api_protocol == "anthropic":
            return {
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }
        return super().headers(key)

    def _request(self, messages, tools, max_output_tokens, response_format=None):
        protocol = self._settings.api_protocol
        validation_messages = messages
        if protocol == "openai":
            validation_messages = [{k: v for k, v in m.items() if k != "response_items"}
                                   if isinstance(m, dict) else m for m in messages]
        payload = super()._request(validation_messages, tools, max_output_tokens, response_format)
        if protocol != "deepseek":
            payload.pop("thinking", None)
        if protocol == "openai":
            from .openai_responses import request
            return request(payload, messages, reasoning_effort=getattr(self, 'reasoning_effort', 'medium'))
        if protocol != "anthropic":
            return payload
        if response_format is not None:
            raise ProviderError("invalid_request")
        system, converted = [], []
        for message in messages:
            role, content = message["role"], message["content"]
            if role == "system":
                system.append(content)
                continue
            blocks = []
            if role == "tool":
                role = "user"
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": message["tool_call_id"],
                        "content": content,
                    }
                )
            else:
                if content:
                    blocks.append({"type": "text", "text": content})
                for call in message.get("tool_calls", []):
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call["id"],
                            "name": call["function"]["name"],
                            "input": strict_json_loads(call["function"]["arguments"]),
                        }
                    )
            if not blocks:
                continue
            if converted and converted[-1]["role"] == role:
                converted[-1]["content"].extend(blocks)
            else:
                converted.append({"role": role, "content": blocks})
        result = {
            "model": self.model,
            "max_tokens": max_output_tokens,
            "system": "\n\n".join(system),
            "messages": converted,
            "stream": False,
        }
        if tools:
            result["tools"] = [
                {
                    "name": t["function"]["name"],
                    "description": t["function"].get("description", ""),
                    "input_schema": t["function"]["parameters"],
                }
                for t in tools
            ]
            result["tool_choice"] = (
                {"type": "tool", "name": tools[0]["function"]["name"]}
                if len(tools) == 1
                else {"type": "any"}
            )
        return result

    def normalize_response(self, body):
        if not isinstance(body, dict):
            raise ProviderError("invalid_response")
        body = copy.deepcopy(body)
        actual = body.get("model")
        # Official aliases may resolve to a dated snapshot, never arbitrary models.
        if actual != self.model and not (
            self._settings.api_protocol in PROTOCOLS
            and isinstance(actual, str)
            and re.fullmatch(re.escape(self.model) + r"-\d{4}-?\d{2}-?\d{2}", actual)
        ):
            raise ProviderError("model_mismatch")
        body["model"] = self.model
        if self._settings.api_protocol == "openai":
            from .openai_responses import normalize
            return normalize(body)
        usage = body.get("usage")
        if not isinstance(usage, dict):
            raise ProviderError("usage_missing")
        if self._settings.api_protocol == "anthropic":
            keys = (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
            values = [usage.get(k, 0) if k.startswith("cache_") else usage.get(k) for k in keys]
            if any(type(v) is not int or v < 0 for v in values):
                raise ProviderError("usage_invalid")
            fresh, output, creation, hit = values
            prompt = fresh + creation + hit
            calls, prose = [], []
            content = body.get("content")
            malformed = not isinstance(content, list) or any(
                not isinstance(b, dict) for b in content
            )
            if malformed:
                content = []
            for block in content:
                if block.get("type") == "tool_use":
                    calls.append(
                        {
                            "id": block.get("id"),
                            "type": "function",
                            "function": {
                                "name": block.get("name"),
                                "arguments": json.dumps(block.get("input"), ensure_ascii=False),
                            },
                        }
                    )
                elif block.get("type") == "text":
                    if not isinstance(block.get("text"), str):
                        malformed = True
                    else:
                        prose.append(block["text"])
            finish = {
                "tool_use": "tool_calls",
                "end_turn": "stop",
                "stop_sequence": "stop",
                "max_tokens": "length",
                "refusal": "content_filter",
            }.get(body.get("stop_reason"), "aborted")
            body = {
                "id": body.get("id"),
                "model": self.model,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": finish,
                        "message": {
                            "role": "invalid" if malformed else "assistant",
                            "content": "\n".join(prose) or None,
                            "tool_calls": calls,
                        },
                    }
                ],
            }
            body["usage"] = {
                "prompt_tokens": prompt,
                "completion_tokens": output,
                "total_tokens": prompt + output,
                "prompt_cache_hit_tokens": hit,
                "prompt_cache_miss_tokens": prompt - hit,
            }
        else:
            prompt = usage.get("prompt_tokens")
            details = usage.get("prompt_tokens_details") or {}
            if not isinstance(details, dict):
                raise ProviderError("usage_invalid")
            hit = usage.get("prompt_cache_hit_tokens", details.get("cached_tokens", 0))
            if type(prompt) is not int or type(hit) is not int or not 0 <= hit <= prompt:
                raise ProviderError("usage_invalid")
            usage["prompt_cache_hit_tokens"] = hit
            usage["prompt_cache_miss_tokens"] = prompt - hit
        return body
