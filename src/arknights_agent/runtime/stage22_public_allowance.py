"""A separate reviewed Stage22 request round; no money or historical rewrites.

The existing Stage21 receipts and attempts are historical evidence. This new
round owns a separate append-only counter and takes over future requests only
after explicit registration. Its receipt grants zero additional money.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from arknights_agent.runtime import stage21_public_allowance as prior
from arknights_agent.runtime.bounded_profile import BOUNDED_INPUT_TOKENS, STAGE16_PROFILE_ID
from arknights_agent.runtime.budget import (
    CAMPAIGN_ID,
    INPUT_PRICE_FLOOR,
    MAX_OUTPUT_TOKENS,
    OUTPUT_PRICE_FLOOR,
    USAGE_FIELDS,
    BudgetError,
    _cost,
    _decimal,
    _money,
)
from arknights_agent.runtime.request_allowance import digest, public_identity

TABLE = "stage22_public_allowance"
ATTEMPT_TABLE = "stage22_request_attempt"
BASELINE_COUNT = 1459
BASELINE_ACCOUNTED = "12.89286020"
BASELINE_UNKNOWN_HOLD = "9.04076800"
BASELINE_REMAINING = "0.46637180"
DEVELOPMENT_COUNT = 103
HOLDOUT_COUNT = 37
MAX_ATTEMPTS = 3
MAX_REQUESTS = 420
ABSOLUTE_LIMIT = BASELINE_COUNT + MAX_REQUESTS
AUTHORIZATION = (
    "那就继续修真实 API 测试发现的问题，重点解决参数和指代误读、条件遗漏、工具提交失败。"
    "别补本地口语规则，修完用开发集回归，再建新的保留集复测，结果和费用分开统计，查不到的别编。"
)
REVIEW_TTL = timedelta(minutes=10)
REVIEWED_FILES = (
    "src/arknights_agent/runtime/stage22_public_allowance.py",
    "src/arknights_agent/runtime/budget.py",
    "src/arknights_agent/runtime/request_allowance.py",
    "src/arknights_agent/runtime/budget_extension.py",
    "src/arknights_agent/runtime/charges.py",
    "src/arknights_agent/runtime/stage22_money_grant.py",
    "tests/test_stage22_public_allowance.py",
    "tests/test_stage22_money_grant.py",
)
REVIEW_SAFEGUARDS = (
    "source_gate_retained", "money_cap_unchanged", "historical_rows_immutable",
    "historical_unknown_holds_retained", "prior_attempts_immutable", "no_automatic_retry",
    "fresh_holdout_unseen_until_implementation_freeze", "public_context_only",
)
PROTECTIONS = {
    f"stage22_attempt_no_{operation.lower()}": (
        f"CREATE TRIGGER stage22_attempt_no_{operation.lower()} BEFORE {operation} ON {ATTEMPT_TABLE} "
        "BEGIN SELECT RAISE(ABORT, 'stage22_attempt_append_only'); END"
    ) for operation in ("UPDATE", "DELETE")
}


def _require(condition, code="stage22_allowance_receipt_invalid"):
    if not condition:
        raise BudgetError(code)


def implementation_hashes():
    return {name: hashlib.sha256((prior.SOURCE_ROOT / name).read_bytes()).hexdigest()
            for name in REVIEWED_FILES}


def _triggers(db, table):
    return {r["name"]: r["sql"] for r in db.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
    )}


def snapshot(db):
    value = prior.snapshot(db)
    value[prior.TABLE] = [dict(r) for r in db.execute(f"SELECT * FROM {prior.TABLE} ORDER BY 1")]
    value[prior.ATTEMPT_TABLE] = [dict(r) for r in db.execute(f"SELECT * FROM {prior.ATTEMPT_TABLE} ORDER BY 1")]
    value["stage21_attempt_protections"] = _triggers(db, prior.ATTEMPT_TABLE)
    return value


def review_template(baseline, plan, *, split_evidence_sha256):
    """Unapproved review template, never a claimed human money authorization."""
    return {
        "reviewer": "independent_agent", "passed": False, "authorization_text": AUTHORIZATION,
        "checked_at": None, "baseline_sha256": digest(baseline), "plan_sha256": digest(plan),
        "reviewed_files_sha256": implementation_hashes(), "split_evidence_sha256": split_evidence_sha256,
        **dict.fromkeys(REVIEW_SAFEGUARDS, False),
    }


def build_receipt(baseline, plan, independent_review_path):
    raw = Path(independent_review_path).read_text(encoding="utf-8")
    receipt = {
        "authorization_text": AUTHORIZATION, "campaign_id": CAMPAIGN_ID,
        "baseline": copy.deepcopy(baseline), "baseline_sha256": digest(baseline),
        "baseline_count": BASELINE_COUNT, "plan": copy.deepcopy(plan), "plan_sha256": digest(plan),
        "development_count": DEVELOPMENT_COUNT, "fresh_holdout_count": HOLDOUT_COUNT,
        "max_attempts_per_turn": MAX_ATTEMPTS, "max_additional_requests": MAX_REQUESTS,
        "absolute_limit": ABSOLUTE_LIMIT, "budget_cny": "28.00000000", "cap_cny": "22.40000000",
        "additional_money_cny": "0", "unknown_costs_released": False,
        "independent_review_json": raw, "review_sha256": hashlib.sha256(raw.encode()).hexdigest(),
    }
    receipt["id"] = digest(receipt)
    return validate(receipt)


def validate(receipt):
    try:
        _require(set(receipt) == {
            "authorization_text", "campaign_id", "baseline", "baseline_sha256", "baseline_count",
            "plan", "plan_sha256", "development_count", "fresh_holdout_count", "max_attempts_per_turn",
            "max_additional_requests", "absolute_limit", "budget_cny", "cap_cny", "additional_money_cny",
            "unknown_costs_released", "independent_review_json", "review_sha256", "id",
        })
        b, plan = receipt["baseline"], receipt["plan"]
        _require(set(b) == {
            "campaign", "reservation", "campaign_review", "reviewed_reservation", "bounded_continuation",
            "budget_extension", "request_allowance", "rag_recheck_allowance", "rag_expansion_allowance",
            "stage20_interrupted_recovery", prior.TABLE, prior.ATTEMPT_TABLE, "stage21_attempt_protections",
        })
        _require(len(b[prior.TABLE]) == 1)
        reg = b[prior.TABLE][0]
        old = prior.validate(json.loads(reg["receipt_json"]))
        _require(reg["id"] == old["id"] and reg["review_sha256"] == old["review_sha256"])
        _require(b["stage21_attempt_protections"] == prior.ATTEMPT_PROTECTIONS)
        rows = b["reservation"]
        _require(len(rows) == BASELINE_COUNT and len({r["reservation_id"] for r in rows}) == BASELINE_COUNT)
        _require(len(b["campaign"]) == 1 and b["campaign"][0]["frozen"] == 0
                 and b["campaign"][0]["freeze_code"] is None)
        _require(sum(r["state"] == "settled" for r in rows) == BASELINE_COUNT - 3)
        _require(_money(sum((Decimal(r["accounted_cny"]) for r in rows if r["state"] == "settled"), Decimal(0))) == BASELINE_ACCOUNTED)
        _require(_money(sum((Decimal(r["reserved_cny"]) for r in rows if r["state"] == "unknown"), Decimal(0))) == BASELINE_UNKNOWN_HOLD)
        historical = {k: copy.deepcopy(v) for k, v in b.items()
                      if k not in {prior.TABLE, prior.ATTEMPT_TABLE, "stage21_attempt_protections"}}
        old_ids = {r["reservation_id"] for r in old["baseline"]["reservation"]}
        historical["reservation"] = [r for r in rows if r["reservation_id"] in old_ids]
        _require(digest(historical) == old["baseline_sha256"])
        attempts = b[prior.ATTEMPT_TABLE]
        _require(len(attempts) == BASELINE_COUNT - prior.BASELINE_COUNT == 279)
        _require({r["reservation_id"] for r in attempts} == {r["reservation_id"] for r in rows} - old_ids)
        old_plan_ids = {public_identity(c) for c in old["plan"]}
        _require(all(r["allowance_id"] == old["id"] and r["public_input_sha256"] in old_plan_ids for r in attempts))
        _require(max(Counter(r["public_input_sha256"] for r in attempts).values()) <= 3)
        _require(len(old["plan"]) == DEVELOPMENT_COUNT + HOLDOUT_COUNT == 140)
        _require(isinstance(plan, list) and len(plan) == DEVELOPMENT_COUNT + HOLDOUT_COUNT)
        _require(all(set(c) == {"case_id", "question", "module", "release_id"}
                     and isinstance(c["case_id"], str) and 1 <= len(c["case_id"]) <= 128
                     and prior._public_question(c["question"]) and c["module"] == "auto"
                     and c["release_id"] == prior.RELEASE_ID for c in plan))
        _require(len({c["case_id"] for c in plan}) == 140 and len({public_identity(c) for c in plan}) == 140)
        _require(plan[:DEVELOPMENT_COUNT] == old["plan"][:DEVELOPMENT_COUNT])
        _require(all(c["case_id"].startswith("stage22-holdout-") for c in plan[DEVELOPMENT_COUNT:]))
        _require(not old_plan_ids.intersection(public_identity(c) for c in plan[DEVELOPMENT_COUNT:]))
        static = {
            "authorization_text": AUTHORIZATION, "campaign_id": CAMPAIGN_ID, "baseline_count": BASELINE_COUNT,
            "development_count": DEVELOPMENT_COUNT, "fresh_holdout_count": HOLDOUT_COUNT,
            "max_attempts_per_turn": 3, "max_additional_requests": MAX_REQUESTS, "absolute_limit": ABSOLUTE_LIMIT,
            "budget_cny": "28.00000000", "cap_cny": "22.40000000", "additional_money_cny": "0",
            "unknown_costs_released": False,
        }
        _require(all(receipt[k] == v for k, v in static.items()))
        _require(digest(b) == receipt["baseline_sha256"] and digest(plan) == receipt["plan_sha256"])
        raw = receipt["independent_review_json"]
        _require(hashlib.sha256(raw.encode()).hexdigest() == receipt["review_sha256"])
        review = json.loads(raw)
        _require(set(review) == {"reviewer", "passed", "authorization_text", "checked_at", "baseline_sha256",
                                 "plan_sha256", "reviewed_files_sha256", "split_evidence_sha256", *REVIEW_SAFEGUARDS})
        _require(review["reviewer"] == "independent_agent" and review["passed"] is True)
        _require(review["authorization_text"] == AUTHORIZATION and review["baseline_sha256"] == receipt["baseline_sha256"]
                 and review["plan_sha256"] == receipt["plan_sha256"])
        _require(all(review[k] is True for k in REVIEW_SAFEGUARDS))
        _require(set(review["reviewed_files_sha256"]) == set(REVIEWED_FILES))
        _require(all(prior._hash(v) for v in review["reviewed_files_sha256"].values()))
        _require(prior._hash(review["split_evidence_sha256"]) and datetime.fromisoformat(review["checked_at"]).tzinfo is not None)
        _require(receipt["id"] == digest({k: v for k, v in receipt.items() if k != "id"}))
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        raise BudgetError("stage22_allowance_receipt_invalid") from None
    return receipt


def _registered(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone():
        return None
    rows = db.execute(f"SELECT * FROM {TABLE}").fetchall()
    _require(len(rows) == 1, "stage22_allowance_registration_invalid")
    receipt = validate(json.loads(rows[0]["receipt_json"]))
    _require(rows[0]["id"] == receipt["id"] and rows[0]["review_sha256"] == receipt["review_sha256"], "stage22_allowance_registration_invalid")
    _require(_triggers(db, ATTEMPT_TABLE) == PROTECTIONS, "stage22_allowance_attempt_protection_invalid")
    current = snapshot(db)
    old_ids = {r["reservation_id"] for r in receipt["baseline"]["reservation"]}
    current["reservation"] = [r for r in current["reservation"] if r["reservation_id"] in old_ids]
    for key in ("frozen", "freeze_code"):
        current["campaign"][0][key] = receipt["baseline"]["campaign"][0][key]
    _require(digest(current) == receipt["baseline_sha256"], "stage22_allowance_history_changed")
    _require(implementation_hashes() == json.loads(receipt["independent_review_json"])["reviewed_files_sha256"], "stage22_allowance_implementation_changed")
    return receipt


def historical_reservation(db, reservation_id):
    """None means no Stage22 round; False means a new Stage22 reservation."""
    receipt = _registered(db)
    return None if receipt is None else reservation_id in {
        r["reservation_id"] for r in receipt["baseline"]["reservation"]
    }


def _reservation_shape(row, expected_state):
    """Replay this round's ordinary reservation and known-usage invariants."""
    code = "stage22_allowance_reservation_invalid"
    _require(row is not None and row["campaign_id"] == CAMPAIGN_ID
             and type(row["input_tokens"]) is int and row["input_tokens"] == BOUNDED_INPUT_TOKENS
             and type(row["output_tokens"]) is int and 1 <= row["output_tokens"] <= MAX_OUTPUT_TOKENS
             and row["state"] == expected_state, code)
    ip = _decimal(row["input_price"], minimum=INPUT_PRICE_FLOOR)
    op = _decimal(row["output_price"], minimum=OUTPUT_PRICE_FLOOR)
    _require(row["reserved_cny"] == _money(_cost(row["input_tokens"], row["output_tokens"], ip, op)), code)
    if expected_state == "settled":
        _require(all(type(row[k]) is int and row[k] >= 0 for k in USAGE_FIELDS)
                 and row["prompt_tokens"] <= row["input_tokens"]
                 and row["completion_tokens"] <= row["output_tokens"]
                 and row["prompt_tokens"] == row["prompt_cache_hit_tokens"] + row["prompt_cache_miss_tokens"]
                 and row["total_tokens"] == row["prompt_tokens"] + row["completion_tokens"]
                 and row["accounted_cny"] == _money(_cost(row["prompt_tokens"], row["completion_tokens"], ip, op)), code)


