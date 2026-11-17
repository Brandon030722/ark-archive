"""One append-only account for HTTP tasks and the bounded Stage40 development run.

The spending-cap waiver changes no old campaign, allowance or reservation row.
Every real attempt still has a full hold, a genuine current-and-historical
request proof, one shared in-flight slot and the original unknown freeze.
"""
from __future__ import annotations

import copy
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from arknights_agent.runtime.bounded_profile import BOUNDED_INPUT_TOKENS, STAGE16_PROFILE_ID
from arknights_agent.runtime.budget import (
    CAMPAIGN_ID,
    INPUT_PRICE_FLOOR,
    LIVE_BUDGET_PATH,
    MAX_OUTPUT_TOKENS,
    OFFICIAL_PRICE_SOURCE,
    OUTPUT_PRICE_FLOOR,
    VERIFIED_MODEL,
    BudgetError,
    BudgetLedger,
    _cost,
    _decimal,
    _money,
)
from arknights_agent.runtime.context_profile import (
    MAX_WIRE_BYTES as CURRENT_CONTEXT_MAX_WIRE_BYTES,
)
from arknights_agent.runtime.context_profile import (
    PROFILE_ID as CURRENT_CONTEXT_PROFILE_ID,
)
from arknights_agent.runtime.context_profile import (
    REVIEW_MAX_WIRE_BYTES,
    REVIEW_WIRE_PROFILE_ID,
    CurrentContextBoundedRequest,
    CurrentContextToolProfile,
)
from arknights_agent.runtime.long_context import (
    CONTEXT_TOKENS,
    LongContextBoundedRequest,
    LongContextToolProfile,
)
from arknights_agent.runtime.long_context import (
    INPUT_TOKENS as LONG_INPUT_TOKENS,
)
from arknights_agent.runtime.long_context import (
    MAX_WIRE_BYTES as LONG_WIRE_BYTES,
)
from arknights_agent.runtime.long_context import (
    PROFILE_ID as LONG_PROFILE_ID,
)
from arknights_agent.runtime.stage26_current_model import MAX_EXPLICIT_OUTPUT_TOKENS, price_receipt
from arknights_agent.runtime.stage26_model_profile import (
    CurrentModelBoundedRequest,
)
from arknights_agent.runtime.stage38_budget import (
    ABSOLUTE_LIMIT,
    CUMULATIVE_REQUEST_LIMIT,
    MAX_ATTEMPTS,
    SEND_HOLD,
    Stage38BudgetLedger,
    attempt_tables,
    attempts,
    require,
)

ATTEMPT_TABLE = "stage40_request_attempt"
MAX_REQUESTS = 27


