"""Content-free failure metadata; never evidence for execution or billing.

Only fixed enum labels and bounded numeric shape information cross this
boundary. Raw response text, tool names, argument keys/values and exception
messages are deliberately absent. Existing billing receipts stay unchanged.
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from arknights_agent.runtime.contracts import FINISH_REASONS, ToolCall, finite_json
from arknights_agent.runtime.provider_argument_failure import (
    MAX_ARGUMENT_BYTES,
    _safe_argument_data,
)

PROTOCOL = "provider-failure-diagnostic-v1"
MAX_TOOL_CALLS = 128
MAX_ARGUMENT_SHAPES = 8
MAX_ARGUMENT_UTF8_BYTES = 4 * 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9_-]{1,256}", re.ASCII)
_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}", re.ASCII)
_FINISH = FINISH_REASONS | {"missing", "invalid_type", "unrecognized"}
_TYPES = {"string", "object", "array", "number", "boolean", "null", "missing", "other"}
_STAGES = {"transport", "response_json", "response_envelope", "usage", "privacy",
           "tool_call_envelope", "tool_arguments", "tool_call_contract", "model_turn_contract"}
_CATEGORIES = {
    "request_error", "timeout", "http_error", "response_too_large", "provider_error",
    "invalid_response_json", "invalid_response_envelope", "model_mismatch", "usage_missing",
    "usage_invalid", "sensitive_response", "invalid_call_structure", "invalid_function_shape",
    "arguments_not_string", "invalid_json_syntax", "duplicate_json_key", "nonfinite_json_number",
    "json_recursion_limit", "invalid_argument_encoding", "arguments_not_object",
    "invalid_call_contract", "invalid_turn_contract", "incoherent_finish_reason",
}
_OMITTED = {
    "retained", "not_argument_failure", "privacy_guard", "finish_reason_not_tool_calls",
    "not_single_tool_call", "invalid_call_structure", "arguments_not_string",
    "arguments_too_large", "invalid_argument_encoding", "call_metadata_disallowed",
    "not_argument_json_error", "projection_validation_failed",
}
_KEYS = {"protocol", "finish_reason", "tool_call_count", "tool_call_count_bounded",
         "argument_shapes", "argument_shapes_omitted_count", "parse_stage", "parse_category",
         "raw_evidence_omitted_reason"}
_SHAPE_KEYS = {"index", "argument_type", "arguments_utf8_bytes", "arguments_utf8_bytes_bounded"}


class _DuplicateJSONKey(ValueError):
    pass


class _NonFiniteJSONNumber(ValueError):
    pass


def _arguments(value):
    def unique_pairs(pairs):
        result = {}
        for key, child in pairs:
            if key in result:
                raise _DuplicateJSONKey
            result[key] = child
        return result

    def nonfinite(_value):
        raise _NonFiniteJSONNumber

    try:
        parsed = json.loads(value, object_pairs_hook=unique_pairs, parse_constant=nonfinite)
        try:
            finite_json(parsed)
        except ValueError:
            return None, "nonfinite_json_number"
        return parsed, None
    except _DuplicateJSONKey:
        return None, "duplicate_json_key"
    except _NonFiniteJSONNumber:
        return None, "nonfinite_json_number"
    except RecursionError:
        return None, "json_recursion_limit"
    except (ValueError, TypeError):
        return None, "invalid_json_syntax"


def _argument_shape(index, function):
    if not isinstance(function, dict) or "arguments" not in function:
        value, kind = None, "missing"
    else:
        value = function["arguments"]
        kind = ("string" if isinstance(value, str) else "null" if value is None else
                "boolean" if isinstance(value, bool) else "number" if isinstance(value, (int, float)) else
                "object" if isinstance(value, dict) else "array" if isinstance(value, list) else "other")
    size = None
    if isinstance(value, str):
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError:
            pass
    return {"index": index, "argument_type": kind,
            "arguments_utf8_bytes": min(size, MAX_ARGUMENT_UTF8_BYTES) if size is not None else None,
            "arguments_utf8_bytes_bounded": size is not None and size > MAX_ARGUMENT_UTF8_BYTES}


def _call_failure(calls):
    if not isinstance(calls, list) or len(calls) > MAX_TOOL_CALLS:
        return "tool_call_envelope", "invalid_call_structure"
    ids = set()
    for index, call in enumerate(calls):
        if (not isinstance(call, dict) or not {"id", "type", "function"} <= set(call)
                or not set(call) <= {"id", "type", "function", "index"}
                or call["type"] != "function"
                or "index" in call and (type(call["index"]) is not int or call["index"] != index)):
            return "tool_call_envelope", "invalid_call_structure"
        function = call["function"]
        if not isinstance(function, dict) or set(function) != {"name", "arguments"}:
            return "tool_call_envelope", "invalid_function_shape"
        if not isinstance(function["arguments"], str):
            return "tool_arguments", "arguments_not_string"
        parsed, error = _arguments(function["arguments"])
        if error:
            return "tool_arguments", error
        if not isinstance(parsed, dict):
            return "tool_arguments", "arguments_not_object"
        try:
            valid = ToolCall(id=call["id"], name=function["name"], arguments=parsed)
        except (ValidationError, ValueError, TypeError, RecursionError):
            return "tool_call_contract", "invalid_call_contract"
        if valid.id in ids:
            return "tool_call_envelope", "invalid_call_structure"
        ids.add(valid.id)
    return None


def _omitted_reason(error_code, finish, calls, *, privacy_blocked, evidence_retained, exact_secret):
    if privacy_blocked:
        return "privacy_guard"
    if evidence_retained:
        return "retained"
    if error_code != "invalid_arguments":
        return "not_argument_failure"
    if finish != "tool_calls":
        return "finish_reason_not_tool_calls"
    if not isinstance(calls, list) or len(calls) != 1:
        return "not_single_tool_call"
    call = calls[0]
    if (not isinstance(call, dict) or not {"id", "type", "function"} <= set(call)
            or not set(call) <= {"id", "type", "function", "index"}
            or call["type"] != "function"
            or "index" in call and (type(call["index"]) is not int or call["index"] != 0)):
        return "invalid_call_structure"
    function = call["function"]
    if not isinstance(function, dict) or set(function) != {"name", "arguments"}:
        return "invalid_call_structure"
    arguments = function["arguments"]
    if not isinstance(arguments, str):
        return "arguments_not_string"
    try:
        size = len(arguments.encode("utf-8"))
    except UnicodeError:
        return "invalid_argument_encoding"
    if size > MAX_ARGUMENT_BYTES:
        return "arguments_too_large"
    if not _safe_argument_data(arguments, exact_secret):
        return "privacy_guard"
    if (not isinstance(call["id"], str) or not _ID.fullmatch(call["id"])
            or not isinstance(function["name"], str) or not _NAME.fullmatch(function["name"])):
        return "call_metadata_disallowed"
    parsed, error = _arguments(arguments)
    if error is None and isinstance(parsed, dict):
        return "not_argument_json_error"
    return "projection_validation_failed"


def build_failure_diagnostic(body, error_code, *, privacy_blocked=False,
                             evidence_retained=False, exact_secret=None):
    """Classify protocol structure without persisting any upstream content."""
    choices = body.get("choices") if isinstance(body, dict) else None
    choice = choices[0] if isinstance(choices, list) and len(choices) == 1 else None
    message = choice.get("message") if isinstance(choice, dict) else None
    calls = message.get("tool_calls") if isinstance(message, dict) else None
    raw_finish = choice.get("finish_reason") if isinstance(choice, dict) else None
    finish = ("missing" if not isinstance(choice, dict) or "finish_reason" not in choice else
              "invalid_type" if not isinstance(raw_finish, str) else
              raw_finish if raw_finish in FINISH_REASONS else "unrecognized")
    count = len(calls) if isinstance(calls, list) else 0
    stage, category = "response_envelope", "invalid_response_envelope"
    if privacy_blocked:
        stage, category = "privacy", "sensitive_response"
    elif error_code in {"request_error", "timeout", "http_error", "response_too_large", "provider_error"}:
        stage, category = "transport", error_code
    elif error_code in {"usage_missing", "usage_invalid"}:
        stage, category = "usage", error_code
    elif error_code == "model_mismatch":
        category = "model_mismatch"
    elif not isinstance(body, dict):
        stage, category = "response_json", "invalid_response_json"
    elif (isinstance(choice, dict) and type(choice.get("index")) is int and choice["index"] == 0
          and isinstance(raw_finish, str) and raw_finish in FINISH_REASONS):
        if isinstance(message, dict) and message.get("role") == "assistant" and "content" in message:
            failure = _call_failure(calls if calls is not None else [])
            if failure:
                stage, category = failure
            elif raw_finish == "stop" and calls or raw_finish == "tool_calls" and not calls:
                stage, category = "model_turn_contract", "incoherent_finish_reason"
            else:
                stage, category = "model_turn_contract", "invalid_turn_contract"
    result = {
        "protocol": PROTOCOL, "finish_reason": finish,
        "tool_call_count": min(count, MAX_TOOL_CALLS), "tool_call_count_bounded": count > MAX_TOOL_CALLS,
        "argument_shapes": [_argument_shape(index, call.get("function") if isinstance(call, dict) else None)
                            for index, call in enumerate(calls[:MAX_ARGUMENT_SHAPES])]
                            if isinstance(calls, list) else [],
        "argument_shapes_omitted_count": min(max(0, count - MAX_ARGUMENT_SHAPES), MAX_TOOL_CALLS),
        "parse_stage": stage, "parse_category": category,
        "raw_evidence_omitted_reason": _omitted_reason(error_code, finish, calls,
            privacy_blocked=privacy_blocked, evidence_retained=evidence_retained, exact_secret=exact_secret),
    }
    return result if valid_failure_diagnostic(result) else None


def valid_failure_diagnostic(value):
    """Accept only the complete fixed schema and bounded enum/numeric values."""
    if not isinstance(value, dict) or set(value) != _KEYS:
        return False
    if any(not isinstance(value[key], str) for key in (
        "protocol", "finish_reason", "parse_stage", "parse_category", "raw_evidence_omitted_reason"
    )):
        return False
    if (value["protocol"] != PROTOCOL or value["finish_reason"] not in _FINISH
            or value["parse_stage"] not in _STAGES or value["parse_category"] not in _CATEGORIES
            or value["raw_evidence_omitted_reason"] not in _OMITTED
            or type(value["tool_call_count"]) is not int or not 0 <= value["tool_call_count"] <= MAX_TOOL_CALLS
            or type(value["tool_call_count_bounded"]) is not bool
            or type(value["argument_shapes_omitted_count"]) is not int
            or not 0 <= value["argument_shapes_omitted_count"] <= MAX_TOOL_CALLS
            or not isinstance(value["argument_shapes"], list)
            or len(value["argument_shapes"]) > MAX_ARGUMENT_SHAPES):
        return False
    for index, shape in enumerate(value["argument_shapes"]):
        if (not isinstance(shape, dict) or set(shape) != _SHAPE_KEYS
                or type(shape["index"]) is not int or shape["index"] != index
                or not isinstance(shape["argument_type"], str)
                or shape["argument_type"] not in _TYPES
                or type(shape["arguments_utf8_bytes_bounded"]) is not bool
                or shape["arguments_utf8_bytes"] is not None and (
                    type(shape["arguments_utf8_bytes"]) is not int
                    or not 0 <= shape["arguments_utf8_bytes"] <= MAX_ARGUMENT_UTF8_BYTES)):
            return False
    return True