def register(book, receipt):
    validate(receipt)
    with book._transaction() as db:
        status = book._summary(db)
        old = _registered(db)
        if old is not None:
            _require(old == receipt, "stage22_allowance_already_registered")
            return {"id": receipt["id"], "reused": True, **status}
        _require(not status["frozen"], "campaign_frozen")
        _require(status["inflight_count"] == 0, "request_inflight")
        _require(status["price_verification_status"] == "ready", status["price_verification_status"])
        _require(snapshot(db) == receipt["baseline"], "stage22_allowance_baseline_changed")
        _require(status["budget_cny"] == receipt["budget_cny"] and status["cap_cny"] == receipt["cap_cny"]
                 and status["remaining_cny"] == BASELINE_REMAINING, "stage22_allowance_cap_changed")
        review = json.loads(receipt["independent_review_json"])
        age = book.clock().astimezone(UTC) - datetime.fromisoformat(review["checked_at"]).astimezone(UTC)
        _require(timedelta(0) <= age < REVIEW_TTL, "stage22_allowance_review_expired")
        _require(implementation_hashes() == review["reviewed_files_sha256"], "stage22_allowance_implementation_changed")
        db.execute(f"CREATE TABLE {TABLE} (id TEXT PRIMARY KEY, review_sha256 TEXT NOT NULL UNIQUE, receipt_json TEXT NOT NULL, authorized_at TEXT NOT NULL)")
        db.execute(f"CREATE TABLE {ATTEMPT_TABLE} (reservation_id TEXT PRIMARY KEY REFERENCES reservation(reservation_id), allowance_id TEXT NOT NULL REFERENCES {TABLE}(id), public_input_sha256 TEXT NOT NULL)")
        for definition in PROTECTIONS.values():
            db.execute(definition)
        db.execute(f"INSERT INTO {TABLE} VALUES (?,?,?,?)", (receipt["id"], receipt["review_sha256"], json.dumps(receipt, ensure_ascii=False, sort_keys=True), book.clock().astimezone(UTC).isoformat()))
        return {"id": receipt["id"], "reused": False, **book._summary(db)}


