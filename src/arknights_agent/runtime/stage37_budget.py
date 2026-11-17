"""Current-task twelve-call accounting without the retired spending/scope gates.

The human explicitly waived spending caps, input preregistration and version
freezes for this one development dialogue. This adapter still writes each full
reservation and usage to the existing ledger, counts all historical attempts,
and inherits the original unknown/timeout escalation and freeze behavior.
It never edits an old allowance, cost row, campaign limit or freeze receipt.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from arknights_agent.runtime.bounded_profile import BOUNDED_INPUT_TOKENS, STAGE16_PROFILE_ID
from arknights_agent.runtime.budget import (
    CAMPAIGN_ID,
    INPUT_PRICE_FLOOR,
    MAX_OUTPUT_TOKENS,
    OFFICIAL_PRICE_SOURCE,
    OUTPUT_PRICE_FLOOR,
    USAGE_FIELDS,
    VERIFIED_MODEL,
    BudgetError,
    BudgetLedger,
    _cost,
    _decimal,
    _money,
)
from arknights_agent.runtime.stage26_model_profile import CurrentModelBoundedRequest

ATTEMPT_TABLE = "stage37_request_attempt"
MAX_REQUESTS, MAX_ATTEMPTS = 12, 3
CUMULATIVE_REQUEST_LIMIT, ABSOLUTE_LIMIT = 441, 2476
SEND_HOLD = "0.29736960"


def require(value, code):
    if not value:
        raise BudgetError(code)


def attempt_tables(db):
    """Discover the persisted identities, including retired rounds, without reset."""
    tables = []
    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        name = row[0]
        if not re.fullmatch(r"stage\d+[a-z0-9_]*_request_attempt", name):
            continue
        cols = {r[1] for r in db.execute(f'PRAGMA table_info("{name}")')}
        if {"reservation_id", "public_input_sha256"} <= cols:
            tables.append(name)
    return sorted(tables)


def attempts(db):
    return [
        {**dict(row), "attempt_table": name}
        for name in attempt_tables(db)
        for row in db.execute(f'SELECT * FROM "{name}" ORDER BY reservation_id')
    ]


class Stage37BudgetLedger(BudgetLedger):
    def __init__(self, path, *, model_id, price_verified_at,
                 price_source=OFFICIAL_PRICE_SOURCE, clock=None):
        # Deliberately do not call the retired constructor or increase its cap.
        self.path = Path(path).resolve()
        require(self.path.is_file(), "fixed_ledger_absent")
        require(model_id == VERIFIED_MODEL, "model_not_verified")
        require(price_source == OFFICIAL_PRICE_SOURCE, "price_source_not_verified")
        require(isinstance(price_verified_at, datetime) and price_verified_at.tzinfo is not None,
                "price_verification_invalid")
        self.model_id, self.price_verified_at = model_id, price_verified_at
        self.clock = clock or (lambda: datetime.now(UTC))
        with self._transaction() as db:
            require(db.execute("SELECT 1 FROM campaign WHERE campaign_id=?", (CAMPAIGN_ID,)).fetchone(),
                    "fixed_campaign_absent")

    def _summary(self, db, reservation=None):
        campaign = db.execute("SELECT * FROM campaign WHERE campaign_id=?", (CAMPAIGN_ID,)).fetchone()
        rows = [dict(r) for r in db.execute("SELECT * FROM reservation WHERE campaign_id=?", (CAMPAIGN_ID,))]
        known = sum((Decimal(r["accounted_cny"]) for r in rows if r["state"] == "settled"), Decimal(0))
        held = sum((Decimal(r["reserved_cny"]) for r in rows if r["state"] != "settled"), Decimal(0))
        native = attempts(db)
        current = [r for r in native if r["attempt_table"] == ATTEMPT_TABLE]
        result = {
            "campaign_id": CAMPAIGN_ID,
            "fee_cap_enforced": False, "input_preregistration_required": False,
            "implementation_freeze_required": False,
            "budget_cny": None, "cap_cny": None, "remaining_cny": None,
            "historical_nominal_cap_cny_not_enforced": "32.00000000",
            "historical_safety_cap_cny_not_enforced": "29.35000000",
            "historical_shared_cap_cny_not_enforced": "4.07000000",
            "accounted_cny": _money(known), "reserved_cny": _money(held),
            "reservation_count": len(rows),
            "inflight_count": sum(r["state"] == "reserved" for r in rows),
            "unknown_count": sum(r["state"] == "unknown" for r in rows),
            "settled_count": sum(r["state"] == "settled" for r in rows),
            "frozen": bool(campaign["frozen"]), "freeze_code": campaign["freeze_code"],
            "price_verification_status": self._price_status(),
            "cost_basis": "conservative_peak_price_times1_10_not_invoice",
            "stage37_requests": len(current), "stage37_max_requests": MAX_REQUESTS,
            "conservative_budget_cost_cny": _money(known),
            "unresolved_budget_hold_cny": _money(held), "provider_invoice_cny": None,
            "provider_reported_usage": {k: sum(r[k] for r in rows if r["state"] == "settled") for k in USAGE_FIELDS},
        }
        if reservation is not None:
            row = self._reservation(db, reservation)
            result.update(reservation_id=row["reservation_id"], reservation_state=row["state"],
                          reservation_cny=row["reserved_cny"], reservation_accounted_cny=row["accounted_cny"],
                          reservation_error_code=row["error_code"],
                          usage={k: row[k] for k in USAGE_FIELDS} if row["state"] == "settled" else None)
        return result

    @staticmethod
    def _reservation(db, ident):
        row = BudgetLedger._reservation(db, ident)
        require(ATTEMPT_TABLE in attempt_tables(db) and db.execute(
            f"SELECT 1 FROM {ATTEMPT_TABLE} WHERE reservation_id=?", (ident,)).fetchone(),
            "stage37_cannot_mutate_historical_reservation")
        return row

    def reserve(self, input_tokens, output_tokens, input_price, output_price, *,
                bounded_request=None, continuation_id=None):
        require(isinstance(bounded_request, CurrentModelBoundedRequest)
                and type(input_tokens) is int and input_tokens == BOUNDED_INPUT_TOKENS
                and type(output_tokens) is int and output_tokens == MAX_OUTPUT_TOKENS,
                "reservation_token_bound_invalid")
        bounded_request.validate_current()
        require(bounded_request.continuation_id == continuation_id
                and bounded_request.profile_id == STAGE16_PROFILE_ID, "bounded_request_proof_invalid")
        ip = _decimal(input_price, minimum=INPUT_PRICE_FLOOR)
        op = _decimal(output_price, minimum=OUTPUT_PRICE_FLOOR)
        held = _cost(input_tokens, output_tokens, ip, op)
        require(held == Decimal(SEND_HOLD), "stage37_full_send_hold_required")
        with self._transaction() as db:
            state = self._summary(db)
            require(not state["frozen"], "campaign_frozen")
            require(not state["inflight_count"], "request_inflight")
            require(state["price_verification_status"] == "ready", state["price_verification_status"])
            old = attempts(db)
            identity = bounded_request.public_input_sha256
            require(sum(r["public_input_sha256"] == identity for r in old) < MAX_ATTEMPTS,
                    "stage37_turn_request_limit_reached")
            require(state["stage37_requests"] < MAX_REQUESTS, "stage37_request_limit_reached")
            counted = {r["reservation_id"] for r in old
                       if int(re.match(r"stage(\d+)", r["attempt_table"])[1]) >= 23}
            require(len(counted) < CUMULATIVE_REQUEST_LIMIT
                    and state["reservation_count"] < ABSOLUTE_LIMIT, "campaign_request_limit_reached")
            # Append a call journal at send time; there is no input allowance,
            # review receipt, spending grant, version seal or preregistered list.
            db.execute(f"CREATE TABLE IF NOT EXISTS {ATTEMPT_TABLE} ("
                       "reservation_id TEXT PRIMARY KEY REFERENCES reservation(reservation_id),"
                       "public_input_sha256 TEXT NOT NULL, started_at TEXT NOT NULL)")
            for op_name in ("UPDATE", "DELETE"):
                db.execute(f"CREATE TRIGGER IF NOT EXISTS stage37_attempt_{op_name.lower()} "
                           f"BEFORE {op_name} ON {ATTEMPT_TABLE} BEGIN "
                           "SELECT RAISE(ABORT, 'stage37_attempt_append_only'); END")
            ident = uuid4().hex
            db.execute("INSERT INTO reservation (reservation_id,campaign_id,input_tokens,output_tokens,"
                       "input_price,output_price,reserved_cny,state) VALUES (?,?,?,?,?,?,?,'reserved')",
                       (ident, CAMPAIGN_ID, input_tokens, output_tokens, str(ip), str(op), _money(held)))
            db.execute(f"INSERT INTO {ATTEMPT_TABLE} VALUES (?,?,?)",
                       (ident, identity, self.clock().astimezone(UTC).isoformat()))
            return ident
