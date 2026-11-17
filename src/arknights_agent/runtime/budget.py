"""Durable conservative model-cost reservations, never a provider invoice.

Only safe identities, token counts and Decimal amounts enter this SQLite ledger.
All model attempts in the fixed campaign share one cap, including restarts.
Unknown billing outcomes retain their reservation and stop the entire campaign.
An explicit reviewed continuation may reopen only invalid-response failures;
their full held costs remain charged against the same cap permanently.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

CAMPAIGN_ID = "deepseek-public-game-eval-18cny"
VERIFIED_MODEL = "deepseek-flash"
OFFICIAL_PRICE_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
MAX_BUDGET_CNY = Decimal("18")
MAX_SAFETY_FRACTION = Decimal("0.8")
INPUT_PRICE_FLOOR = Decimal("2")
OUTPUT_PRICE_FLOOR = Decimal("8")
RESERVATION_INPUT_TOKENS = 2_000_000
# Opt-in for the separately frozen compact protocol. The production runtime and
# original JSON baseline keep their existing two-million-token reservation.
# Official Flash 1M context, clarified by the V4.1 model's published config:
# https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/raw/main/config.json
CONTEXT_RESERVATION_INPUT_TOKENS = 1_048_576
MAX_OUTPUT_TOKENS = 1024
# Evaluation-only output allowance. It requires the separately verified context
# reservation; default model/runtime settings retain the original 1024 limit.
EVALUATION_MAX_OUTPUT_TOKENS = 8192
MAX_CAMPAIGN_RESERVATIONS = 200
MARGIN = Decimal("1.1")
MILLION = Decimal("1000000")
MONEY_UNIT = Decimal("0.00000001")
# One shared validity window for reviewed pricing and current-model evidence.
# Changing this window never changes receipt timestamps or accounting history.
PRICE_TTL = timedelta(days=7)
# A live campaign cannot create another allowance by changing cwd or settings.
LIVE_BUDGET_PATH = Path(__file__).resolve().parents[3] / ".local/model-budget.sqlite3"
USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
)
SAFE_UNKNOWN_CODES = frozenset(
    {
        "timeout",
        "network_timeout",
        "request_failed",
        "transport_error",
        "http_error",
        "provider_error",
        "usage_missing",
        "usage_invalid",
        "missing_usage",
        "invalid_usage",
        "invalid_response",
        "interrupted",
        "unknown_failure",
    }
)


class BudgetError(RuntimeError):
    """A stable public code, with no database path or upstream error text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _decimal(value, *, minimum=Decimal("0"), maximum=Decimal("1000000")):
    if isinstance(value, bool):
        raise BudgetError("budget_argument_invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise BudgetError("budget_argument_invalid") from None
    if not result.is_finite() or not minimum <= result <= maximum:
        raise BudgetError("budget_argument_invalid")
    return result


def _money(value: Decimal) -> str:
    return format(value.quantize(MONEY_UNIT, rounding=ROUND_CEILING), "f")


def _cost(input_tokens, output_tokens, input_price, output_price):
    # Cache hits are deliberately charged at the peak cache-miss input price.
    return (
        (Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price)
        / MILLION
        * MARGIN
    ).quantize(MONEY_UNIT, rounding=ROUND_CEILING)


class BudgetLedger:
    def __init__(
        self,
        path: Path | str,
        *,
        budget_cny=18,
        safety_fraction=Decimal("0.8"),
        account_scope="default",
        model_id=None,
        price_verified_at: datetime | None = None,
        price_source=OFFICIAL_PRICE_SOURCE,
        clock: Callable[[], datetime] | None = None,
    ):
        budget = _decimal(budget_cny, maximum=MAX_BUDGET_CNY)
        safety = _decimal(safety_fraction, maximum=MAX_SAFETY_FRACTION)
        if safety <= 0:
            raise BudgetError("budget_argument_invalid")
        # API-key changes must still use the same campaign, never a key-derived
        # new account or ledger. Also avoid accepting a secret as an identity.
        if account_scope != "default":
            raise BudgetError("account_scope_mismatch")
        if model_id not in (None, VERIFIED_MODEL):
            raise BudgetError("model_not_verified")
        if price_source != OFFICIAL_PRICE_SOURCE:
            raise BudgetError("price_source_not_verified")
        if price_verified_at is not None and (
            not isinstance(price_verified_at, datetime) or price_verified_at.tzinfo is None
        ):
            raise BudgetError("price_verification_invalid")
        self.path = Path(path).resolve()
        self.model_id = model_id
        self.price_verified_at = price_verified_at
        self.clock = clock or (lambda: datetime.now(UTC))
        # Costs round up; the cap rounds down so precision cannot raise it.
        cap = (budget * safety).quantize(MONEY_UNIT, rounding=ROUND_FLOOR)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._transaction() as db:
                db.execute(
                    """CREATE TABLE IF NOT EXISTS campaign (
                    campaign_id TEXT PRIMARY KEY,
                    account_scope TEXT NOT NULL,
                    budget_cny TEXT NOT NULL,
                    safety_fraction TEXT NOT NULL,
                    cap_cny TEXT NOT NULL,
                    frozen INTEGER NOT NULL DEFAULT 0 CHECK(frozen IN (0,1)),
                    freeze_code TEXT,
                    CHECK(campaign_id='deepseek-public-game-eval-18cny'))"""
                )
                db.execute(
                    """CREATE TABLE IF NOT EXISTS reservation (
                    reservation_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    input_price TEXT NOT NULL,
                    output_price TEXT NOT NULL,
                    reserved_cny TEXT NOT NULL,
                    accounted_cny TEXT,
                    state TEXT NOT NULL CHECK(state IN ('reserved','settled','unknown')),
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    prompt_cache_hit_tokens INTEGER,
                    prompt_cache_miss_tokens INTEGER,
                    error_code TEXT)"""
                )
                db.execute(
                    """CREATE UNIQUE INDEX IF NOT EXISTS one_inflight_reservation
                    ON reservation(campaign_id) WHERE state='reserved'"""
                )
                campaign = db.execute(
                    "SELECT * FROM campaign WHERE campaign_id=?", (CAMPAIGN_ID,)
                ).fetchone()
                if campaign is None:
                    db.execute(
                        """INSERT INTO campaign
                        (campaign_id,account_scope,budget_cny,safety_fraction,cap_cny)
                        VALUES (?,?,?,?,?)""",
                        (CAMPAIGN_ID, account_scope, str(budget), str(safety), _money(cap)),
                    )
                else:
                    if campaign["account_scope"] != account_scope:
                        raise BudgetError("account_scope_mismatch")
                    if cap > Decimal(campaign["cap_cny"]):
                        raise BudgetError("campaign_cap_increase_forbidden")
                    if cap < Decimal(campaign["cap_cny"]):
                        db.execute(
                            """UPDATE campaign SET budget_cny=?,safety_fraction=?,cap_cny=?
                            WHERE campaign_id=?""",
                            (str(budget), str(safety), _money(cap), CAMPAIGN_ID),
                        )
        except OSError:
            raise BudgetError("ledger_unavailable") from None

    @contextmanager
    def _transaction(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.Error:
            if db is not None:
                db.rollback()
            raise BudgetError("ledger_unavailable") from None
        except BaseException:
            if db is not None:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    def _price_status(self):
        if self.model_id != VERIFIED_MODEL or self.price_verified_at is None:
            return "price_not_verified"
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            return "price_verification_invalid"
        age = now - self.price_verified_at
        if age < timedelta(0):
            return "price_verification_in_future"
        if age >= PRICE_TTL:
            return "price_verification_expired"
        return "ready"

    def _summary(self, db, reservation=None):
        campaign = db.execute(
            "SELECT * FROM campaign WHERE campaign_id=?", (CAMPAIGN_ID,)
        ).fetchone()
        rows = db.execute(
            "SELECT * FROM reservation WHERE campaign_id=?", (CAMPAIGN_ID,)
        ).fetchall()
        accounted = sum(
            (Decimal(row["accounted_cny"]) for row in rows if row["state"] == "settled"),
            Decimal(0),
        )
        reserved = sum(
            (Decimal(row["reserved_cny"]) for row in rows if row["state"] != "settled"),
            Decimal(0),
        )
        from arknights_agent.runtime.budget_extension import effective_limits

        budget, cap = effective_limits(db, campaign)
        result = {
            "campaign_id": CAMPAIGN_ID,
            "budget_cny": _money(budget),
            "safety_fraction": campaign["safety_fraction"],
            "cap_cny": _money(cap),
            "accounted_cny": _money(accounted),
            "reserved_cny": _money(reserved),
            "remaining_cny": _money(max(Decimal(0), cap - accounted - reserved)),
            "reservation_count": len(rows),
            "inflight_count": sum(row["state"] == "reserved" for row in rows),
            "unknown_count": sum(row["state"] == "unknown" for row in rows),
            "settled_count": sum(row["state"] == "settled" for row in rows),
            "frozen": bool(campaign["frozen"]),
            "freeze_code": campaign["freeze_code"],
            "price_verification_status": self._price_status(),
            "cost_basis": "conservative_high_peak_estimate_not_invoice",
            "conservative_budget_cost_cny": _money(accounted),
            "unresolved_budget_hold_cny": _money(reserved),
            "provider_invoice_cny": None,
            "provider_reported_usage": {
                key: sum(row[key] for row in rows if row["state"] == "settled")
                for key in USAGE_FIELDS
            },
        }
        if reservation is not None:
            row = self._reservation(db, reservation)
            result.update(
                reservation_id=row["reservation_id"],
                reservation_state=row["state"],
                reservation_cny=row["reserved_cny"],
                reservation_accounted_cny=row["accounted_cny"],
                reservation_error_code=row["error_code"],
                usage={key: row[key] for key in USAGE_FIELDS}
                if row["state"] == "settled"
                else None,
            )
        return result

    @staticmethod
    def _reservation(db, ident):
        if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{32}", ident):
            raise BudgetError("reservation_id_invalid")
        row = db.execute(
            "SELECT * FROM reservation WHERE reservation_id=? AND campaign_id=?",
            (ident, CAMPAIGN_ID),
        ).fetchone()
        if row is None:
            raise BudgetError("reservation_unknown")
        return row

    def authorize_bounded_continuation(self, receipt) -> dict:
        """Record an explicit new human request without changing old accounting.

        This is never called by default runtime requests or old collectors.
        Only future source-verified byte-bounded tool turns can use the receipt;
        the old 200-row limit remains mandatory for every default reservation.
        """
        from arknights_agent.runtime.bounded_profile import STAGE16_PROFILE_ID, validate_receipt

        validate_receipt(receipt)
        with self._transaction() as db:
            status = self._summary(db)
            if status["frozen"]:
                raise BudgetError("campaign_frozen")
            if status["inflight_count"]:
                raise BudgetError("request_inflight")
            if status["price_verification_status"] != "ready":
                raise BudgetError(status["price_verification_status"])
            self._validate_continuation_baseline(db, receipt)
            existing = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='bounded_continuation'"
            ).fetchone()
            if existing:
                rows = db.execute("SELECT * FROM bounded_continuation").fetchall()
                if rows:
                    matching = [row for row in rows
                                if row["continuation_id"] == receipt["continuation_id"]]
                    if matching:
                        if (len(matching) != 1 or json.loads(matching[0]["receipt_json"]) != receipt
                            or matching[0]["campaign_id"] != CAMPAIGN_ID):
                            raise BudgetError("continuation_already_authorized")
                        return {"continuation_id": receipt["continuation_id"],
                                "max_additional_requests": receipt["max_additional_requests"],
                                "reused": True}
                    if (receipt["profile_id"] != STAGE16_PROFILE_ID or len(rows) != 1
                        or rows[0]["continuation_id"] != receipt["parent_continuation_id"]):
                        raise BudgetError("continuation_already_authorized")
            if receipt["profile_id"] == STAGE16_PROFILE_ID and not existing:
                raise BudgetError("continuation_parent_not_authorized")
            if status["reservation_count"] != receipt["baseline_count"]:
                raise BudgetError("continuation_baseline_count_mismatch")
            # Receipt metadata is a separate additive table. Campaign values,
            # existing reservations and existing protocol reviews stay intact.
            db.execute(
                """CREATE TABLE IF NOT EXISTS bounded_continuation (
                continuation_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
                receipt_json TEXT NOT NULL,
                authorized_at TEXT NOT NULL)"""
            )
            db.execute(
                """INSERT INTO bounded_continuation
                (continuation_id,campaign_id,receipt_json,authorized_at) VALUES (?,?,?,?)""",
                (receipt["continuation_id"], CAMPAIGN_ID,
                 json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                 self.clock().astimezone(UTC).isoformat()),
            )
            return {"continuation_id": receipt["continuation_id"],
                    "max_additional_requests": receipt["max_additional_requests"], "reused": False}

    @staticmethod
    def _validate_continuation_baseline(db, receipt):
        from arknights_agent.runtime.bounded_profile import (
            PROFILE_ID,
            STAGE16_PROFILE_ID,
            ledger_tables_hash,
            reservation_rows_hash,
            validate_receipt,
        )

        campaign = db.execute("SELECT * FROM campaign WHERE campaign_id=?", (CAMPAIGN_ID,)).fetchone()
        if any(str(campaign[key]) != receipt[key]
               for key in ("budget_cny", "safety_fraction", "cap_cny")):
            raise BudgetError("continuation_cap_mismatch")
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM reservation WHERE campaign_id=?", (CAMPAIGN_ID,)
        )]
        baseline_ids = set(receipt["baseline_reservation_ids"])
        baseline = [row for row in rows if row["reservation_id"] in baseline_ids]
        if (len(baseline) != receipt["baseline_count"]
            or reservation_rows_hash(baseline) != receipt["baseline_reservation_sha256"]):
            raise BudgetError("continuation_historical_rows_changed")
        snapshot = {"campaign": [dict(campaign)], "reservation": baseline}
        for table in ("campaign_review", "reviewed_reservation"):
            exists = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            snapshot[table] = [dict(row) for row in db.execute(f"SELECT * FROM {table}")] \
                if exists else []
        if receipt["profile_id"] == STAGE16_PROFILE_ID:
            exists = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='bounded_continuation'"
            ).fetchone()
            if not exists:
                raise BudgetError("continuation_parent_not_authorized")
            registrations = [dict(row) for row in db.execute("SELECT * FROM bounded_continuation")]
            parents = [row for row in registrations
                       if row["continuation_id"] != receipt["continuation_id"]]
            if (len(parents) != 1
                or parents[0]["continuation_id"] != receipt["parent_continuation_id"]
                or parents[0]["campaign_id"] != CAMPAIGN_ID):
                raise BudgetError("continuation_parent_invalid")
            parent = validate_receipt(json.loads(parents[0]["receipt_json"]))
            if (parent["profile_id"] != PROFILE_ID
                or parent["continuation_id"] != receipt["parent_continuation_id"]):
                raise BudgetError("continuation_parent_invalid")
            BudgetLedger._validate_continuation_baseline(db, parent)
            parent_ids = set(parent["baseline_reservation_ids"])
            if any(row["state"] != "settled" for row in baseline
                   if row["reservation_id"] not in parent_ids):
                raise BudgetError("continuation_parent_outcome_unresolved")
            snapshot["bounded_continuation"] = parents
        if ledger_tables_hash(snapshot) != receipt["baseline_tables_sha256"]:
            raise BudgetError("continuation_historical_tables_changed")

    def _bounded_request_limit(self, db, bounded_request, continuation_id):
        from arknights_agent.runtime.bounded_profile import BoundedRequest, validate_receipt

        if (not isinstance(bounded_request, BoundedRequest)
            or continuation_id != bounded_request.continuation_id):
            raise BudgetError("bounded_request_proof_invalid")
        bounded_request.validate()
        if not db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bounded_continuation'"
        ).fetchone():
            raise BudgetError("continuation_not_authorized")
        row = db.execute(
            "SELECT * FROM bounded_continuation WHERE continuation_id=? AND campaign_id=?",
            (continuation_id, CAMPAIGN_ID),
        ).fetchone()
        if row is None:
            raise BudgetError("continuation_not_authorized")
        receipt = json.loads(row["receipt_json"])
        validate_receipt(receipt)
        if bounded_request.profile_id != receipt["profile_id"]:
            raise BudgetError("bounded_request_proof_invalid")
        for registration in db.execute("SELECT * FROM bounded_continuation"):
            registered = validate_receipt(json.loads(registration["receipt_json"]))
            if (registration["campaign_id"] != CAMPAIGN_ID
                or registration["continuation_id"] != registered["continuation_id"]):
                raise BudgetError("continuation_parent_invalid")
            if registered.get("parent_continuation_id") == continuation_id:
                raise BudgetError("continuation_superseded")
        self._validate_continuation_baseline(db, receipt)
        from arknights_agent.runtime.request_allowance import request_limit

        return request_limit(db, bounded_request,
                             receipt["baseline_count"] + receipt["max_additional_requests"])

    def reserve(self, input_tokens, output_tokens, input_price, output_price, *,
                bounded_request=None, continuation_id=None) -> str:
        bounded = bounded_request is not None
        if bounded:
            from arknights_agent.runtime.bounded_profile import BOUNDED_INPUT_TOKENS, BoundedRequest

            if (not isinstance(bounded_request, BoundedRequest)
                or type(input_tokens) is not int or input_tokens != BOUNDED_INPUT_TOKENS
                or type(output_tokens) is not int or not 1 <= output_tokens <= MAX_OUTPUT_TOKENS):
                raise BudgetError("reservation_token_bound_invalid")
            bounded_request.validate()
        elif continuation_id is not None:
            raise BudgetError("bounded_request_proof_invalid")
        if (
            type(input_tokens) is not int
            or (not bounded and input_tokens not in {
                RESERVATION_INPUT_TOKENS, CONTEXT_RESERVATION_INPUT_TOKENS,
            })
            or type(output_tokens) is not int
            or not 1 <= output_tokens <= (
                EVALUATION_MAX_OUTPUT_TOKENS
                if input_tokens == CONTEXT_RESERVATION_INPUT_TOKENS
                else MAX_OUTPUT_TOKENS
            )
        ):
            raise BudgetError("reservation_token_bound_invalid")
        input_price = _decimal(input_price, minimum=INPUT_PRICE_FLOOR)
        output_price = _decimal(output_price, minimum=OUTPUT_PRICE_FLOOR)
        reserved = _cost(input_tokens, output_tokens, input_price, output_price)
        with self._transaction() as db:
            status = self._summary(db)
            if status["frozen"]:
                raise BudgetError("campaign_frozen")
            if status["price_verification_status"] != "ready":
                raise BudgetError(status["price_verification_status"])
            if status["inflight_count"]:
                raise BudgetError("request_inflight")
            from arknights_agent.runtime.stage20_interrupted_recovery import guard as recovery_guard
            from arknights_agent.runtime.stage21_public_allowance import guard as stage21_guard
            from arknights_agent.runtime.stage21_public_allowance import record_attempt
            from arknights_agent.runtime.stage22_public_allowance import guard as stage22_guard
            from arknights_agent.runtime.stage22_public_allowance import (
                record_attempt as record_stage22,
            )

            stage22_limit = stage22_guard(db, bounded_request)
            stage21_limit = stage21_guard(db, bounded_request) if stage22_limit is None else None
            recovery_limit = recovery_guard(db, bounded_request) if stage22_limit is None and stage21_limit is None else None
            request_limit = self._bounded_request_limit(db, bounded_request, continuation_id) \
                if bounded else MAX_CAMPAIGN_RESERVATIONS
            if recovery_limit is not None:
                request_limit = max(request_limit, recovery_limit)
            if stage21_limit is not None:
                request_limit = stage21_limit
            if stage22_limit is not None:
                request_limit = stage22_limit
            if status["reservation_count"] >= request_limit:
                raise BudgetError("campaign_request_limit_reached")
            if reserved > Decimal(status["remaining_cny"]):
                raise BudgetError("budget_exhausted")
            ident = uuid4().hex
            db.execute(
                """INSERT INTO reservation
                (reservation_id,campaign_id,input_tokens,output_tokens,input_price,
                output_price,reserved_cny,state) VALUES (?,?,?,?,?,?,?,'reserved')""",
                (
                    ident,
                    CAMPAIGN_ID,
                    input_tokens,
                    output_tokens,
                    str(input_price),
                    str(output_price),
                    _money(reserved),
                ),
            )
            if stage22_limit is not None:
                record_stage22(db, bounded_request, ident)
            else:
                record_attempt(db, bounded_request, ident)
            return ident

    @staticmethod
    def _usage(row, usage):
        if not isinstance(usage, Mapping) or any(key not in usage for key in USAGE_FIELDS):
            return None, "usage_missing"
        counts = {key: usage[key] for key in USAGE_FIELDS}
        if any(type(value) is not int or value < 0 for value in counts.values()):
            return None, "usage_invalid"
        if (
            counts["prompt_tokens"] > row["input_tokens"]
            or counts["completion_tokens"] > row["output_tokens"]
            or counts["total_tokens"] != counts["prompt_tokens"] + counts["completion_tokens"]
            or counts["prompt_cache_hit_tokens"] + counts["prompt_cache_miss_tokens"]
            != counts["prompt_tokens"]
        ):
            return None, "usage_invalid"
        return counts, None

    def _freeze_unknown(self, db, ident, code, reported_usage=None):
        safe_code = (
            code if isinstance(code, str) and code in SAFE_UNKNOWN_CODES else "unknown_failure"
        )
        row = self._reservation(db, ident)
        self._hold_bounded_unknown(db, row, reported_usage)
        db.execute(
            "UPDATE reservation SET state='unknown',error_code=? WHERE reservation_id=?",
            (safe_code, ident),
        )
        if getattr(self, "pause_campaign_on_unknown", True):
            db.execute(
                "UPDATE campaign SET frozen=1,freeze_code=coalesce(freeze_code,?) WHERE campaign_id=?",
                (safe_code, CAMPAIGN_ID),
            )

    @staticmethod
    def _hold_bounded_unknown(db, row, reported_usage):
        """An opaque/bad future response holds the public full-context cost.

        The smaller send-time reservation is a bounded public-template profile,
        not proof of opaque API preprocessing or of an unknown invoice. Only
        new bounded rows are escalated; old accounting remains byte-identical.
        A larger hold can exhaust the cap, in which case freezing prevents any
        further request. No unknown outcome is settled or released here.
        """
        from arknights_agent.runtime.bounded_profile import BOUNDED_INPUT_TOKENS
        from arknights_agent.runtime.stage21_public_allowance import protects_historical_reservation
        from arknights_agent.runtime.stage22_public_allowance import historical_reservation

        protected = historical_reservation(db, row["reservation_id"])
        if protected is True or (protected is None and protects_historical_reservation(db, row["reservation_id"])):
            return False
        if row["input_tokens"] != BOUNDED_INPUT_TOKENS:
            return False
        exists = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bounded_continuation'"
        ).fetchone()
        if not exists:
            return False
        receipts = db.execute(
            "SELECT receipt_json FROM bounded_continuation WHERE campaign_id=?", (CAMPAIGN_ID,)
        ).fetchall()
        if not receipts:
            return False
        if any(row["reservation_id"] in json.loads(item["receipt_json"])["baseline_reservation_ids"]
               for item in receipts):
            return False
        return BudgetLedger._escalate_bounded_unknown(db, row, reported_usage)

    @staticmethod
    def _escalate_bounded_unknown(db, row, reported_usage):
        """Raise an owned bounded attempt's hold; never settle, release or thaw.

        Callers establish ownership first. Historical callers retain their
        original receipt/source checks; current HTTP attempts use their own
        append-only task journal rather than reauthorizing a retired round.
        """
        input_price, output_price = Decimal(row["input_price"]), Decimal(row["output_price"])
        held = max(Decimal(row["reserved_cny"]),
                   _cost(CONTEXT_RESERVATION_INPUT_TOKENS, row["output_tokens"],
                         input_price, output_price))
        counts = None
        if isinstance(reported_usage, Mapping) and all(key in reported_usage for key in USAGE_FIELDS):
            candidate = {key: reported_usage[key] for key in USAGE_FIELDS}
            if (all(type(value) is int and 0 <= value <= 2**63 - 1 for value in candidate.values())
                and candidate["total_tokens"] == candidate["prompt_tokens"] + candidate["completion_tokens"]
                and candidate["prompt_cache_hit_tokens"] + candidate["prompt_cache_miss_tokens"]
                == candidate["prompt_tokens"]):
                counts = candidate
                held = max(held, _cost(counts["prompt_tokens"], counts["completion_tokens"],
                                      input_price, output_price))
                previous = {key: row[key] for key in USAGE_FIELDS}
                if (all(type(value) is int and value >= 0 for value in previous.values())
                    and previous["total_tokens"] == previous["prompt_tokens"] + previous["completion_tokens"]
                    and previous["prompt_cache_hit_tokens"] + previous["prompt_cache_miss_tokens"]
                    == previous["prompt_tokens"]
                    and _cost(previous["prompt_tokens"], previous["completion_tokens"],
                              input_price, output_price)
                    > _cost(counts["prompt_tokens"], counts["completion_tokens"],
                            input_price, output_price)):
                    counts = previous
        if counts is None:
            db.execute("UPDATE reservation SET reserved_cny=? WHERE reservation_id=?",
                       (_money(held), row["reservation_id"]))
        else:
            db.execute(
                """UPDATE reservation SET reserved_cny=?,prompt_tokens=?,completion_tokens=?,
                total_tokens=?,prompt_cache_hit_tokens=?,prompt_cache_miss_tokens=?
                WHERE reservation_id=?""",
                (_money(held), *(counts[key] for key in USAGE_FIELDS), row["reservation_id"]),
            )
        return True

    def settle(self, reservation, usage) -> dict:
        with self._transaction() as db:
            row = self._reservation(db, reservation)
            if row["state"] == "unknown":
                if self._hold_bounded_unknown(db, row, usage):
                    # A late future reply may only raise the hold, never
                    # settle, release or thaw the earlier unknown outcome.
                    if getattr(self, "pause_campaign_on_unknown", True):
                        db.execute(
                            "UPDATE campaign SET frozen=1,freeze_code=coalesce(freeze_code,?) WHERE campaign_id=?",
                            (row["error_code"] or "unknown_failure", CAMPAIGN_ID),
                        )
                    return self._summary(db, reservation)
                raise BudgetError("reservation_outcome_unknown")
            counts, error = self._usage(row, usage)
            if row["state"] == "settled":
                if error or any(row[key] != value for key, value in counts.items()):
                    raise BudgetError("settlement_conflict")
                return self._summary(db, reservation)
            if error:
                self._freeze_unknown(db, reservation, error, reported_usage=usage)
                return self._summary(db, reservation)
            accounted = _cost(
                counts["prompt_tokens"],
                counts["completion_tokens"],
                Decimal(row["input_price"]),
                Decimal(row["output_price"]),
            )
            db.execute(
                """UPDATE reservation SET state='settled',accounted_cny=?,
                prompt_tokens=?,completion_tokens=?,total_tokens=?,prompt_cache_hit_tokens=?,
                prompt_cache_miss_tokens=? WHERE reservation_id=?""",
                (_money(accounted), *(counts[key] for key in USAGE_FIELDS), reservation),
            )
            return self._summary(db, reservation)

    def fail_unknown(self, reservation, code) -> dict:
        with self._transaction() as db:
            row = self._reservation(db, reservation)
            if row["state"] == "reserved":
                self._freeze_unknown(db, reservation, code)
            # Once known, a repeated failure cannot undo a valid settlement.
            # Once unknown, a restart or later response cannot silently thaw it.
            return self._summary(db, reservation)

    def review_interrupted_stage20(self, receipt) -> dict:
        """Register an independent, source-bound review; runtime never calls it."""
        from arknights_agent.runtime.stage20_interrupted_recovery import register

        return register(self, receipt)

    def authorize_stage21_public_plan(self, receipt) -> dict:
        """Append an independently reviewed public plan within the existing cap."""
        from arknights_agent.runtime.stage21_public_allowance import register

        return register(self, receipt)

    def authorize_stage22_public_plan(self, receipt) -> dict:
        """A separate fixed request round; grant no money or historical changes."""
        from arknights_agent.runtime.stage22_public_allowance import register

        return register(self, receipt)

    def authorize_stage22_money_grant(self, receipt) -> dict:
        """Register the human-authorized fixed +4 CNY grant without releasing holds."""
        from arknights_agent.runtime.stage22_money_grant import register

        return register(self, receipt)

    def review_held_unknowns(self, reservation_ids, review_sha256) -> dict:
        """Explicitly reopen a reviewed protocol failure without releasing funds.

        Runtime requests never call this method. The caller must supply the
        digest of an independent review and list every held unknown outcome.
        It records that review, leaves every reservation byte-for-byte intact,
        and clears only the campaign freeze. A subsequent unknown outcome
        freezes again and requires a new review with a new digest.

        The accounting invariant is unchanged: settled costs plus all held
        reservations plus the next reservation must fit inside the original
        cap. This method neither estimates an unknown bill nor settles it.
        """
        if (
            not isinstance(reservation_ids, (list, tuple))
            or not 1 <= len(reservation_ids) <= MAX_CAMPAIGN_RESERVATIONS
            or any(
                not isinstance(ident, str) or re.fullmatch(r"[0-9a-f]{32}", ident) is None
                for ident in reservation_ids
            )
            or len(set(reservation_ids)) != len(reservation_ids)
            or not isinstance(review_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", review_sha256) is None
        ):
            raise BudgetError("review_argument_invalid")
        with self._transaction() as db:
            status = self._summary(db)
            if not status["frozen"]:
                raise BudgetError("campaign_not_frozen")
            if status["inflight_count"]:
                raise BudgetError("request_inflight")
            if status["price_verification_status"] != "ready":
                raise BudgetError(status["price_verification_status"])
            unknowns = db.execute(
                """SELECT reservation_id,error_code FROM reservation
                WHERE campaign_id=? AND state='unknown'""",
                (CAMPAIGN_ID,),
            ).fetchall()
            if set(reservation_ids) != {row["reservation_id"] for row in unknowns}:
                raise BudgetError("review_unknown_set_mismatch")
            if any(row["error_code"] != "invalid_response" for row in unknowns):
                raise BudgetError("review_outcome_not_eligible")
            # Create review metadata only for explicit reviews. Ordinary
            # reservations and runtime status do not create or invoke reviews.
            db.execute(
                """CREATE TABLE IF NOT EXISTS campaign_review (
                review_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
                review_sha256 TEXT NOT NULL UNIQUE,
                reviewed_at TEXT NOT NULL,
                previous_freeze_code TEXT NOT NULL)"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS reviewed_reservation (
                review_id TEXT NOT NULL REFERENCES campaign_review(review_id),
                reservation_id TEXT NOT NULL REFERENCES reservation(reservation_id),
                PRIMARY KEY(review_id,reservation_id))"""
            )
            if db.execute(
                "SELECT 1 FROM campaign_review WHERE review_sha256=?",
                (review_sha256,),
            ).fetchone():
                raise BudgetError("review_digest_reused")
            previously_reviewed = {
                row["reservation_id"] for row in db.execute(
                    """SELECT rr.reservation_id FROM reviewed_reservation rr
                    JOIN campaign_review cr ON cr.review_id=rr.review_id
                    WHERE cr.campaign_id=?""", (CAMPAIGN_ID,)
                )
            }
            if set(reservation_ids) <= previously_reviewed:
                raise BudgetError("review_no_new_unknown")
            review_id = uuid4().hex
            db.execute(
                """INSERT INTO campaign_review
                (review_id,campaign_id,review_sha256,reviewed_at,previous_freeze_code)
                VALUES (?,?,?,?,?)""",
                (review_id, CAMPAIGN_ID, review_sha256,
                 self.clock().astimezone(UTC).isoformat(), status["freeze_code"]),
            )
            db.executemany(
                "INSERT INTO reviewed_reservation (review_id,reservation_id) VALUES (?,?)",
                [(review_id, ident) for ident in sorted(reservation_ids)],
            )
            db.execute(
                "UPDATE campaign SET frozen=0,freeze_code=NULL WHERE campaign_id=?",
                (CAMPAIGN_ID,),
            )
            result = self._summary(db)
            result.update(
                review_id=review_id, review_sha256=review_sha256,
                reviewed_unknown_count=len(reservation_ids),
            )
            return result

    def status(self) -> dict:
        with self._transaction() as db:
            return self._summary(db)

    def authorize_budget_extension(self, receipt) -> dict:
        """Register the new ten-yuan human authorization exactly once.

        Keep the original base cap and all reservation/review rows untouched.
        Only the first registration clears this reviewed freeze; repeated calls
        cannot clear a later failure. Ordinary runtime never invokes this path.
        """
        from arknights_agent.runtime.budget_extension import digest, snapshot, validate

        validate(receipt)
        with self._transaction() as db:
            exists = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='budget_extension'"
            ).fetchone()
            if exists:
                rows = db.execute("SELECT * FROM budget_extension").fetchall()
                if len(rows) != 1 or json.loads(rows[0]["receipt_json"]) != receipt:
                    raise BudgetError("extension_already_registered")
                return {"reused": True, **self._summary(db)}
            if self._price_status() != "ready":
                raise BudgetError(self._price_status())
            if digest(snapshot(db)) != receipt["baseline_sha256"]:
                raise BudgetError("extension_baseline_changed")
            db.execute("""CREATE TABLE budget_extension (
                extension_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
                receipt_json TEXT NOT NULL,
                authorized_at TEXT NOT NULL)""")
            db.execute("INSERT INTO budget_extension VALUES (?,?,?,?)", (
                receipt["extension_id"], CAMPAIGN_ID,
                json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                self.clock().astimezone(UTC).isoformat()))
            db.execute("UPDATE campaign SET frozen=0,freeze_code=NULL WHERE campaign_id=?", (CAMPAIGN_ID,))
            return {"reused": False, **self._summary(db)}
