"""DeepSeek non-streaming turns with bounded HTTP reads and explicit simulation.

No retries, redirects, model fallback, default model, game facts or fee accounting.
Tool names are syntax-checked here; the runtime enforces its tool allowlist and
each tool's argument schema before dispatching a call.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time

import httpx
from pydantic import SecretStr, ValidationError

from arknights_agent.runtime.budget import EVALUATION_MAX_OUTPUT_TOKENS
from arknights_agent.runtime.contracts import (
    FINISH_REASONS,
    USAGE_FIELDS,
    ModelTurn,
    ProviderError,
    ToolCall,
    finite_json,
    strict_json_loads,
)
from arknights_agent.runtime.provider_argument_failure import build_argument_failure_evidence
from arknights_agent.runtime.provider_response_diagnostic import build_failure_diagnostic
from arknights_agent.runtime.security import contains_secret, redact

ENDPOINT = "https://api.deepseek.com/chat/completions"
MAX_RESPONSE_BYTES = 128 * 1024
MAX_OUTPUT_TOKENS = 1024
UNLIMITED_MAX_OUTPUT_TOKENS = 8192
MAX_REQUEST_BYTES = 200_000
MAX_EVALUATION_REQUEST_BYTES = 256 * 1024
# Direct human authorization in chat 01a12ea6-3825-70c0-b61c-b6109e2aa0e0:
# configured DeepSeek, public game questions, <=18 CNY, no top-ups (2026-10-12).
# Calls still require explicit live settings and the runner's durable budget.
LIVE_CALLS_AUTHORIZED = True
TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
RESPONSE_ID = re.compile(r"^[A-Za-z0-9_-]{1,256}$", re.ASCII)


def _contains_secret(value, secret):
    if isinstance(value, str):
        return secret in value
    if isinstance(value, dict):
        return any(
            _contains_secret(key, secret) or _contains_secret(child, secret)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret(child, secret) for child in value)
    return False


class DeepSeekProvider:
    def endpoint(self):
        return ENDPOINT

    def headers(self, key):
        return {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}

    def normalize_response(self, body):
        return body

    def model_allowed(self):
        return self.model == 'deepseek-flash'

    def __init__(self, settings, transport=None, simulation=False, *, evaluation_limits=False):
        self._settings = settings
        self._transport = transport
        self.simulation = simulation
        self.evaluation_limits = evaluation_limits
        self.model = getattr(settings, "deepseek_model", None)
        self.last_response_id = None
        self._last_failure_receipt = None
        self._last_argument_failure_evidence = None
        self._last_failure_diagnostic = None
        self._preflight()

    def _preflight(self):
        if type(self.simulation) is not bool or type(self.evaluation_limits) is not bool:
            raise ProviderError("invalid_configuration")
        mocked = isinstance(self._transport, httpx.MockTransport)
        if self._transport is not None and not mocked:
            raise ProviderError("invalid_transport")
        if mocked and not self.simulation:
            raise ProviderError("simulation_required")
        if self.simulation and not mocked:
            raise ProviderError("simulation_transport_required")
        if not self.simulation and getattr(self._settings, "model_live_enabled", False) is not True:
            raise ProviderError("live_disabled")
        if not self.simulation and not LIVE_CALLS_AUTHORIZED:
            raise ProviderError("live_authorization_unverified")
        secret = getattr(self._settings, "deepseek_api_key", None)
        if (
            not isinstance(self.model, str)
            or not self.model.strip()
            or self.model != self.model.strip()
            or len(self.model) > 200
            or not isinstance(secret, SecretStr)
            or len(secret) == 0
        ):
            raise ProviderError("configuration_missing")
        if getattr(self._settings, "deepseek_model", None) != self.model:
            raise ProviderError("invalid_configuration")
        if not self.model_allowed():
            raise ProviderError("invalid_configuration")
        timeout = getattr(self._settings, "deepseek_timeout_seconds", None)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 60
        ):
            raise ProviderError("invalid_configuration")

    def _request(self, messages, tools, max_output_tokens, response_format=None):
        evaluation = getattr(self, "evaluation_limits", False) is True
        maximum = EVALUATION_MAX_OUTPUT_TOKENS if evaluation else MAX_OUTPUT_TOKENS
        if not evaluation and getattr(self._settings, "agent_large_output_enabled", False) is True:
            configured_output = getattr(self._settings, "agent_max_output_tokens", None)
            if type(configured_output) is not int or not 1 <= configured_output <= UNLIMITED_MAX_OUTPUT_TOKENS:
                raise ProviderError("invalid_configuration")
            maximum = configured_output
        if (
            type(max_output_tokens) is not int
            or not 1 <= max_output_tokens <= maximum
            or not isinstance(messages, list)
            or not 1 <= len(messages) <= 512
            or not isinstance(tools, list)
            or len(tools) > 128
        ):
            raise ProviderError("invalid_request")
        if evaluation and (tools or response_format != {"type": "json_object"}):
            raise ProviderError("invalid_request")
        if response_format is not None and (
            not isinstance(response_format, dict)
            or response_format != {"type": "json_object"}
        ):
            raise ProviderError("invalid_request")
        try:
            finite_json(messages)
            finite_json(tools)
            json.dumps({"messages": messages, "tools": tools}, allow_nan=False)
            names = set()
            for tool in tools:
                if (
                    not isinstance(tool, dict)
                    or set(tool) != {"type", "function"}
                    or tool["type"] != "function"
                ):
                    raise ValueError("invalid tool specification")
                function = tool["function"]
                if (
                    not isinstance(function, dict)
                    or not set(function) <= {"name", "description", "parameters"}
                    or not isinstance(function.get("name"), str)
                    or TOOL_NAME.fullmatch(function["name"]) is None
                    or function["name"] in names
                    or ("description" in function and not isinstance(function["description"], str))
                    or not isinstance(function.get("parameters"), dict)
                    or function["parameters"].get("type") != "object"
                ):
                    raise ValueError("invalid tool specification")
                names.add(function["name"])
            for message in messages:
                if not isinstance(message, dict) or message.get("role") not in {
                    "system",
                    "user",
                    "assistant",
                    "tool",
                }:
                    raise ValueError("invalid message")
                role = message["role"]
                allowed = {"role", "content", "name"}
                if role == "assistant":
                    allowed.add("tool_calls")
                if role == "tool":
                    allowed.add("tool_call_id")
                if not set(message) <= allowed or "content" not in message:
                    raise ValueError("invalid message")
                if not isinstance(message["content"], str) and not (
                    role == "assistant" and message["content"] is None
                ):
                    raise ValueError("invalid message")
                if role == "tool" and (
                    not isinstance(message.get("tool_call_id"), str) or not message["tool_call_id"]
                ):
                    raise ValueError("invalid message")
                if "name" in message and not isinstance(message["name"], str):
                    raise ValueError("invalid message")
                if role == "assistant" and "tool_calls" in message:
                    _parse_tool_calls(message["tool_calls"])
        except (ValueError, TypeError, RecursionError, ProviderError):
            raise ProviderError("invalid_request") from None
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "thinking": {"type": "disabled"},
            "max_tokens": max_output_tokens,
        }
        if tools:
            # This workflow requires a tool result, never uncertified prose.
            # Non-thinking DeepSeek supports both required and named choices.
            payload.update(tools=tools, tool_choice="required")
            if len(names) == 1:
                payload["tool_choice"] = {
                    "type": "function", "function": {"name": next(iter(names))}
                }
        if response_format is not None:
            payload["response_format"] = {"type": "json_object"}
        if contains_secret(payload) or redact(payload) != payload:
            raise ProviderError("invalid_request")
        return payload

    def _prepare_request(self, messages, tools, max_output_tokens, response_format=None):
        self._preflight()
        payload = self._request(messages, tools, max_output_tokens, response_format)
        try:
            encoded = json.dumps(
                payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        except (UnicodeError, ValueError, TypeError, RecursionError):
            raise ProviderError("invalid_request") from None
        maximum = MAX_EVALUATION_REQUEST_BYTES if self.evaluation_limits else MAX_REQUEST_BYTES
        if getattr(self._settings, "agent_model_first", False) is True:
            maximum = 8 * 1024 * 1024
        if len(encoded) > maximum:
            raise ProviderError("invalid_request")
        secret = self._settings.deepseek_api_key
        try:
            key = secret.get_secret_value()
        except AttributeError:
            raise ProviderError("configuration_missing") from None
        if not isinstance(key, str) or not key.strip():
            raise ProviderError("configuration_missing")
        if _contains_secret(payload, key):
            raise ProviderError("invalid_request")
        return encoded, key

    def request_size(self, messages, tools, *, max_output_tokens):
        """Measure the complete JSON body without a secret, client or request."""
        payload = self._request(messages, tools, max_output_tokens)
        try:
            return len(json.dumps(payload, ensure_ascii=False, allow_nan=False,
                                  separators=(",", ":")).encode("utf-8"))
        except (UnicodeError, ValueError, TypeError, RecursionError):
            raise ProviderError("invalid_request") from None

    def validate_request(self, messages, tools, *, max_output_tokens, response_format=None):
        """Apply every request guard without a client or a billable HTTP request.

        The runtime calls this before creating a durable budget reservation.
        No credentials or prepared request bytes are returned to its caller.
        """
        self._prepare_request(messages, tools, max_output_tokens, response_format)

    def complete(self, messages, tools, *, max_output_tokens, response_format=None):
        self.last_response_diagnostic = {}
        self.last_response_id = None
        self._last_failure_receipt = None
        self._last_argument_failure_evidence = None
        self._last_failure_diagnostic = None
        encoded, key = self._prepare_request(messages, tools, max_output_tokens, response_format)
        started = time.monotonic()
        body, privacy_blocked = None, False
        try:
            with httpx.Client(
                timeout=self._settings.deepseek_timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                with client.stream(
                    "POST",
                    self.endpoint(),
                    content=encoded,
                    headers=self.headers(key),
                ) as response:
                    if response.status_code != 200:
                        from .http_failure import http_failure_diagnostic
                        self.last_response_diagnostic = http_failure_diagnostic(
                            response, deadline=started + self._settings.deepseek_timeout_seconds,
                            exact_secret=key,
                        )
                        # Rejection diagnostics do not prove a zero invoice.
                        # Unknown usage remains fully held, with no retry.
                        raise ProviderError("http_error")
                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        try:
                            if int(content_length) > MAX_RESPONSE_BYTES:
                                raise ProviderError("response_too_large")
                        except ValueError:
                            raise ProviderError("invalid_response") from None
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        if time.monotonic() - started > self._settings.deepseek_timeout_seconds:
                            raise ProviderError("timeout")
                        if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ProviderError("response_too_large")
                        raw.extend(chunk)
            if time.monotonic() - started > self._settings.deepseek_timeout_seconds:
                raise ProviderError("timeout")
            body = strict_json_loads(bytes(raw).decode("utf-8"))
            if _contains_secret(body, key) or contains_secret(body) or redact(body) != body:
                privacy_blocked = True
                raise ProviderError("invalid_response")
            body = self.normalize_response(body)
            self.last_response_diagnostic = _response_diagnostic(body)
            response_id = body.get("id") if isinstance(body, dict) else None
            if (
                isinstance(response_id, str)
                and RESPONSE_ID.fullmatch(response_id)
                and not contains_secret(response_id)
            ):
                self.last_response_id = response_id
            try:
                return _parse_turn(body, self.model, self.simulation)
            except ProviderError as exc:
                # Billing evidence is independent of a malformed completion.
                # Keep only bounded numeric usage and protocol metadata; never
                # retain upstream prose, arguments, headers or response bytes.
                if exc.code in {"invalid_arguments", "invalid_response"}:
                    receipt = _failure_receipt(
                        body, bytes(raw), self.model, self.simulation, self.last_response_id
                    )
                    if receipt is not None:
                        self._last_failure_receipt = (exc, receipt)
                        if exc.code == "invalid_arguments":
                            evidence = build_argument_failure_evidence(
                                body, receipt, self.model, self.simulation, exact_secret=key
                            )
                            if evidence is not None:
                                self._last_argument_failure_evidence = (exc, evidence)
                raise
        except ProviderError as exc:
            self._record_failure_diagnostic(exc, body, privacy_blocked=privacy_blocked, exact_secret=key)
            raise
        except httpx.TimeoutException:
            error = ProviderError("timeout")
            self._record_failure_diagnostic(error, body, exact_secret=key)
            raise error from None
        except httpx.HTTPError as exc:
            # Class only: never persist exception text, URLs, headers or keys.
            kinds = {"ConnectError", "ReadError", "WriteError", "CloseError", "PoolTimeout",
                     "RemoteProtocolError", "LocalProtocolError", "ProxyError",
                     "UnsupportedProtocol", "DecodingError", "TooManyRedirects"}
            kind = type(exc).__name__
            self.last_response_diagnostic = {"transport_error_type": kind if kind in kinds else "HTTPError"}
            error = ProviderError("request_error")
            self._record_failure_diagnostic(error, body, exact_secret=key)
            raise error from None
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, ValidationError):
            error = ProviderError("invalid_response")
            self._record_failure_diagnostic(error, body, privacy_blocked=privacy_blocked, exact_secret=key)
            raise error from None

    def _record_failure_diagnostic(self, error, body, *, privacy_blocked=False, exact_secret=None):
        # Diagnostics never alter the original exception, receipt or retry
        # permission. If classification itself cannot be bounded, omit it.
        try:
            diagnostic = build_failure_diagnostic(
                body, error.code, privacy_blocked=privacy_blocked,
                evidence_retained=self._last_argument_failure_evidence is not None,
                exact_secret=exact_secret)
        except Exception:
            diagnostic = None
        if diagnostic is not None:
            self._last_failure_diagnostic = (error, diagnostic)

    def failure_diagnostic(self, error):
        """Return content-free metadata bound to this exact latest exception."""
        current = self._last_failure_diagnostic
        if current is None or current[0] is not error:
            return None
        return copy.deepcopy(current[1])

    def failure_receipt(self, error):
        """Return evidence only for this instance's exact latest failed call.

        A journal/transport error or an old exception cannot borrow another
        response's usage. The runner still applies the reservation's original
        token bounds before any settlement.
        """
        current = self._last_failure_receipt
        if current is None or current[0] is not error:
            return None
        return copy.deepcopy(current[1])

    def argument_failure_evidence(self, error):
        """Full bounded unexecuted arguments for this exact latest exception.

        A receipt remains independently readable for billing even when no safe
        argument projection exists. Neither accessor grants execution or retry.
        """
        current = self._last_argument_failure_evidence
        if current is None or current[0] is not error:
            return None
        return copy.deepcopy(current[1])


def _response_diagnostic(body):
    """Only protocol shape, never upstream prose, arguments or unknown keys."""
    choices = body.get("choices") if isinstance(body, dict) else None
    choice = choices[0] if isinstance(choices, list) and len(choices) == 1 else None
    message = choice.get("message") if isinstance(choice, dict) else None
    calls = message.get("tool_calls") if isinstance(message, dict) else None
    return {
        "single_choice": isinstance(choices, list) and len(choices) == 1,
        "message_has_content": isinstance(message, dict) and "content" in message,
        "tool_calls_present": isinstance(calls, list) and bool(calls),
        "tool_calls_have_index": isinstance(calls, list)
        and any(isinstance(call, dict) and "index" in call for call in calls),
    }


def _parse_usage(body):
    usage = body.get("usage")
    if not isinstance(usage, dict) or not USAGE_FIELDS <= set(usage):
        raise ProviderError("usage_missing")
    usage = {name: usage[name] for name in USAGE_FIELDS}
    if (
        any(type(number) is not int or not 0 <= number <= 2**63 - 1
            for number in usage.values())
        or usage["prompt_cache_hit_tokens"] + usage["prompt_cache_miss_tokens"]
        != usage["prompt_tokens"]
        or usage["prompt_tokens"] + usage["completion_tokens"] != usage["total_tokens"]
    ):
        raise ProviderError("usage_invalid")
    return usage


def _failure_receipt(body, raw, model, simulation, response_id):
    if (not isinstance(body, dict) or body.get("model") != model
            or response_id is None or body.get("id") != response_id
            or not 1 <= len(raw) <= MAX_RESPONSE_BYTES):
        return None
    try:
        usage = _parse_usage(body)
    except ProviderError:
        return None
    return {
        "protocol": "provider-failure-usage-v1", "http_status": 200,
        "response_id": response_id, "model": model, "simulation": simulation,
        "usage": usage, "response_body_sha256": hashlib.sha256(raw).hexdigest(),
        "response_body_bytes": len(raw), "response_structure": _response_diagnostic(body),
    }


def valid_failure_receipt(receipt, model, simulation):
    """Fail closed if an adapter exposes incomplete or incompatible evidence."""
    try:
        return (
            isinstance(receipt, dict)
            and set(receipt) == {"protocol", "http_status", "response_id", "model", "simulation",
                                 "usage", "response_body_sha256", "response_body_bytes",
                                 "response_structure"}
            and receipt.get("protocol") == "provider-failure-usage-v1"
            and type(receipt.get("http_status")) is int and receipt["http_status"] == 200
            and receipt.get("model") == model
            and type(receipt.get("simulation")) is bool and receipt["simulation"] is simulation
            and isinstance(receipt.get("response_id"), str)
            and RESPONSE_ID.fullmatch(receipt["response_id"]) is not None
            and isinstance(receipt.get("response_body_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", receipt["response_body_sha256"]) is not None
            and type(receipt.get("response_body_bytes")) is int
            and 1 <= receipt["response_body_bytes"] <= MAX_RESPONSE_BYTES
            and isinstance(receipt.get("response_structure"), dict)
            and set(receipt["response_structure"]) == {"single_choice", "message_has_content",
                "tool_calls_present", "tool_calls_have_index"}
            and all(type(value) is bool for value in receipt["response_structure"].values())
            and _parse_usage(receipt) == receipt["usage"]
            and not contains_secret(receipt) and redact(receipt) == receipt
        )
    except (ProviderError, ValueError, TypeError, KeyError, RecursionError):
        return False


def _parse_tool_calls(value):
    if not isinstance(value, list) or len(value) > 128:
        raise ProviderError("invalid_response")
    calls = []
    for position, wire in enumerate(value):
        if (
            not isinstance(wire, dict)
            or not {"id", "type", "function"} <= set(wire)
            or not set(wire) <= {"id", "type", "function", "index"}
            or wire["type"] != "function"
            or ("index" in wire and (type(wire["index"]) is not int or wire["index"] != position))
        ):
            raise ProviderError("invalid_response")
        function = wire["function"]
        if (
            not isinstance(function, dict)
            or set(function) != {"name", "arguments"}
            or not isinstance(function["arguments"], str)
        ):
            raise ProviderError("invalid_arguments")
        try:
            arguments = strict_json_loads(function["arguments"])
            call = ToolCall(id=wire["id"], name=function["name"], arguments=arguments)
        except (ValueError, TypeError, KeyError, RecursionError, ValidationError):
            raise ProviderError("invalid_arguments") from None
        if any(previous.id == call.id for previous in calls):
            raise ProviderError("invalid_response")
        calls.append(call)
    return calls


def _parse_turn(body, model, simulation):
    if not isinstance(body, dict) or body.get("model") != model:
        raise ProviderError("model_mismatch")
    usage = _parse_usage(body)
    choices = body.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ProviderError("invalid_response")
    choice = choices[0]
    if (
        type(choice.get("index")) is not int
        or choice["index"] != 0
        or choice.get("finish_reason") not in FINISH_REASONS
    ):
        raise ProviderError("invalid_response")
    message = choice.get("message")
    if (
        not isinstance(message, dict)
        or message.get("role") != "assistant"
        or "content" not in message
    ):
        raise ProviderError("invalid_response")
    try:
        return ModelTurn(
            content=message["content"],
            tool_calls=_parse_tool_calls(message.get("tool_calls", [])),
            usage=usage,
            finish_reason=choice["finish_reason"],
            model=body["model"],
            simulation=simulation,
            response_items=body.get("_response_items", []),
        )
    except ValidationError:
        raise ProviderError("invalid_response") from None
