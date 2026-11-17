"""One human-authorized Stage22 +4 CNY grant, with all old holds retained.

The fixed 140-turn request plan must be registered first. This grant cannot
alter its attempts, source gate, reservation math, or historical ledger rows.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from arknights_agent.runtime import stage22_public_allowance as allowance
from arknights_agent.runtime.budget import CAMPAIGN_ID, BudgetError
from arknights_agent.runtime.request_allowance import digest

TABLE = "stage22_money_grant"
HUMAN_QUESTION = (
    "是否允许将累计测试预算上限从 28 元提高到 32 元（增加 4 元许可，不充值），"
    "以完成 103 轮开发集回归和 37 轮新保留集真实 API 复测？旧未知挂账保持，"
    "预计扣费须按实际用量另行统计。"
)
HUMAN_ANSWER = "允许提高到 32 元，继续完整复测"
QUESTION_ITEM_ID = '["request_user_input_async","call_0eddd3560c6d407fbc0da150cc225004",0]'
REVIEW_TTL = timedelta(minutes=10)
REVIEW_SAFEGUARDS = (
    "human_question_and_answer_verified", "fixed_plan_and_split_verified",
    "historical_rows_and_receipts_immutable", "historical_unknown_holds_retained",
    "prior_attempts_immutable", "source_gate_retained", "reservation_math_unchanged",
    "request_limits_unchanged", "no_recharge", "no_thaw_or_retry",
)
PROTECTIONS = {
    f"stage22_money_grant_no_{op.lower()}": (
        f"CREATE TRIGGER stage22_money_grant_no_{op.lower()} BEFORE {op} ON {TABLE} "
        "BEGIN SELECT RAISE(ABORT, 'stage22_money_grant_append_only'); END"
    ) for op in ("UPDATE", "DELETE")
}
STATIC = {
    "campaign_id": CAMPAIGN_ID, "additional_budget_cny": "4.00000000",
    "additional_cap_cny": "3.20000000", "budget_cny": "32.00000000",
    "cap_cny": "25.60000000", "unknown_costs_released": False,
    "recharge_authorized": False,
}


def _require(condition, code="stage22_money_grant_receipt_invalid"):
    if not condition:
        raise BudgetError(code)


def validate_human_authorization(value):
    _require(isinstance(value, dict) and set(value) == {
        "received_and_recorded_at_utc", "human_question", "human_answer", "question_item_id",
        "source", "nominal_cap_cny", "additional_nominal_cny", "effective_cap_cny",
        "old_unknowns_retained", "recharge_authorized",
    })
    expected = {
        "human_question": HUMAN_QUESTION, "human_answer": HUMAN_ANSWER,
        "question_item_id": QUESTION_ITEM_ID, "source": "direct_user_question_reply_in_current_chat",
        "nominal_cap_cny": "32", "additional_nominal_cny": "4",
        "effective_cap_cny": "25.60000000", "old_unknowns_retained": True,
        "recharge_authorized": False,
    }
    _require(all(value[k] == v and type(value[k]) is type(v) for k, v in expected.items()))
    _require(datetime.fromisoformat(value["received_and_recorded_at_utc"]).tzinfo is not None)
    return value


def snapshot(db):
    baseline = allowance.snapshot(db)
    for name in (allowance.TABLE, allowance.ATTEMPT_TABLE):
        baseline[name] = [dict(r) for r in db.execute(f"SELECT * FROM {name} ORDER BY 1")]
    baseline["stage22_attempt_protections"] = allowance._triggers(db, allowance.ATTEMPT_TABLE)
    return baseline


def review_template(baseline, human_authorization):
    """Unsigned template; a separate reviewer must actually verify every item."""
    return {
        "reviewer": "independent_agent", "passed": False, "checked_at": None,
        "baseline_sha256": digest(baseline),
        "human_authorization_sha256": digest(human_authorization),
        "allowance_id": json.loads(baseline[allowance.TABLE][0]["receipt_json"])["id"],
        "plan_sha256": json.loads(baseline[allowance.TABLE][0]["receipt_json"])["plan_sha256"],
        "reviewed_files_sha256": allowance.implementation_hashes(),
        **dict.fromkeys(REVIEW_SAFEGUARDS, False),
    }


def build_receipt(baseline, human_authorization, independent_review_path):
    raw = Path(independent_review_path).read_text(encoding="utf-8")
    reg = json.loads(baseline[allowance.TABLE][0]["receipt_json"])
    receipt = {
        **STATIC, "baseline": copy.deepcopy(baseline), "baseline_sha256": digest(baseline),
        "human_authorization": copy.deepcopy(human_authorization),
        "human_authorization_sha256": digest(human_authorization),
        "allowance_id": reg["id"], "plan_sha256": reg["plan_sha256"],
        "independent_review_json": raw, "review_sha256": hashlib.sha256(raw.encode()).hexdigest(),
    }
    receipt["id"] = digest(receipt)
    return validate(receipt)


def validate(receipt):
    try:
        _require(set(receipt) == {*STATIC, "baseline", "baseline_sha256", "human_authorization",
                                 "human_authorization_sha256", "allowance_id", "plan_sha256",
                                 "independent_review_json", "review_sha256", "id"})
        _require(all(receipt[k] == v and type(receipt[k]) is type(v) for k, v in STATIC.items()))
        b = receipt["baseline"]
        _require(len(b[allowance.TABLE]) == 1 and b[allowance.ATTEMPT_TABLE] == [])
        row = b[allowance.TABLE][0]
        reg = allowance.validate(json.loads(row["receipt_json"]))
        _require(row["id"] == reg["id"] == receipt["allowance_id"]
                 and row["review_sha256"] == reg["review_sha256"]
                 and reg["plan_sha256"] == receipt["plan_sha256"])
        _require(b["stage22_attempt_protections"] == allowance.PROTECTIONS)
        old = {k: v for k, v in b.items() if k not in {
            allowance.TABLE, allowance.ATTEMPT_TABLE, "stage22_attempt_protections",
        }}
        _require(old == reg["baseline"] and digest(b) == receipt["baseline_sha256"])
        human = validate_human_authorization(receipt["human_authorization"])
        _require(digest(human) == receipt["human_authorization_sha256"])
        raw = receipt["independent_review_json"]
        _require(hashlib.sha256(raw.encode()).hexdigest() == receipt["review_sha256"])
        review = json.loads(raw)
        _require(set(review) == {"reviewer", "passed", "checked_at", "baseline_sha256",
                                 "human_authorization_sha256", "allowance_id", "plan_sha256",
                                 "reviewed_files_sha256", *REVIEW_SAFEGUARDS})
        _require(review["reviewer"] == "independent_agent" and review["passed"] is True)
        _require(all(review[k] is True for k in REVIEW_SAFEGUARDS))
        _require(all(review[k] == receipt[k] for k in (
            "baseline_sha256", "human_authorization_sha256", "allowance_id", "plan_sha256",
        )))
        hashes = review["reviewed_files_sha256"]
        _require(set(hashes) == set(allowance.REVIEWED_FILES)
                 and all(allowance.prior._hash(v) for v in hashes.values()))
        checked_at = datetime.fromisoformat(review["checked_at"])
        _require(checked_at.tzinfo is not None and checked_at >= datetime.fromisoformat(
            human["received_and_recorded_at_utc"]))
        _require(receipt["id"] == digest({k: v for k, v in receipt.items() if k != "id"}))
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        raise BudgetError("stage22_money_grant_receipt_invalid") from None
    return receipt


def _registered(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone():
        return None
    rows = db.execute(f"SELECT * FROM {TABLE}").fetchall()
    _require(len(rows) == 1, "stage22_money_grant_registration_invalid")
    row = rows[0]
    receipt = validate(json.loads(row["receipt_json"]))
    _require(row["id"] == receipt["id"] and row["review_sha256"] == receipt["review_sha256"],
             "stage22_money_grant_registration_invalid")
    _require(allowance._triggers(db, TABLE) == PROTECTIONS,
             "stage22_money_grant_protection_invalid")
    reg = allowance._registered(db)
    _require(reg is not None and reg["id"] == receipt["allowance_id"],
             "stage22_money_grant_plan_changed")
    current = snapshot(db)
    old_ids = {r["reservation_id"] for r in receipt["baseline"]["reservation"]}
    current["reservation"] = [r for r in current["reservation"] if r["reservation_id"] in old_ids]
    # New attempts are checked by the fixed Stage22 guard; they are not old history.
    current[allowance.ATTEMPT_TABLE] = []
    for key in ("frozen", "freeze_code"):
        current["campaign"][0][key] = receipt["baseline"]["campaign"][0][key]
    _require(current == receipt["baseline"], "stage22_money_grant_history_changed")
    review = json.loads(receipt["independent_review_json"])
    _require(allowance.implementation_hashes() == review["reviewed_files_sha256"],
             "stage22_money_grant_implementation_changed")
    return receipt


def effective_limits(db, budget, cap):
    receipt = _registered(db)
    if receipt is None:
        return budget, cap
    _require(budget == Decimal("28") and cap == Decimal("22.4"),
             "stage22_money_grant_parent_cap_changed")
    return budget + Decimal(receipt["additional_budget_cny"]), cap + Decimal(receipt["additional_cap_cny"])


def register(book, receipt):
    validate(receipt)
    with book._transaction() as db:
        status = book._summary(db)
        old = _registered(db)
        if old is not None:
            _require(old == receipt, "stage22_money_grant_already_registered")
            return {"id": receipt["id"], "reused": True, **status}
        _require(not status["frozen"], "campaign_frozen")
        _require(status["inflight_count"] == 0, "request_inflight")
        _require(status["price_verification_status"] == "ready", status["price_verification_status"])
        _require(allowance._registered(db) is not None, "stage22_money_grant_plan_required")
        _require(snapshot(db) == receipt["baseline"], "stage22_money_grant_baseline_changed")
        _require(status["budget_cny"] == "28.00000000" and status["cap_cny"] == "22.40000000"
                 and status["remaining_cny"] == allowance.BASELINE_REMAINING,
                 "stage22_money_grant_parent_cap_changed")
        review = json.loads(receipt["independent_review_json"])
        age = book.clock().astimezone(UTC) - datetime.fromisoformat(review["checked_at"]).astimezone(UTC)
        _require(timedelta(0) <= age < REVIEW_TTL, "stage22_money_grant_review_expired")
        _require(allowance.implementation_hashes() == review["reviewed_files_sha256"],
                 "stage22_money_grant_implementation_changed")
        db.execute(f"CREATE TABLE {TABLE} (id TEXT PRIMARY KEY, review_sha256 TEXT NOT NULL UNIQUE, receipt_json TEXT NOT NULL, authorized_at TEXT NOT NULL)")
        for definition in PROTECTIONS.values():
            db.execute(definition)
        db.execute(f"INSERT INTO {TABLE} VALUES (?,?,?,?)", (
            receipt["id"], receipt["review_sha256"], json.dumps(receipt, ensure_ascii=False, sort_keys=True),
            book.clock().astimezone(UTC).isoformat(),
        ))
        return {"id": receipt["id"], "reused": False, **book._summary(db)}