class Stage40BudgetLedger(Stage38BudgetLedger):
    """Persist request identity and HTTP task identity in the existing campaign."""

    def __init__(self, path, *, model_id, price_verified_at,
                 price_source=OFFICIAL_PRICE_SOURCE, clock=None, task_id=None,
                 request_limit=MAX_REQUESTS, unlimited=False):
        self.path = Path(path).resolve()
        require(self.path.is_file(), "fixed_ledger_absent")
        require(model_id == VERIFIED_MODEL, "model_not_verified")
        require(price_source == OFFICIAL_PRICE_SOURCE, "price_source_not_verified")
        require(isinstance(price_verified_at, datetime) and price_verified_at.tzinfo is not None,
                "price_verification_invalid")
        require(task_id is None or self._valid_task_id(task_id), "stage40_task_identity_invalid")
        require(type(request_limit) is int and 1 <= request_limit <= CUMULATIVE_REQUEST_LIMIT,
                "web_request_limit_invalid")
        require(type(unlimited) is bool, "unlimited_policy_invalid")
        self.request_limit = request_limit
        self.unlimited = unlimited
        self.model_id, self.price_verified_at, self.task_id = model_id, price_verified_at, task_id
        self.clock = clock or (lambda: datetime.now(UTC))
        # Merely constructing or checking a web policy never initializes a
        # ledger, creates a journal, or changes even a retired receipt.
        with self._readonly() as db:
            require(db.execute("SELECT 1 FROM campaign WHERE campaign_id=?", (CAMPAIGN_ID,)).fetchone(),
                    "fixed_campaign_absent")

    @staticmethod
    def _valid_task_id(value):
        return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None

    @contextmanager
    def _readonly(self):
        # sqlite3.Connection is a context manager; URI mode also prevents an
        # accidental missing path from creating a fresh campaign.
        db = None
        try:
            db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            yield db
        except sqlite3.Error:
            raise BudgetError("ledger_unavailable") from None
        finally:
            if db is not None:
                db.close()

    def for_task(self, task_id):
        require(self._valid_task_id(task_id), "stage40_task_identity_invalid")
        result = copy.copy(self)
        result.task_id = task_id
        return result

    def require_task_not_failed(self, db):
        # A new user turn can continue, but the failed execution cannot resume
        # or consume another reservation, even under task-isolation policy.
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stage40_request_attempt'").fetchone():
            return
        failed = db.execute(
            "SELECT 1 FROM stage40_request_attempt a JOIN reservation r USING(reservation_id) "
            "WHERE a.task_id=? AND r.state='unknown' LIMIT 1", (self.task_id,),
        ).fetchone()
        require(not failed, "task_already_failed")

    def _summary(self, db, reservation=None):
        result = super()._summary(db)
        current = [r for r in attempts(db) if r["attempt_table"] == ATTEMPT_TABLE]
        result.update(stage40_requests=len(current),
                      stage40_max_requests=None if self.unlimited else self.request_limit,
                      stage40_remaining_requests=None if self.unlimited else max(0, self.request_limit - len(current)))
        if self.unlimited:
            result.update(request_limits_enforced=False, identity_attempt_limit_enforced=False,
                          historical_web_limit_not_enforced=self.request_limit,
                          historical_campaign_request_limit_not_enforced=ABSOLUTE_LIMIT,
                          historical_post23_request_limit_not_enforced=CUMULATIVE_REQUEST_LIMIT)
        if reservation is not None:
            row = self._reservation(db, reservation)
            result.update(reservation_id=row["reservation_id"], reservation_state=row["state"],
                          reservation_cny=row["reserved_cny"],
                          reservation_accounted_cny=row["accounted_cny"],
                          reservation_error_code=row["error_code"],
                          usage={key: row[key] for key in (
                              "prompt_tokens", "completion_tokens", "total_tokens",
                              "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")}
                          if row["state"] == "settled" else None)
        return result

    def _reservation(self, db, ident):
        row = BudgetLedger._reservation(db, ident)
        require(ATTEMPT_TABLE in attempt_tables(db), "stage40_cannot_mutate_historical_reservation")
        journal = db.execute(f"SELECT * FROM {ATTEMPT_TABLE} WHERE reservation_id=?", (ident,)).fetchone()
        require(journal is not None and journal["task_id"] == self.task_id,
                "stage40_cannot_mutate_historical_reservation")
        return row

    def _hold_bounded_unknown(self, db, row, reported_usage):
        # The current request was source-verified before reserve. Recording an
        # uncertain outcome must remain possible after source/code freshness
        # changes; it is not another authorization of a historical allowance.
        owned = self._reservation(db, row["reservation_id"])
        require(dict(owned) == dict(row)
                and owned["campaign_id"] == CAMPAIGN_ID
                and owned["input_tokens"] in {BOUNDED_INPUT_TOKENS, LONG_INPUT_TOKENS}
                and owned["state"] in {"reserved", "unknown"},
                "stage40_unknown_reservation_invalid")
        return BudgetLedger._escalate_bounded_unknown(db, owned, reported_usage)

    def status(self):
        with self._readonly() as db:
            return self._summary(db)

    def available(self):
        """Non-reserving safe status for the web model selector."""
        state = self.status()
        reason = "ready"
        if state["frozen"]:
            reason = "campaign_frozen"
        elif state["inflight_count"]:
            reason = "request_inflight"
        elif state["price_verification_status"] != "ready":
            reason = state["price_verification_status"]
        elif not self.unlimited and state["stage40_requests"] >= self.request_limit:
            reason = "stage40_request_limit_reached"
        elif not self.unlimited and state["reservation_count"] >= ABSOLUTE_LIMIT:
            reason = "campaign_request_limit_reached"
        elif not self.unlimited:
            with self._readonly() as db:
                counted = {r["reservation_id"] for r in attempts(db)
                           if int(re.match(r"stage(\d+)", r["attempt_table"])[1]) >= 23}
            if len(counted) >= CUMULATIVE_REQUEST_LIMIT:
                reason = "campaign_request_limit_reached"
        return {"ready": reason == "ready", "reason": reason,
                "stage40_requests": state["stage40_requests"],
                "remaining_requests": state["stage40_remaining_requests"],
                "maximum_requests": None if self.unlimited else self.request_limit}

    def identity_capacity(self, public_input_sha256):
        require(isinstance(public_input_sha256, str)
                and re.fullmatch(r"[a-f0-9]{64}", public_input_sha256),
                "stage40_public_identity_invalid")
        with self._readonly() as db:
            prior = sum(r["public_input_sha256"] == public_input_sha256 for r in attempts(db))
        return {"public_input_sha256": public_input_sha256, "historical_attempts": prior,
                "maximum_attempts": None if self.unlimited else MAX_ATTEMPTS,
                "remaining_attempts": None if self.unlimited else max(0, MAX_ATTEMPTS - prior)}

    def reserve(self, input_tokens, output_tokens, input_price, output_price, *,
                bounded_request=None, continuation_id=None):
        require(self._valid_task_id(self.task_id), "stage40_task_identity_required")
        long_context = type(bounded_request) is LongContextBoundedRequest
        current_context = type(bounded_request) is CurrentContextBoundedRequest or long_context
        expected_input = LONG_INPUT_TOKENS if long_context and self.unlimited else BOUNDED_INPUT_TOKENS
        proved_output = bounded_request.output_tokens if current_context else MAX_OUTPUT_TOKENS
        require((current_context or isinstance(bounded_request, CurrentModelBoundedRequest))
                and type(input_tokens) is int and input_tokens == expected_input
                and type(output_tokens) is int and output_tokens == proved_output
                and proved_output in ({MAX_OUTPUT_TOKENS, MAX_EXPLICIT_OUTPUT_TOKENS}
                                      if self.unlimited and current_context else {MAX_OUTPUT_TOKENS}),
                "reservation_token_bound_invalid")
        bounded_request.validate_current()
        require(bounded_request.continuation_id == continuation_id
                and (bounded_request.profile_id in {CURRENT_CONTEXT_PROFILE_ID, REVIEW_WIRE_PROFILE_ID, LONG_PROFILE_ID}
                     if current_context else bounded_request.profile_id == STAGE16_PROFILE_ID),
                "bounded_request_proof_invalid")
        identity = bounded_request.public_input_sha256
        require(isinstance(identity, str) and re.fullmatch(r"[a-f0-9]{64}", identity),
                "stage40_public_identity_invalid")
        ip = _decimal(input_price, minimum=INPUT_PRICE_FLOOR)
        op = _decimal(output_price, minimum=OUTPUT_PRICE_FLOOR)
        held = _cost(input_tokens, output_tokens, ip, op)
        required_hold = (Decimal(SEND_HOLD) if proved_output == MAX_OUTPUT_TOKENS else
                         _cost(expected_input, proved_output, INPUT_PRICE_FLOOR, OUTPUT_PRICE_FLOOR))
        require(held == required_hold, "stage40_full_send_hold_required")
        with self._transaction() as db:
            if current_context:
                registered = db.execute("SELECT receipt_json FROM bounded_continuation "
                                        "WHERE continuation_id=?", (continuation_id,)).fetchone()
                require(registered is not None
                        and json.loads(registered[0]) == bounded_request.parent_receipt,
                        "bounded_request_parent_receipt_mismatch")
            state = self._summary(db)
            require(not state["frozen"], "campaign_frozen")
            require(not state["inflight_count"], "request_inflight")
            self.require_task_not_failed(db)
            require(state["price_verification_status"] == "ready", state["price_verification_status"])
            old = attempts(db)
            if not self.unlimited:
                require(sum(r["public_input_sha256"] == identity for r in old) < MAX_ATTEMPTS,
                        "stage40_turn_request_limit_reached")
                require(state["stage40_requests"] < self.request_limit, "stage40_request_limit_reached")
                counted = {r["reservation_id"] for r in old
                           if int(re.match(r"stage(\d+)", r["attempt_table"])[1]) >= 23}
                require(len(counted) < CUMULATIVE_REQUEST_LIMIT
                        and state["reservation_count"] < ABSOLUTE_LIMIT, "campaign_request_limit_reached")
            db.execute(f"CREATE TABLE IF NOT EXISTS {ATTEMPT_TABLE} ("
                       "reservation_id TEXT PRIMARY KEY REFERENCES reservation(reservation_id),"
                       "public_input_sha256 TEXT NOT NULL, task_id TEXT NOT NULL, started_at TEXT NOT NULL)")
            for action in ("UPDATE", "DELETE"):
                db.execute(f"CREATE TRIGGER IF NOT EXISTS stage40_attempt_{action.lower()} "
                           f"BEFORE {action} ON {ATTEMPT_TABLE} BEGIN "
                           "SELECT RAISE(ABORT, 'stage40_attempt_append_only'); END")
            ident = uuid4().hex
            db.execute("INSERT INTO reservation (reservation_id,campaign_id,input_tokens,output_tokens,"
                       "input_price,output_price,reserved_cny,state) VALUES (?,?,?,?,?,?,?,'reserved')",
                       (ident, CAMPAIGN_ID, input_tokens, output_tokens, str(ip), str(op), _money(held)))
            db.execute(f"INSERT INTO {ATTEMPT_TABLE} VALUES (?,?,?,?)",
                       (ident, identity, self.task_id, self.clock().astimezone(UTC).isoformat()))
            return ident


