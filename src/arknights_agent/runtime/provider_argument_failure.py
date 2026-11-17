"""Bounded unexecuted tool-argument evidence, separate from billing receipts.

This module never repairs JSON, executes a tool or retries a provider. Raw model
arguments remain untrusted data. A caller may offer feedback only after the
matching known-usage reservation was successfully settled under its own guards.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re

from arknights_agent.runtime.contracts import USAGE_FIELDS, strict_json_loads
from arknights_agent.runtime.security import contains_secret, redact

PROTOCOL = "provider-argument-failure-v1"
MAX_ARGUMENT_BYTES = 4096
MAX_EVIDENCE_BYTES = 8192
_ID = re.compile(r"[A-Za-z0-9_-]{1,256}", re.ASCII)
_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}", re.ASCII)
_ESCAPED = re.compile(r'\\(?:u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2})|(["\\/bfnrt]))')
_FIELD_LITERAL = re.compile(r"[\"']([^\"']{1,128})[\"']\s*:")
_KEYS = frozenset({
    "protocol", "error_code", "response_id", "model", "simulation", "usage",
    "response_body_sha256", "response_body_bytes", "finish_reason", "tool_call_id",
    "tool_name", "arguments", "arguments_utf8_bytes", "arguments_sha256", "failure_kind",
})
_BOUND_RECEIPT_KEYS = (
    "response_id", "model", "simulation", "usage", "response_body_sha256", "response_body_bytes",
)


def _safe_argument_data(arguments, exact_secret=None):
    # Privacy views only: none is returned, stored, parsed as a tool or used to
    # repair the original argument string. Decode bounded common escapes so a
    # malformed JSON string cannot hide an ordinary credential key/value.
    value = arguments
    escapes = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
    for _ in range(8):
        if (contains_secret(value) or redact(value) != value
                or exact_secret and exact_secret in value
                or any(contains_secret({key: None}) for key in _FIELD_LITERAL.findall(value))):
            return False
        decoded = _ESCAPED.sub(lambda m: chr(int(m[1] or m[2], 16))
                               if m[1] or m[2] else escapes.get(m[3], m[3]), value)
        if decoded == value:
            return True
        value = decoded
    return False


def _argument_error(arguments):
    try:
        value = strict_json_loads(arguments)
    except (ValueError, TypeError, RecursionError):
        return "invalid_json"
    return None if isinstance(value, dict) else "arguments_not_object"


def valid_argument_failure_evidence(evidence, receipt, model, simulation):
    """Check complete bounded raw arguments and their exact billing identity.

    This is structural evidence, not permission to execute or spend. The caller
    must also check its current tool allowlist, settlement and remaining limits.
    """
    from arknights_agent.runtime.providers import valid_failure_receipt

    try:
        if (not valid_failure_receipt(receipt, model, simulation)
                or not isinstance(evidence, dict) or set(evidence) != _KEYS
                or evidence["protocol"] != PROTOCOL or evidence["error_code"] != "invalid_arguments"
                or evidence["finish_reason"] != "tool_calls"
                or any(type(evidence[key]) is not type(receipt[key])
                       or evidence[key] != receipt[key] for key in _BOUND_RECEIPT_KEYS)
                or set(evidence["usage"]) != USAGE_FIELDS
                or any(type(value) is not int for value in evidence["usage"].values())
                or not isinstance(evidence["tool_call_id"], str)
                or _ID.fullmatch(evidence["tool_call_id"]) is None
                or not isinstance(evidence["tool_name"], str)
                or _NAME.fullmatch(evidence["tool_name"]) is None
                or not isinstance(evidence["arguments"], str)
                or len(evidence["arguments"]) > MAX_ARGUMENT_BYTES
                or type(evidence["arguments_utf8_bytes"]) is not int
                or not isinstance(evidence["arguments_sha256"], str)
                or evidence["failure_kind"] not in {"invalid_json", "arguments_not_object"}
                or contains_secret(evidence) or redact(evidence) != evidence
                or not _safe_argument_data(evidence["arguments"])):
            return False
        raw = evidence["arguments"].encode("utf-8")
        encoded = json.dumps(evidence, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
        return (len(raw) == evidence["arguments_utf8_bytes"] <= MAX_ARGUMENT_BYTES
                and len(encoded) <= MAX_EVIDENCE_BYTES
                and hashlib.sha256(raw).hexdigest() == evidence["arguments_sha256"]
                and _argument_error(evidence["arguments"]) == evidence["failure_kind"])
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        return False


def build_argument_failure_evidence(body, receipt, model, simulation, *, exact_secret=None):
    """Project one safe failed completion; omit any incomplete/ambiguous case.

    The provider calls this only after checking the complete original response
    for its configured secret and general credential patterns. No prose, headers,
    sibling calls or truncated argument fragment enters the projection.
    """
    try:
        if (not isinstance(body, dict) or contains_secret(body) or redact(body) != body
                or body.get("model") != model
                or not isinstance(receipt, dict) or body.get("id") != receipt.get("response_id")):
            return None
        choices = body.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            return None
        choice = choices[0]
        if (not isinstance(choice, dict) or type(choice.get("index")) is not int
                or choice["index"] != 0 or choice.get("finish_reason") != "tool_calls"):
            return None
        message = choice.get("message")
        if (not isinstance(message, dict) or message.get("role") != "assistant"
                or "content" not in message
                or message["content"] is not None and not isinstance(message["content"], str)):
            return None
        calls = message.get("tool_calls")
        if not isinstance(calls, list) or len(calls) != 1:
            return None
        call = calls[0]
        if (not isinstance(call, dict) or not {"id", "type", "function"} <= set(call)
                or not set(call) <= {"id", "type", "function", "index"}
                or call["type"] != "function"
                or "index" in call and (type(call["index"]) is not int or call["index"] != 0)):
            return None
        function = call["function"]
        if (not isinstance(function, dict) or set(function) != {"name", "arguments"}
                or not isinstance(function["arguments"], str)):
            return None
        raw = function["arguments"].encode("utf-8")
        if len(raw) > MAX_ARGUMENT_BYTES or not _safe_argument_data(function["arguments"], exact_secret):
            return None
        evidence = {
            "protocol": PROTOCOL, "error_code": "invalid_arguments",
            **{key: copy.deepcopy(receipt[key]) for key in _BOUND_RECEIPT_KEYS},
            "finish_reason": "tool_calls", "tool_call_id": call["id"],
            "tool_name": function["name"], "arguments": function["arguments"],
            "arguments_utf8_bytes": len(raw), "arguments_sha256": hashlib.sha256(raw).hexdigest(),
            "failure_kind": _argument_error(function["arguments"]),
        }
        return evidence if valid_argument_failure_evidence(evidence, receipt, model, simulation) else None
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        return None
