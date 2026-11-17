"""Strict model turns and safe provider errors, independent of game answers."""

from __future__ import annotations

import json
import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

USAGE_FIELDS = {
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
}
FINISH_REASONS = {
    "stop",
    "tool_calls",
    "length",
    "content_filter",
    "insufficient_system_resource",
    "aborted",
}
ERROR_CODES = {
    "provider_error",
    "live_disabled",
    "live_authorization_unverified",
    "configuration_missing",
    "invalid_configuration",
    "simulation_required",
    "simulation_transport_required",
    "invalid_transport",
    "invalid_request",
    "http_error",
    "timeout",
    "request_error",
    "response_too_large",
    "invalid_response",
    "invalid_arguments",
    "usage_missing",
    "usage_invalid",
    "model_mismatch",
}


class ProviderError(Exception):
    """Only a fixed safe code may cross the provider boundary."""

    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and code in ERROR_CODES else "provider_error"
        super().__init__(self.code)


def finite_json(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("non-string JSON object key")
        for child in value.values():
            finite_json(child)
    elif isinstance(value, list):
        for child in value:
            finite_json(child)
    elif value is not None and not isinstance(value, (str, bool, int, float, dict)):
        raise ValueError("non-JSON value")


def strict_json_loads(value: str | bytes) -> Any:
    """Reject duplicate keys at every depth and all non-finite numbers."""

    def unique_pairs(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    def invalid_constant(_):
        raise ValueError("non-finite JSON number")

    result = json.loads(value, object_pairs_hook=unique_pairs, parse_constant=invalid_constant)
    finite_json(result)
    return result


class RuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, frozen=True)


class ToolCall(RuntimeModel):
    id: str = Field(min_length=1, max_length=256)
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    arguments: dict[str, JsonValue]

    @field_validator("arguments")
    @classmethod
    def finite_arguments(cls, value):
        finite_json(value)
        return value

    def to_wire(self):
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False, allow_nan=False),
            },
        }


class ModelTurn(RuntimeModel):
    content: str | None
    tool_calls: list[ToolCall] = Field(max_length=128)
    usage: dict[str, int]
    finish_reason: str
    model: str = Field(min_length=1, max_length=200)
    simulation: bool = False
    response_items: list[dict] = Field(default_factory=list, max_length=256, repr=False)

    def assistant_message(self):
        result = {"role": "assistant", "content": None,
                  "tool_calls": [call.to_wire() for call in self.tool_calls]}
        if self.response_items:
            result["response_items"] = self.response_items
        return result

    @field_validator("usage")
    @classmethod
    def valid_usage(cls, value):
        if set(value) != USAGE_FIELDS or any(number < 0 for number in value.values()):
            raise ValueError("invalid token usage")
        if (
            value["prompt_cache_hit_tokens"] + value["prompt_cache_miss_tokens"]
            != value["prompt_tokens"]
        ):
            raise ValueError("invalid cache token usage")
        if value["prompt_tokens"] + value["completion_tokens"] != value["total_tokens"]:
            raise ValueError("invalid total token usage")
        return value

    @model_validator(mode="after")
    def coherent_turn(self):
        if self.finish_reason not in FINISH_REASONS:
            raise ValueError("unknown completion finish reason")
        if len({call.id for call in self.tool_calls}) != len(self.tool_calls):
            raise ValueError("duplicate tool call id")
        if self.finish_reason == "tool_calls" and not self.tool_calls:
            raise ValueError("tool finish without tool calls")
        if self.finish_reason == "stop" and self.tool_calls:
            raise ValueError("stop finish with tool calls")
        if self.finish_reason == "stop" and self.content is None:
            raise ValueError("stop finish without content")
        return self
