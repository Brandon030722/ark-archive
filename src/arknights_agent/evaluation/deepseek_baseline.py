"""Bounded DeepSeek general-model predictions for evaluation only.

This module never reads a case's hidden expectation, runs tools, or publishes
game facts. JSON mode is a different wire protocol from the legacy OpenAI
baseline; the public task and strict prediction contract remain shared.
Every attempted request uses the existing durable campaign ledger, with no
retries, automatic reviews, released unknown reservations, or extra allowance.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from arknights_agent.errors import InvalidData
from arknights_agent.evaluation.baselines import (
    ARMS,
    PROMPT_VERSION,
    _bind_cases,
    _decode_output,
    _empty,
    _hash,
    _instructions,
    _json,
    build_public_input,
    response_schema,
)
from arknights_agent.runtime.budget import (
    CONTEXT_RESERVATION_INPUT_TOKENS,
    EVALUATION_MAX_OUTPUT_TOKENS,
    LIVE_BUDGET_PATH,
    MAX_OUTPUT_TOKENS,
    RESERVATION_INPUT_TOKENS,
    VERIFIED_MODEL,
    BudgetError,
    BudgetLedger,
)
from arknights_agent.runtime.contracts import ModelTurn, ProviderError
from arknights_agent.runtime.providers import MAX_EVALUATION_REQUEST_BYTES, DeepSeekProvider
from arknights_agent.runtime.security import contains_secret

ADAPTER_PROMPT_VERSION = PROMPT_VERSION + "+deepseek-json-v1"
RESOURCE_PROMPT_VERSION = ADAPTER_PROMPT_VERSION + "+resource-v2"
EVALUATION_RESOURCE_PROTOCOLS = {"json_resource_v2", "inline_v1", "native_v2", "native_v2_3072"}
NATIVE_SMALL_OUTPUT_TOKENS = 3072
RESPONSE_FORMAT = {"type": "json_object"}


def _messages(public_input, arm):
    return [
        {
            "role": "system",
            "content": _instructions(arm)
            + "\nJSON输出必须包含下列schema中的全部字段，且不能有额外字段。"
            + "其中value_json、conditions_json和field_value_json是JSON字符串或schema规定的null。"
            + "输出JSON结构schema："
            + _json(response_schema())
            + "\n以下只展示空JSON结构，不是当前题的答案："
            + _json({
                "status": "pending_manual", "conditions_json": "{}", "facts": [],
                "combinations": [], "citations": [], "answer": "",
            }),
        },
        {"role": "user", "content": _json(public_input)},
    ]


def build_prompt(case, arm):
    """Return only messages made from the original public-input whitelist."""
    return _messages(build_public_input(case, arm), arm)


def to_prediction(content, case, arm, trace, latency_ms):
    """Apply the unchanged ModelOutput decoder and strict prediction validator."""
    return _decode_output(
        {
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": content}]}
            ],
        },
        case,
        arm,
        trace,
        latency_ms,
    )


def _protocol(protocol):
    if protocol == "json_v1":
        return ADAPTER_PROMPT_VERSION, build_public_input, _messages, to_prediction
    if protocol == "json_resource_v2":
        return RESOURCE_PROMPT_VERSION, build_public_input, _messages, to_prediction
    if protocol == "inline_v1":
        from arknights_agent.evaluation import inline_citations

        return (
            inline_citations.PROMPT_VERSION, build_public_input,
            inline_citations._messages, inline_citations.to_prediction,
        )
    if protocol in {"native_v2", "native_v2_3072"}:
        from arknights_agent.evaluation import native_citations

        return (
            native_citations.PROMPT_VERSION + ("+output-3072" if protocol == "native_v2_3072" else ""), build_public_input,
            native_citations._messages, native_citations.to_prediction,
        )
    if protocol == "compact_v1":
        from arknights_agent.evaluation import compact_wire

        return (
            compact_wire.COMPACT_PROMPT_VERSION, compact_wire.build_compact_input,
            compact_wire._messages, compact_wire.to_prediction,
        )
    raise InvalidData("DeepSeek evaluation protocol must be json_v1, compact_v1, json_resource_v2, inline_v1, native_v2 or native_v2_3072")


def _trace(arm, model, simulation, *, prompt_version=ADAPTER_PROMPT_VERSION,
           max_output_tokens=MAX_OUTPUT_TOKENS):
    return {
        "arm": arm,
        "provider": "deepseek",
        "model": model,
        "prompt_version": prompt_version,
        "base_prompt_version": PROMPT_VERSION,
        "execution_origin": "not_run",
        "simulation": simulation,
        "measured_model_run": False,
        "evaluation_only": True,
        "facts_verified": False,
        "prompt_hash": None,
        "input_hash": None,
        "source_packet_hash": None,
        "source_packet_chars": 0,
        "collected_at": datetime.now(UTC).isoformat(),
        "usage": None,
        "response_id": None,
        "response_content": None,
        "finish_reason": None,
        "receipt": None,
        "request_parameters": {
            "max_output_tokens": max_output_tokens,
            "response_format": dict(RESPONSE_FORMAT),
            "thinking": {"type": "disabled"},
            "tools": [],
            "stream": False,
            "retries": 0,
        },
    }


def _ledger(settings, simulation, budget_ledger):
    try:
        configured_path = Path(settings.model_budget_path).resolve()
    except (AttributeError, TypeError, ValueError):
        raise BudgetError("budget_argument_invalid") from None
    if budget_ledger is not None and not isinstance(budget_ledger, BudgetLedger):
        raise BudgetError("budget_argument_invalid")
    if not simulation:
        if configured_path != LIVE_BUDGET_PATH.resolve() or (
            budget_ledger is not None and budget_ledger.path != LIVE_BUDGET_PATH.resolve()
        ):
            raise BudgetError("budget_path_mismatch")
    elif configured_path == LIVE_BUDGET_PATH.resolve() or (
        budget_ledger is not None and budget_ledger.path == LIVE_BUDGET_PATH.resolve()
    ):
        raise BudgetError("simulation_ledger_required")
    return budget_ledger or BudgetLedger(
        configured_path,
        budget_cny=settings.model_total_budget_cny,
        safety_fraction=settings.model_budget_safety_fraction,
        model_id=settings.deepseek_model,
        price_verified_at=settings.model_price_verified_at,
        price_source=settings.model_price_source,
    )


def run_deepseek_baseline(
    cases,
    settings,
    arm="closed_book",
    transport=None,
    simulation=False,
    budget_ledger=None,
    on_prediction: Callable[[dict], None] | None = None,
    protocol="json_v1",
):
    """Return one bound prediction and receipt per case, in input order.

    MockTransport requires explicit ``simulation=True`` and a separate ledger.
    Live calls require explicit live settings and the fixed campaign path.
    ``on_prediction`` runs immediately after each appended row, including
    errors and not-run rows, so a caller can persist receipts incrementally.
    A callback failure propagates after the request's billing outcome is saved.
    """
    bound = _bind_cases(cases)
    prompt_version, input_builder, message_builder, decoder = _protocol(protocol)
    reservation_input_tokens = (
        CONTEXT_RESERVATION_INPUT_TOKENS if protocol in {"compact_v1", *EVALUATION_RESOURCE_PROTOCOLS}
        else RESERVATION_INPUT_TOKENS
    )
    max_output_tokens = (
        NATIVE_SMALL_OUTPUT_TOKENS if protocol == "native_v2_3072" else
        EVALUATION_MAX_OUTPUT_TOKENS if protocol in EVALUATION_RESOURCE_PROTOCOLS else MAX_OUTPUT_TOKENS
    )
    if arm not in ARMS:
        raise InvalidData("基线 arm 必须是 closed_book 或 source_packet。")
    if on_prediction is not None and not callable(on_prediction):
        raise InvalidData("基线进度回调必须可调用。")
    model = getattr(settings, "deepseek_model", None)
    model = model if model == VERIFIED_MODEL else None
    predictions = []

    def append(row):
        predictions.append(row)
        if on_prediction is not None:
            on_prediction(row)

    def skipped(case, reason, *, trace=None):
        return _empty(
            case,
            arm,
            "not_run",
            reason,
            model=model,
            trace={**_trace(arm, model, simulation is True, prompt_version=prompt_version,
                           max_output_tokens=max_output_tokens),
                   **(trace or {})},
        )

    if not bound:
        return predictions
    try:
        options = {"evaluation_limits": True} if protocol in EVALUATION_RESOURCE_PROTOCOLS else {}
        provider = DeepSeekProvider(settings, transport=transport, simulation=simulation, **options)
        ledger = _ledger(settings, simulation, budget_ledger)
    except (ProviderError, BudgetError) as exc:
        for case in bound.values():
            append(skipped(case, exc.code))
        return predictions

    stop_reason = None
    last_budget = None
    for case in bound.values():
        if stop_reason is not None:
            append(skipped(case, stop_reason, trace={"batch_stopped": True, "budget": last_budget}))
            continue
        trace = _trace(arm, provider.model, provider.simulation, prompt_version=prompt_version,
                       max_output_tokens=max_output_tokens)
        try:
            public_input = input_builder(case, arm)
            messages = message_builder(public_input, arm)
            trace.update(
                prompt_hash=_hash({
                    "messages": messages,
                    "response_format": RESPONSE_FORMAT,
                    "max_output_tokens": max_output_tokens,
                    "tools": [],
                    "thinking": {"type": "disabled"},
                }),
                input_hash=_hash(public_input),
                source_packet_hash=_hash(public_input.get("source_packet", []))
                if arm == "source_packet" else None,
                source_packet_chars=len(_json(public_input.get("source_packet", [])))
                if arm == "source_packet" else 0,
            )
            if protocol in EVALUATION_RESOURCE_PROTOCOLS:
                trace.update(protocol=protocol, request_bytes_limit=MAX_EVALUATION_REQUEST_BYTES,
                             reservation_input_tokens=reservation_input_tokens)
            if protocol == "compact_v1":
                original_input = build_public_input(case, arm)
                trace.update(
                    protocol=protocol,
                    original_input_hash=_hash(original_input),
                    original_source_packet_hash=_hash(original_input.get("source_packet", []))
                    if arm == "source_packet" else None,
                    source_table_hash=_hash(public_input.get("source_table", []))
                    if arm == "source_packet" else None,
                    reservation_input_tokens=reservation_input_tokens,
                )
            provider.validate_request(
                messages,
                [],
                max_output_tokens=max_output_tokens,
                response_format=RESPONSE_FORMAT,
            )
        except (InvalidData, UnicodeError, ValueError, TypeError, RecursionError):
            append(skipped(case, "public_input_or_packet_invalid", trace=trace))
            continue
        except ProviderError as exc:
            append(skipped(case, exc.code, trace=trace))
            continue
        try:
            reservation = ledger.reserve(
                input_tokens=reservation_input_tokens,
                output_tokens=max_output_tokens,
                input_price=settings.model_input_cny_per_million,
                output_price=settings.model_output_cny_per_million,
            )
        except BudgetError as exc:
            stop_reason = exc.code
            last_budget = ledger.status()
            append(skipped(case, exc.code, trace={**trace, "budget": last_budget}))
            continue

        started = time.perf_counter()
        trace["execution_origin"] = "protocol_simulation" if provider.simulation else "live_api"
        receipt = {
            "model": provider.model,
            "prompt_version": prompt_version,
            "prompt_hash": trace["prompt_hash"],
            "input_hash": trace["input_hash"],
            "output_hash": None,
            "usage": None,
            "response_id": None,
            "latency_ms": None,
            "reservation_id": reservation,
            "simulation": provider.simulation,
            "request_attempted": True,
            "error_code": None,
            "budget": None,
        }
        trace["receipt"] = receipt
        try:
            turn = provider.complete(
                messages,
                [],
                max_output_tokens=max_output_tokens,
                response_format=RESPONSE_FORMAT,
            )
            if (
                not isinstance(turn, ModelTurn)
                or turn.simulation != provider.simulation
                or turn.model != provider.model
            ):
                raise ProviderError("invalid_response")
            elapsed = (time.perf_counter() - started) * 1000
            last_budget = ledger.settle(reservation, turn.usage)
            trace["usage"] = dict(turn.usage)
            trace["response_id"] = provider.last_response_id
            trace["finish_reason"] = turn.finish_reason
            trace["response_content"] = None if contains_secret(turn.content) else turn.content
            trace["output_hash"] = hashlib.sha256(
                (turn.content or "").encode("utf-8", errors="surrogatepass")
            ).hexdigest()
            trace["budget"] = last_budget
            trace["measured_model_run"] = not provider.simulation
            receipt.update(
                usage=dict(turn.usage),
                response_id=provider.last_response_id,
                output_hash=trace["output_hash"],
                latency_ms=elapsed,
                budget=last_budget,
                finish_reason=turn.finish_reason,
            )
        except (ProviderError, BudgetError) as exc:
            stop_reason = exc.code
            last_budget = ledger.fail_unknown(reservation, exc.code)
            elapsed = (time.perf_counter() - started) * 1000
            receipt.update(error_code=exc.code, latency_ms=elapsed, budget=last_budget)
            trace.update(budget=last_budget, response_id=provider.last_response_id)
            receipt["response_id"] = trace["response_id"]
            append(_empty(
                case, arm, "error", exc.code, model=model,
                trace={**trace, "reason": exc.code}, latency_ms=elapsed,
            ))
            continue
        except Exception:
            stop_reason = "provider_error"
            last_budget = ledger.fail_unknown(reservation, stop_reason)
            elapsed = (time.perf_counter() - started) * 1000
            receipt.update(error_code=stop_reason, latency_ms=elapsed, budget=last_budget)
            append(_empty(
                case, arm, "error", stop_reason, model=model,
                trace={**trace, "budget": last_budget, "reason": stop_reason}, latency_ms=elapsed,
            ))
            continue
        except (KeyboardInterrupt, SystemExit):
            ledger.fail_unknown(reservation, "interrupted")
            raise

        if last_budget["reservation_state"] != "settled" or last_budget["frozen"]:
            stop_reason = last_budget["reservation_error_code"] or "usage_invalid"
            receipt["error_code"] = stop_reason
            append(_empty(
                case, arm, "error", stop_reason, model=model,
                trace={**trace, "reason": stop_reason}, latency_ms=elapsed,
            ))
            continue
        try:
            if (
                turn.finish_reason != "stop" or turn.tool_calls
                or not isinstance(turn.content, str) or contains_secret(turn.content)
            ):
                raise ValueError("not a completed JSON prediction")
            row = decoder(turn.content, case, arm, trace, elapsed)
        except (InvalidData, ValidationError, ValueError, KeyError, TypeError, RecursionError):
            reason = "model_output_truncated" if turn.finish_reason == "length" else "model_output_invalid"
            receipt["error_code"] = reason
            row = _empty(
                case, arm, "error", reason, model=model,
                trace={**trace, "reason": reason}, latency_ms=elapsed,
            )
        append(row)
    return predictions
