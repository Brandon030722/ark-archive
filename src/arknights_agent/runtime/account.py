"""Explicit, read-only DeepSeek account preflight; never a status-page side effect.

Balances describe the account at this instant, not campaign spending or a fee
invoice. The durable campaign ledger remains the authority for the spending cap.
"""

from __future__ import annotations

import math
import re
import time
from decimal import Decimal, InvalidOperation

import httpx
from pydantic import SecretStr

from arknights_agent.runtime.contracts import strict_json_loads

BALANCE_ENDPOINT = "https://api.deepseek.com/user/balance"
MAX_RESPONSE_BYTES = 16 * 1024
MAX_TIMEOUT_SECONDS = 10
MAX_BALANCE_CNY = Decimal("1000000000")
_AMOUNT = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_ERROR_CODES = frozenset({
    "account_configuration_missing",
    "account_invalid_configuration",
    "account_invalid_transport",
    "account_timeout",
    "account_request_error",
    "account_http_error",
    "account_response_too_large",
    "account_invalid_response",
    "account_currency_missing",
})


class AccountError(RuntimeError):
    """Expose only a stable code, never upstream bodies or credential values."""

    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and code in _ERROR_CODES else "account_request_error"
        super().__init__(self.code)


def _amount(value) -> Decimal:
    if (
        not isinstance(value, str)
        or len(value) > 64
        or _AMOUNT.fullmatch(value) is None
    ):
        raise AccountError("account_invalid_response")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise AccountError("account_invalid_response") from None
    if not amount.is_finite() or not Decimal(0) <= amount <= MAX_BALANCE_CNY:
        raise AccountError("account_invalid_response")
    return amount


def _parse_balance(body) -> dict:
    if (
        not isinstance(body, dict)
        or type(body.get("is_available")) is not bool
        or not isinstance(body.get("balance_infos"), list)
        or not 1 <= len(body["balance_infos"]) <= 2
    ):
        raise AccountError("account_invalid_response")
    selected = None
    currencies = set()
    for info in body["balance_infos"]:
        if (
            not isinstance(info, dict)
            or info.get("currency") not in {"CNY", "USD"}
            or info["currency"] in currencies
        ):
            raise AccountError("account_invalid_response")
        currencies.add(info["currency"])
        total = _amount(info.get("total_balance"))
        granted = _amount(info.get("granted_balance"))
        topped_up = _amount(info.get("topped_up_balance"))
        if total != granted + topped_up:
            raise AccountError("account_invalid_response")
        if info["currency"] == "CNY":
            selected = {
                "is_available": body["is_available"],
                "currency": "CNY",
                "total_balance_cny": format(total, "f"),
                "granted_balance_cny": format(granted, "f"),
                "topped_up_balance_cny": format(topped_up, "f"),
            }
    if selected is None:
        raise AccountError("account_currency_missing")
    return selected


def read_balance(
    api_key: SecretStr,
    *,
    timeout_seconds: float = 5.0,
    transport=None,
) -> dict:
    """One fixed-endpoint GET; call only from an explicit account preflight.

    A MockTransport supports synthetic protocol tests. Redirects, environment
    proxies and retries are disabled. The return value contains only booleans,
    a known currency and validated Decimal strings; it contains no raw body.
    """
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS
    ):
        raise AccountError("account_invalid_configuration")
    if transport is not None and not isinstance(transport, httpx.MockTransport):
        raise AccountError("account_invalid_transport")
    if not isinstance(api_key, SecretStr) or not len(api_key):
        raise AccountError("account_configuration_missing")
    key = api_key.get_secret_value()
    if (
        not key
        or key != key.strip()
        or len(key) > 4096
        or any(ord(char) < 33 or ord(char) > 126 for char in key)
    ):
        raise AccountError("account_invalid_configuration")
    started = time.monotonic()
    try:
        with httpx.Client(
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with client.stream(
                "GET", BALANCE_ENDPOINT,
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            ) as response:
                if response.status_code != 200:
                    raise AccountError("account_http_error")
                length = response.headers.get("content-length")
                if length is not None:
                    if not length.isascii() or not length.isdecimal():
                        raise AccountError("account_invalid_response")
                    if int(length) > MAX_RESPONSE_BYTES:
                        raise AccountError("account_response_too_large")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() - started > timeout_seconds:
                        raise AccountError("account_timeout")
                    if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise AccountError("account_response_too_large")
                    raw.extend(chunk)
        if time.monotonic() - started > timeout_seconds:
            raise AccountError("account_timeout")
        return _parse_balance(strict_json_loads(bytes(raw).decode("utf-8")))
    except AccountError:
        raise
    except httpx.TimeoutException:
        raise AccountError("account_timeout") from None
    except httpx.HTTPError:
        raise AccountError("account_request_error") from None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise AccountError("account_invalid_response") from None
