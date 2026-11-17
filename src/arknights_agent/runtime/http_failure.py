"""Bounded, allowlisted provider error metadata. Never persist upstream prose."""

import time

from .contracts import strict_json_loads
from .security import contains_secret, redact

MAX_ERROR_BYTES = 16384
ERROR_CODES = frozenset({
    "invalid_api_key", "invalid_request_error", "model_not_found", "model_not_supported",
    "unsupported_parameter", "unsupported_value", "invalid_value", "invalid_type",
    "missing_required_parameter", "invalid_function_parameters", "invalid_json_schema",
    "context_length_exceeded", "insufficient_quota", "rate_limit_exceeded",
    "unsupported_country_region_territory", "permission_denied", "access_terminated",
    "authentication_error", "permission_error", "not_found_error", "rate_limit_error",
    "overloaded_error", "api_error", "server_error",
})
PARAMETERS = frozenset({
    "model", "messages", "tools", "tool_choice", "max_tokens", "max_completion_tokens",
    "temperature", "top_p", "stream", "response_format", "reasoning_effort", "service_tier",
    "input", "reasoning", "max_output_tokens", "text", "include", "store",
})


def http_failure_diagnostic(response, *, deadline, exact_secret):
    result = {"http_status": response.status_code}
    # Redirects are not followed or interpreted as provider error envelopes.
    if not 400 <= response.status_code <= 599:
        return result
    try:
        raw = bytearray()
        for chunk in response.iter_bytes():
            if time.monotonic() > deadline or len(raw) + len(chunk) > MAX_ERROR_BYTES:
                return result
            raw.extend(chunk)
        value = strict_json_loads(bytes(raw).decode("utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("error"), dict):
            return result
        error = value["error"]
        text = str(error)
        if exact_secret in text or contains_secret(error) or redact(error) != error:
            return result
        for field in ("code", "type"):
            item = error.get(field)
            if isinstance(item, str) and item in ERROR_CODES:
                result["error_" + field] = item
        param = error.get("param")
        if isinstance(param, str) and param in PARAMETERS:
            result["error_param"] = param
        # Some providers only express these fixed protocol categories in a
        # message. Retain the category, never the message or echoed values.
        message = error.get("message")
        if "error_code" not in result and isinstance(message, str):
            for phrase, code in (("unsupported parameter", "unsupported_parameter"),
                                 ("unsupported value", "unsupported_value")):
                if message.lower().startswith(phrase):
                    result["error_code"] = code
                    break
    except Exception:
        # A broken error body cannot erase the already observed HTTP status.
        pass
    return result
