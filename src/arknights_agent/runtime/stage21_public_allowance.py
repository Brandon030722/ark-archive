"""A reviewed, exact Stage21 public dialogue plan within the existing 28 CNY cap.

The previous receipts and all three unknown costs are immutable. Registration
does not thaw a frozen campaign. Each planned question/context has at most three
provider attempts, recorded atomically with the existing cost reservation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from arknights_agent.runtime.bounded_profile import BOUNDED_INPUT_TOKENS, STAGE16_PROFILE_ID
from arknights_agent.runtime.budget import (
    CAMPAIGN_ID,
    INPUT_PRICE_FLOOR,
    OUTPUT_PRICE_FLOOR,
    BudgetError,
    _cost,
    _money,
)
from arknights_agent.runtime.request_allowance import digest, public_identity
from arknights_agent.runtime.stage20_interrupted_recovery import (
    RELEASE_ID,
)
from arknights_agent.runtime.stage20_interrupted_recovery import snapshot as prior_snapshot
from arknights_agent.runtime.stage20_interrupted_recovery import validate as validate_recovery

TABLE = "stage21_public_allowance"
ATTEMPT_TABLE = "stage21_request_attempt"
ATTEMPT_PROTECTIONS = {
    "stage21_attempt_no_update": (
        f"CREATE TRIGGER stage21_attempt_no_update BEFORE UPDATE ON {ATTEMPT_TABLE} "
        "BEGIN SELECT RAISE(ABORT, 'stage21_attempt_append_only'); END"
    ),
    "stage21_attempt_no_delete": (
        f"CREATE TRIGGER stage21_attempt_no_delete BEFORE DELETE ON {ATTEMPT_TABLE} "
        "BEGIN SELECT RAISE(ABORT, 'stage21_attempt_append_only'); END"
    ),
}
AUTHORIZATION = (
    "那就按你说的继续做第21阶段，补口语问法、局内条件和多轮追问，"
    "建立开发集与保留集分开的新题集，做真实模型测试，发现遗漏和引用错误就修，查不到的别编，"
)
BASELINE_COUNT = 1180
BASELINE_ACCOUNTED = "11.45492040"
BASELINE_UNKNOWN_HOLD = "9.04076800"
BASELINE_REMAINING = "1.90431160"
MAX_PLAN_TURNS = 150
MAX_ATTEMPTS_PER_TURN = 3
REVIEW_TTL = timedelta(minutes=10)
REVIEWED_FILES = (
    "src/arknights_agent/runtime/stage21_public_allowance.py",
    "src/arknights_agent/runtime/budget.py",
    "src/arknights_agent/runtime/request_allowance.py",
    "tests/test_stage21_public_allowance.py",
)
SOURCE_ROOT = Path(__file__).resolve().parents[3]
REVIEW_SAFEGUARDS = (
    "source_gate_retained", "money_cap_unchanged", "historical_unknown_holds_retained",
    "no_automatic_retry", "development_holdout_separate", "holdout_frozen_before_development_evaluation",
    "public_context_only",
)


def _require(condition, code="stage21_allowance_receipt_invalid"):
    if not condition:
        raise BudgetError(code)


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def implementation_hashes():
    """Read only the independently reviewed policy and its adversarial tests."""
    try:
        return {name: hashlib.sha256((SOURCE_ROOT / name).read_bytes()).hexdigest()
                for name in REVIEWED_FILES}
    except OSError:
        raise BudgetError("stage21_allowance_implementation_unavailable") from None


def snapshot(db):
    current = prior_snapshot(db)
    current["stage20_interrupted_recovery"] = [
        dict(row) for row in db.execute("SELECT * FROM stage20_interrupted_recovery ORDER BY 1")
    ]
    return current


def _public_question(value):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 8000:
        return False
    # Dialogue binds the current literal question and previous literal user
    # questions. No expected answer, answer rewrite, or evaluator field belongs
    # in the public user envelope. Plain single-turn questions remain literal.
    if value.lstrip().startswith("{"):
        try:
            dialogue = json.loads(value)
            return (set(dialogue) == {"current_question", "previous_questions"}
                    and isinstance(dialogue["current_question"], str)
                    and bool(dialogue["current_question"].strip())
                    and isinstance(dialogue["previous_questions"], list)
                    and 1 <= len(dialogue["previous_questions"]) <= 5
                    and all(isinstance(q, str) and q.strip()
                            for q in dialogue["previous_questions"]))
        except (TypeError, ValueError):
            return False
    return True


def build_receipt(baseline, plan, independent_review_path):
    """Build a receipt after independent review; no writes or provider calls."""
    review_json = Path(independent_review_path).read_text(encoding="utf-8")
    receipt = {
        "authorization_text": AUTHORIZATION,
        "campaign_id": CAMPAIGN_ID,
        "baseline": copy.deepcopy(baseline),
        "baseline_sha256": digest(baseline),
        "baseline_count": BASELINE_COUNT,
        "baseline_accounted_cny": BASELINE_ACCOUNTED,
        "baseline_unknown_hold_cny": BASELINE_UNKNOWN_HOLD,
        "baseline_remaining_cny": BASELINE_REMAINING,
        "plan": copy.deepcopy(plan),
        "plan_sha256": digest(plan),
        "release_id": RELEASE_ID,
        "max_attempts_per_turn": MAX_ATTEMPTS_PER_TURN,
        "max_additional_requests": len(plan) * MAX_ATTEMPTS_PER_TURN,
        "absolute_limit": BASELINE_COUNT + len(plan) * MAX_ATTEMPTS_PER_TURN,
        "additional_money_cny": "0",
        "budget_cny": "28.00000000",
        "cap_cny": "22.40000000",
        "unknown_costs_released": False,
        "independent_review_json": review_json,
        "review_sha256": hashlib.sha256(review_json.encode("utf-8")).hexdigest(),
    }
    receipt["id"] = digest(receipt)
    return validate(receipt)


def validate(receipt):
    try:
        _require(set(receipt) == {
            "authorization_text", "campaign_id", "baseline", "baseline_sha256", "baseline_count",
            "baseline_accounted_cny", "baseline_unknown_hold_cny", "baseline_remaining_cny",
            "plan", "plan_sha256", "release_id", "max_attempts_per_turn", "max_additional_requests",
            "absolute_limit", "additional_money_cny", "budget_cny", "cap_cny",
            "unknown_costs_released", "independent_review_json", "review_sha256", "id",
        })
        b, plan = receipt["baseline"], receipt["plan"]
        _require(set(b) == {
            "campaign", "reservation", "campaign_review", "reviewed_reservation", "bounded_continuation",
            "budget_extension", "request_allowance", "rag_recheck_allowance", "rag_expansion_allowance",
            "stage20_interrupted_recovery",
        })
        _require(len(b["campaign"]) == 1 and len(b["stage20_interrupted_recovery"]) == 1)
        registration = b["stage20_interrupted_recovery"][0]
        old = validate_recovery(json.loads(registration["receipt_json"]))
        _require(registration["id"] == old["id"] and registration["review_sha256"] == old["review_sha256"])
        campaign = b["campaign"][0]
        _require(campaign["campaign_id"] == CAMPAIGN_ID and campaign["frozen"] == 0)
        _require(campaign["freeze_code"] is None and campaign["budget_cny"] == "18")
        _require(campaign["cap_cny"] == "14.40000000" and campaign["safety_fraction"] == "0.8")
        rows = b["reservation"]
        _require(len(rows) == BASELINE_COUNT and len({r["reservation_id"] for r in rows}) == BASELINE_COUNT)
        _require(sum(row["state"] == "settled" for row in rows) == BASELINE_COUNT - 3)
        unknown = {r["reservation_id"]: r for r in rows if r["state"] == "unknown"}
        prior_unknown = {r["reservation_id"]: r for r in old["baseline"]["reservation"] if r["state"] == "unknown"}
        _require(unknown == prior_unknown and len(unknown) == 3)
        _require(_money(sum((Decimal(r["reserved_cny"]) for r in unknown.values()), Decimal(0))) == BASELINE_UNKNOWN_HOLD)
        _require(_money(sum((Decimal(r["accounted_cny"]) for r in rows if r["state"] == "settled"), Decimal(0))) == BASELINE_ACCOUNTED)
        historical = copy.deepcopy(b)
        del historical["stage20_interrupted_recovery"]
        prior_ids = {r["reservation_id"] for r in old["baseline"]["reservation"]}
        historical["reservation"] = [r for r in historical["reservation"] if r["reservation_id"] in prior_ids]
        for key in ("frozen", "freeze_code"):
            historical["campaign"][0][key] = old["baseline"]["campaign"][0][key]
        _require(digest(historical) == old["baseline_sha256"])
        _require(receipt["baseline_count"] == BASELINE_COUNT)
        _require(receipt["baseline_accounted_cny"] == BASELINE_ACCOUNTED)
        _require(receipt["baseline_unknown_hold_cny"] == BASELINE_UNKNOWN_HOLD)
        _require(receipt["baseline_remaining_cny"] == BASELINE_REMAINING)
        _require(isinstance(plan, list) and 1 <= len(plan) <= MAX_PLAN_TURNS)
        _require(all(set(c) == {"case_id", "question", "module", "release_id"}
                     and isinstance(c["case_id"], str) and c["case_id"].startswith("stage21-")
                     and _public_question(c["question"]) and c["module"] == "auto"
                     and c["release_id"] == RELEASE_ID for c in plan))
        _require(len({c["case_id"] for c in plan}) == len(plan))
        _require(len({public_identity(c) for c in plan}) == len(plan))
        prior_identities = {public_identity(c) for c in old["pending_plan"]}
        _require(not prior_identities.intersection(public_identity(c) for c in plan))
        _require(receipt["release_id"] == RELEASE_ID and receipt["authorization_text"] == AUTHORIZATION)
        _require(receipt["campaign_id"] == CAMPAIGN_ID and receipt["additional_money_cny"] == "0")
        _require(receipt["budget_cny"] == "28.00000000" and receipt["cap_cny"] == "22.40000000")
        _require(receipt["max_attempts_per_turn"] == MAX_ATTEMPTS_PER_TURN)
        _require(receipt["max_additional_requests"] == MAX_ATTEMPTS_PER_TURN * len(plan))
        _require(receipt["absolute_limit"] == BASELINE_COUNT + receipt["max_additional_requests"])
        _require(receipt["unknown_costs_released"] is False)
        _require(receipt["baseline_sha256"] == digest(b) and receipt["plan_sha256"] == digest(plan))
        raw = receipt["independent_review_json"]
        _require(hashlib.sha256(raw.encode("utf-8")).hexdigest() == receipt["review_sha256"])
        review = json.loads(raw)
        _require(set(review) == {
            "reviewer", "passed", "authorization_text", "checked_at", "plan_sha256",
            "baseline_sha256", "release_id", "reviewed_files_sha256", "per_turn_request_limit",
            "split_evidence_sha256", *REVIEW_SAFEGUARDS,
        })
        _require(review["reviewer"] == "independent_agent" and review["passed"] is True)
        _require(review["authorization_text"] == AUTHORIZATION)
        _require(review["plan_sha256"] == receipt["plan_sha256"])
        _require(review["baseline_sha256"] == receipt["baseline_sha256"])
        _require(review["release_id"] == RELEASE_ID and review["per_turn_request_limit"] == 3)
        _require(all(review[key] is True for key in REVIEW_SAFEGUARDS))
        _require(set(review["reviewed_files_sha256"]) == set(REVIEWED_FILES))
        _require(all(_hash(value) for value in review["reviewed_files_sha256"].values()))
        _require(_hash(review["split_evidence_sha256"]))
        _require(datetime.fromisoformat(review["checked_at"]).tzinfo is not None)
        _require(receipt["id"] == digest({k: v for k, v in receipt.items() if k != "id"}))
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        raise BudgetError("stage21_allowance_receipt_invalid") from None
    return receipt


def verify_history(db, receipt):
    current = snapshot(db)
    ids = {r["reservation_id"] for r in receipt["baseline"]["reservation"]}
    current["reservation"] = [r for r in current["reservation"] if r["reservation_id"] in ids]
    for key in ("frozen", "freeze_code"):
        current["campaign"][0][key] = receipt["baseline"]["campaign"][0][key]
    _require(digest(current) == receipt["baseline_sha256"], "stage21_allowance_history_changed")


def _verify_attempt_protections(db):
    """Quota identities are append-only; missing or replaced protections stop use."""
    actual = {
        row["name"]: row["sql"]
        for row in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
            (ATTEMPT_TABLE,),
        )
    }
    _require(actual == ATTEMPT_PROTECTIONS, "stage21_allowance_attempt_protection_invalid")


def _registered(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone():
        return None
    rows = db.execute(f"SELECT * FROM {TABLE}").fetchall()
    _require(len(rows) == 1, "stage21_allowance_registration_invalid")
    receipt = validate(json.loads(rows[0]["receipt_json"]))
    _require(rows[0]["id"] == receipt["id"] and rows[0]["review_sha256"] == receipt["review_sha256"],
             "stage21_allowance_registration_invalid")
    _verify_attempt_protections(db)
    verify_history(db, receipt)
    review = json.loads(receipt["independent_review_json"])
    _require(implementation_hashes() == review["reviewed_files_sha256"], "stage21_allowance_implementation_changed")
    return receipt


def protects_historical_reservation(db, reservation_id):
    """A late provider reply cannot rewrite any reviewed historical unknown row."""
    receipt = _registered(db)
    return receipt is not None and reservation_id in {
        row["reservation_id"] for row in receipt["baseline"]["reservation"]
    }


def register(book, receipt):
    """Register only the reviewed plan; never thaw or release unknown outcomes."""
    validate(receipt)
    with book._transaction() as db:
        status = book._summary(db)
        existing = _registered(db)
        if existing is not None:
            _require(existing == receipt, "stage21_allowance_already_registered")
            return {"id": receipt["id"], "reused": True, **status}
        _require(not status["frozen"], "campaign_frozen")
        _require(status["inflight_count"] == 0, "request_inflight")
        _require(status["price_verification_status"] == "ready", status["price_verification_status"])
        _require(status["reservation_count"] == BASELINE_COUNT, "stage21_allowance_baseline_changed")
        _require(snapshot(db) == receipt["baseline"], "stage21_allowance_baseline_changed")
        _require(status["budget_cny"] == receipt["budget_cny"] and status["cap_cny"] == receipt["cap_cny"], "stage21_allowance_cap_changed")
        _require(status["accounted_cny"] == BASELINE_ACCOUNTED and status["reserved_cny"] == BASELINE_UNKNOWN_HOLD
                 and status["remaining_cny"] == BASELINE_REMAINING, "stage21_allowance_baseline_changed")
        review = json.loads(receipt["independent_review_json"])
        checked = datetime.fromisoformat(review["checked_at"])
        age = book.clock().astimezone(UTC) - checked.astimezone(UTC)
        _require(timedelta(0) <= age < REVIEW_TTL, "stage21_allowance_review_expired")
        _require(implementation_hashes() == review["reviewed_files_sha256"], "stage21_allowance_implementation_changed")
        _require(Decimal(status["remaining_cny"]) >= _cost(BOUNDED_INPUT_TOKENS, 1024, INPUT_PRICE_FLOOR, OUTPUT_PRICE_FLOOR), "budget_exhausted")
        db.execute(f"CREATE TABLE {TABLE} (id TEXT PRIMARY KEY, review_sha256 TEXT NOT NULL UNIQUE, receipt_json TEXT NOT NULL, authorized_at TEXT NOT NULL)")
        db.execute(f"CREATE TABLE {ATTEMPT_TABLE} (reservation_id TEXT PRIMARY KEY REFERENCES reservation(reservation_id), allowance_id TEXT NOT NULL REFERENCES {TABLE}(id), public_input_sha256 TEXT NOT NULL)")
        for definition in ATTEMPT_PROTECTIONS.values():
            db.execute(definition)
        _verify_attempt_protections(db)
        db.execute(f"INSERT INTO {TABLE} VALUES (?,?,?,?)", (
            receipt["id"], receipt["review_sha256"], json.dumps(receipt, ensure_ascii=False, sort_keys=True),
            book.clock().astimezone(UTC).isoformat(),
        ))
        return {"id": receipt["id"], "reused": False, **book._summary(db)}


def guard(db, bounded_request):
    """Return a limit only for registered identities; old Stage20 gates still run."""
    receipt = _registered(db)
    if receipt is None:
        return None
    identities = {public_identity(c) for c in receipt["plan"]}
    old = json.loads(receipt["baseline"]["stage20_interrupted_recovery"][0]["receipt_json"])
    allowed = identities | {public_identity(c) for c in old["pending_plan"]}
    attempts = db.execute(f"SELECT * FROM {ATTEMPT_TABLE} ORDER BY reservation_id").fetchall()
    prior_ids = {r["reservation_id"] for r in receipt["baseline"]["reservation"]}
    current_ids = {r[0] for r in db.execute("SELECT reservation_id FROM reservation")} - prior_ids
    _require({r["reservation_id"] for r in attempts} == current_ids,
             "stage21_allowance_attempt_history_invalid")
    _require(all(r["allowance_id"] == receipt["id"] and r["public_input_sha256"] in allowed for r in attempts),
             "stage21_allowance_attempt_history_invalid")
    if bounded_request is None:
        return None
    bounded_request.validate()
    if bounded_request.public_input_sha256 not in identities:
        return None
    _require(bounded_request.profile_id == STAGE16_PROFILE_ID, "stage21_allowance_profile_invalid")
    count = sum(r["public_input_sha256"] == bounded_request.public_input_sha256 for r in attempts)
    _require(count < MAX_ATTEMPTS_PER_TURN, "stage21_turn_request_limit_reached")
    total = db.execute("SELECT count(*) FROM reservation").fetchone()[0]
    _require(total < receipt["absolute_limit"], "campaign_request_limit_reached")
    return receipt["absolute_limit"]


def record_attempt(db, bounded_request, reservation_id):
    """Track all later bounded rows atomically, preserving old Stage20 scope."""
    receipt = _registered(db)
    if receipt is None:
        return
    old = json.loads(receipt["baseline"]["stage20_interrupted_recovery"][0]["receipt_json"])
    allowed = {public_identity(c) for c in receipt["plan"] + old["pending_plan"]}
    _require(bounded_request is not None and bounded_request.public_input_sha256 in allowed,
             "stage21_allowance_request_outside_plan")
    db.execute(f"INSERT INTO {ATTEMPT_TABLE} VALUES (?,?,?)", (
        reservation_id, receipt["id"], bounded_request.public_input_sha256,
    ))