def guard(db, bounded_request):
    receipt = _registered(db)
    if receipt is None:
        return None
    _require(bounded_request is not None and bounded_request.profile_id == STAGE16_PROFILE_ID, "stage22_request_outside_plan")
    bounded_request.validate()
    identities = {public_identity(c) for c in receipt["plan"]}
    _require(bounded_request.public_input_sha256 in identities, "stage22_request_outside_plan")
    attempts = [dict(r) for r in db.execute(f"SELECT * FROM {ATTEMPT_TABLE}")]
    old_ids = {r["reservation_id"] for r in receipt["baseline"]["reservation"]}
    new_rows = [dict(r) for r in db.execute("SELECT * FROM reservation")
                if r["reservation_id"] not in old_ids]
    new_ids = {r["reservation_id"] for r in new_rows}
    _require({r["reservation_id"] for r in attempts} == new_ids and all(
        r["allowance_id"] == receipt["id"] and r["public_input_sha256"] in identities for r in attempts
    ), "stage22_allowance_attempt_history_invalid")
    for row in new_rows:
        _reservation_shape(row, "settled")
    _require(sum(r["public_input_sha256"] == bounded_request.public_input_sha256 for r in attempts) < MAX_ATTEMPTS, "stage22_turn_request_limit_reached")
    _require(len(attempts) < MAX_REQUESTS, "campaign_request_limit_reached")
    return ABSOLUTE_LIMIT


def record_attempt(db, bound, reservation_id):
    bound.validate()
    _require(bound.profile_id == STAGE16_PROFILE_ID, "stage22_request_outside_plan")
    receipt = _registered(db)
    _require(receipt is not None and bound.public_input_sha256 in {public_identity(c) for c in receipt["plan"]}, "stage22_request_outside_plan")
    _require(reservation_id not in {r["reservation_id"] for r in receipt["baseline"]["reservation"]}, "stage22_allowance_reservation_invalid")
    row = db.execute("SELECT * FROM reservation WHERE reservation_id=?", (reservation_id,)).fetchone()
    _reservation_shape(row, "reserved")
    _require(db.execute(f"SELECT count(*) FROM {ATTEMPT_TABLE} WHERE public_input_sha256=?",
                        (bound.public_input_sha256,)).fetchone()[0] < MAX_ATTEMPTS,
             "stage22_turn_request_limit_reached")
    db.execute(f"INSERT INTO {ATTEMPT_TABLE} VALUES (?,?,?)", (reservation_id, receipt["id"], bound.public_input_sha256))