def load_runtime(settings, *, pricing_sources, current_model_sources, legacy_source_root,
                 request_limit=MAX_REQUESTS, unlimited=False, semantic_review=False, model_first=False):
    """Explicit server-start injection; reading proofs never calls a provider."""
    require(type(semantic_review) is bool, "semantic_review_configuration_invalid")
    require(type(unlimited) is bool, "unlimited_policy_invalid")
    require(type(model_first) is bool and (not model_first or (unlimited and semantic_review)),
            "model_first_configuration_invalid")
    require(settings.deepseek_model == VERIFIED_MODEL and bool(settings.deepseek_api_key),
            "configured_provider_missing")
    require(settings.model_budget_path.resolve() == LIVE_BUDGET_PATH.resolve(), "budget_path_mismatch")
    price = price_receipt(pricing_sources)
    book = Stage40BudgetLedger(LIVE_BUDGET_PATH, model_id=VERIFIED_MODEL,
                               price_verified_at=datetime.fromisoformat(price["checked_at"]),
                               request_limit=request_limit, unlimited=unlimited)
    with book._readonly() as db:
        receipts = [json.loads(row[0]) for row in db.execute("SELECT receipt_json FROM bounded_continuation")]
    receipts = [row for row in receipts if row["profile_id"] == STAGE16_PROFILE_ID]
    require(len(receipts) == 1, "bounded_source_profile_ambiguous")
    # Future requests have an independent current-encoder proof. Historical
    # receipt/tokenizer bytes and their 16 KiB request contract are untouched.
    output_tokens = MAX_EXPLICIT_OUTPUT_TOKENS if unlimited else MAX_OUTPUT_TOKENS
    profile_tool_calls = 12 if unlimited and semantic_review else 8
    wire_limit = REVIEW_MAX_WIRE_BYTES if unlimited and semantic_review else CURRENT_CONTEXT_MAX_WIRE_BYTES
    profile = CurrentContextToolProfile(receipt=receipts[0],
                                        current_source_root=current_model_sources,
                                        max_output_tokens=output_tokens,
                                        max_tool_calls=profile_tool_calls,
                                        max_messages=32 if profile_tool_calls == 12 else 20,
                                        max_wire_bytes=wire_limit)
    if model_first:
        wire_limit = LONG_WIRE_BYTES
        profile = LongContextToolProfile(receipt=receipts[0], current_source_root=current_model_sources,
                                         max_tool_calls=settings.agent_progress_max_tools)
    from arknights_agent.config import Settings, semantic_review_required_seconds

    model_calls = (8 if unlimited else 6) if semantic_review else 3
    tool_calls = 12 if unlimited and semantic_review else settings.agent_max_tool_calls
    if model_first:
        model_calls = settings.agent_progress_max_calls
        tool_calls = settings.agent_progress_max_tools
    # Explicit unlimited service admission must cover the complete, still
    # finite source-proved flow. The output allowance is carried by the exact
    # current request proof and fully reserved per call; no old row is changed.
    total_tokens = ((BOUNDED_INPUT_TOKENS + output_tokens) * model_calls
                    if unlimited else settings.agent_max_total_tokens)
    if model_first:
        total_tokens = CONTEXT_TOKENS * model_calls
    provider_timeout = (60 if unlimited else 15 if settings.agent_large_output_enabled
                        else settings.deepseek_timeout_seconds)
    total_seconds = settings.agent_max_total_seconds
    if semantic_review or unlimited:
        total_seconds = semantic_review_required_seconds(
            model_calls=model_calls, provider_timeout_seconds=provider_timeout,
            tool_calls=tool_calls,
            database_timeout_ms=settings.database_tool_timeout_ms,
            web_search_enabled=False,
        )
    elif settings.agent_semantic_review or settings.agent_large_output_enabled:
        # An explicit CLI opt-out must restore the normal execution contract,
        # rather than carrying a review-derived deadline into another mode.
        total_seconds = 45
    configured = Settings.model_validate({**settings.model_dump(), **{
        "model_live_enabled": True, "web_search_enabled": False,
        "model_input_cny_per_million": Decimal("2"), "model_output_cny_per_million": Decimal("8"),
        "model_price_verified_at": datetime.fromisoformat(price["checked_at"]),
        "model_price_source": OFFICIAL_PRICE_SOURCE,
        "agent_semantic_review": semantic_review,
        "agent_model_first": model_first,
        "agent_max_model_calls": model_calls,
        "agent_max_tool_calls": tool_calls,
        "agent_large_output_enabled": unlimited,
        "deepseek_timeout_seconds": provider_timeout,
        "agent_max_total_tokens": total_tokens,
        "agent_max_output_tokens": output_tokens,
        "agent_max_prompt_bytes": wire_limit,
        "tool_result_max_bytes": 262144 if model_first else settings.tool_result_max_bytes,
        "agent_roguelike_delivery": "verified_query", "agent_max_total_seconds": total_seconds,
    }})
    return book, profile, configured
