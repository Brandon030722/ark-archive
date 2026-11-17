"""One reviewed Stage20 orphan recovery; preserve every cost and prior gate.

This is never called by a runtime failure handler. A human-authorized resumed
collection needs a separate independent review of the quiescent collector. The
single interrupted request remains an unknown full-context hold permanently.
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
    CONTEXT_RESERVATION_INPUT_TOKENS,
    INPUT_PRICE_FLOOR,
    OUTPUT_PRICE_FLOOR,
    USAGE_FIELDS,
    BudgetError,
    _cost,
    _money,
)
from arknights_agent.runtime.rag_expansion_allowance import snapshot as prior_snapshot
from arknights_agent.runtime.rag_expansion_allowance import validate as validate_expansion
from arknights_agent.runtime.request_allowance import digest, public_identity

TABLE = "stage20_interrupted_recovery"
AUTHORIZATION = "阅读这个对话，继续"
SOURCE_THREAD = "codex://threads/01a12ea9-8e2b-7bf1-bc55-72ba7db1f643"
ORPHAN_ID = "643b829283d349ecb42a6300eca8aefa"
ORPHAN_CASE = "stage20-rogue_2-probability-062"
RELEASE_ID = "5a6aaa0fa6d24d2ea5bf5f9c2d590fff"
COLLECTOR_NAME = "stage20-live-repair"
BASELINE_COUNT = 1075
ORIGINAL_ABSOLUTE_LIMIT = 1176
MAX_ADDITIONAL_REQUESTS = 156
ABSOLUTE_LIMIT = BASELINE_COUNT + MAX_ADDITIONAL_REQUESTS
REPAIR_RECHECK_IDS = (
    "stage20-rogue_1-event-048", "stage20-rogue_1-event-049", "stage20-rogue_1-event-050",
    "stage20-rogue_2-event-059", "stage20-rogue_2-event-060", "stage20-rogue_2-event-061",
)
COMPLETED_COUNT = 61
FILES = ("protocol.json", "report.json", "model-turns.jsonl", "results.jsonl", "ledger-before.json")
REVIEW_TTL = timedelta(minutes=10)


def snapshot(db):
    current = prior_snapshot(db)
    current["rag_expansion_allowance"] = [
        dict(row) for row in db.execute("SELECT * FROM rag_expansion_allowance ORDER BY 1")
    ]
    return current


def _require(condition, code="stage20_recovery_receipt_invalid"):
    if not condition:
        raise BudgetError(code)


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def inspect_collector(directory):
    """Read exact interrupted artifacts; no ledger writes, credentials or HTTP."""
    directory = Path(directory)
    try:
        _require(directory.name == COLLECTOR_NAME, "stage20_recovery_collector_invalid")
        contents = {name: (directory / name).read_bytes() for name in FILES}
        protocol = json.loads(contents["protocol.json"])
        report = json.loads(contents["report.json"])
        turns = [json.loads(line) for line in contents["model-turns.jsonl"].splitlines()]
        results = [json.loads(line) for line in contents["results.jsonl"].splitlines()]
        plan = protocol["plan"]
        orphan = [row for row in turns if row.get("reservation_id") == ORPHAN_ID]
        _require(len(orphan) == 1 and orphan[0]["phase"] == "started")
        started = orphan[0]
        _require(started == turns[-1] and started["sequence"] == 125)
        _require(started["case_id"] == ORPHAN_CASE)
        _require(len(plan) == 107 and plan[COMPLETED_COUNT]["case_id"] == ORPHAN_CASE)
        _require(report["collector_complete"] is False and len(report["cases"]) == COMPLETED_COUNT)
        _require([row["case_id"] for row in results] == [c["case_id"] for c in plan[:COMPLETED_COUNT]])
        _require(all(row["model_submitted"] is True and row["baseline_equal"] is True for row in results))
        _require([row["case_id"] for row in report["cases"]] == [row["case_id"] for row in results])
        request = started["public_request"]
        public = next(json.loads(message["content"]) for message in request["messages"] if message["role"] == "user")
        _require(public == {key: plan[COMPLETED_COUNT][key] for key in ("question", "module", "release_id")})
        _require(request["model"] == "deepseek-flash" and request["max_tokens"] == 1024)
        _require(request["stream"] is False)
        return {
            "collector_name": COLLECTOR_NAME,
            "files_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()},
            "plan_sha256": digest(plan),
            "started_row_sha256": digest(started),
            "orphan_reservation_id": ORPHAN_ID,
            "orphan_case_id": ORPHAN_CASE,
            "last_sequence": 125,
            "completed_count": COMPLETED_COUNT,
            "collector_complete": False,
            "orphan_started_without_outcome": True,
        }
    except (OSError, KeyError, TypeError, ValueError, IndexError, StopIteration):
        raise BudgetError("stage20_recovery_collector_invalid") from None


def build_receipt(baseline, collector_directory, independent_review_path):
    """Bind a frozen full-ledger snapshot to independently reviewed artifacts."""
    proof = inspect_collector(collector_directory)
    review_json = Path(independent_review_path).read_text(encoding="utf-8")
    expansion = json.loads(baseline["rag_expansion_allowance"][0]["receipt_json"])
    pending = [c for c in expansion["plan"][:COMPLETED_COUNT] if c["case_id"] in REPAIR_RECHECK_IDS]
    pending += expansion["plan"][COMPLETED_COUNT:]
    receipt = {
        "authorization_text": AUTHORIZATION,
        "source_thread": SOURCE_THREAD,
        "baseline": copy.deepcopy(baseline),
        "baseline_sha256": digest(baseline),
        "collector_proof": proof,
        "independent_review_json": review_json,
        "review_sha256": hashlib.sha256(review_json.encode()).hexdigest(),
        "orphan_reservation_id": ORPHAN_ID,
        "additional_money_cny": "0",
        "budget_cny": "28.00000000",
        "cap_cny": "22.40000000",
        "original_absolute_limit": ORIGINAL_ABSOLUTE_LIMIT,
        "pending_plan": copy.deepcopy(pending),
        "max_additional_requests": MAX_ADDITIONAL_REQUESTS,
        "absolute_limit": ABSOLUTE_LIMIT,
        "unknown_costs_released": False,
    }
    receipt["id"] = digest(receipt)
    return validate(receipt)


def validate(receipt):
    try:
        _require(set(receipt) == {
            "authorization_text", "source_thread", "baseline", "baseline_sha256", "collector_proof",
            "independent_review_json", "review_sha256", "orphan_reservation_id", "additional_money_cny",
            "budget_cny", "cap_cny", "original_absolute_limit", "pending_plan", "max_additional_requests",
            "absolute_limit", "unknown_costs_released", "id",
        })
        b, proof = receipt["baseline"], receipt["collector_proof"]
        _require(set(b) == {
            "campaign", "reservation", "campaign_review", "reviewed_reservation", "bounded_continuation",
            "budget_extension", "request_allowance", "rag_recheck_allowance", "rag_expansion_allowance",
        })
        _require(len(b["campaign"]) == 1 and len(b["rag_expansion_allowance"]) == 1)
        expansion = validate_expansion(json.loads(b["rag_expansion_allowance"][0]["receipt_json"]))
        _require(b["rag_expansion_allowance"][0]["id"] == expansion["id"])
        plan = expansion["plan"]
        _require(len(plan) == 107 and expansion["baseline_count"] == 855)
        _require(expansion["absolute_limit"] == ORIGINAL_ABSOLUTE_LIMIT)
        _require({c["release_id"] for c in plan} == {RELEASE_ID})
        _require(plan[COMPLETED_COUNT]["case_id"] == ORPHAN_CASE)
        expected_pending = [c for c in plan[:COMPLETED_COUNT] if c["case_id"] in REPAIR_RECHECK_IDS]
        _require([c["case_id"] for c in expected_pending] == list(REPAIR_RECHECK_IDS))
        expected_pending += plan[COMPLETED_COUNT:]
        _require(len(expected_pending) == 52 and receipt["pending_plan"] == expected_pending)
        campaign = b["campaign"][0]
        _require(campaign["frozen"] == 1 and campaign["freeze_code"] == "interrupted")
        _require(campaign["budget_cny"] == "18" and campaign["cap_cny"] == "14.40000000")
        _require(campaign["safety_fraction"] == "0.8")
        rows = b["reservation"]
        _require(len(rows) == BASELINE_COUNT and len({r["reservation_id"] for r in rows}) == BASELINE_COUNT)
        _require(sum(row["state"] == "settled" for row in rows) == BASELINE_COUNT - 3)
        old_unknown = {row["reservation_id"]: row for row in expansion["baseline"]["reservation"] if row["state"] == "unknown"}
        unknown = {row["reservation_id"]: row for row in rows if row["state"] == "unknown"}
        _require(len(old_unknown) == 2 and set(unknown) == {*old_unknown, ORPHAN_ID})
        _require(all(unknown[ident] == row for ident, row in old_unknown.items()))
        orphan = unknown[ORPHAN_ID]
        held = _money(_cost(CONTEXT_RESERVATION_INPUT_TOKENS, 1024, INPUT_PRICE_FLOOR, OUTPUT_PRICE_FLOOR))
        _require(orphan["state"] == "unknown" and orphan["error_code"] == "interrupted")
        _require(orphan["reserved_cny"] == held and orphan["accounted_cny"] is None)
        _require(orphan["input_tokens"] == BOUNDED_INPUT_TOKENS and orphan["output_tokens"] == 1024)
        _require(orphan["input_price"] == "2" and orphan["output_price"] == "8")
        _require(all(orphan[key] is None for key in USAGE_FIELDS))
        historical = copy.deepcopy(b)
        del historical["rag_expansion_allowance"]
        old_ids = {r["reservation_id"] for r in expansion["baseline"]["reservation"]}
        historical["reservation"] = [r for r in historical["reservation"] if r["reservation_id"] in old_ids]
        for key in ("frozen", "freeze_code"):
            historical["campaign"][0][key] = expansion["baseline"]["campaign"][0][key]
        _require(digest(historical) == expansion["baseline_sha256"])
        _require(set(proof) == {
            "collector_name", "files_sha256", "plan_sha256", "started_row_sha256", "orphan_reservation_id",
            "orphan_case_id", "last_sequence", "completed_count", "collector_complete", "orphan_started_without_outcome",
        })
        _require(proof["collector_name"] == COLLECTOR_NAME and proof["orphan_reservation_id"] == ORPHAN_ID)
        _require(proof["orphan_case_id"] == ORPHAN_CASE and proof["last_sequence"] == 125)
        _require(proof["completed_count"] == COMPLETED_COUNT and proof["collector_complete"] is False)
        _require(proof["orphan_started_without_outcome"] is True and proof["plan_sha256"] == digest(plan))
        _require(set(proof["files_sha256"]) == set(FILES))
        _require(all(_hash(value) for value in proof["files_sha256"].values()) and _hash(proof["started_row_sha256"]))
        raw_review = receipt["independent_review_json"]
        _require(hashlib.sha256(raw_review.encode()).hexdigest() == receipt["review_sha256"])
        review = json.loads(raw_review)
        _require(set(review) == {
            "reviewer", "passed", "authorization_text", "source_thread", "checked_at", "collector_name",
            "collector_files_sha256", "orphan_reservation_id", "worker_processes", "no_automatic_retry",
            "unknown_hold_retained", "money_cap_unchanged", "repair_recheck_case_ids", "repair_evidence_sha256",
        })
        _require(review["reviewer"] == "independent_agent" and review["passed"] is True)
        _require(review["authorization_text"] == AUTHORIZATION and review["source_thread"] == SOURCE_THREAD)
        _require(review["collector_name"] == COLLECTOR_NAME and review["orphan_reservation_id"] == ORPHAN_ID)
        _require(review["collector_files_sha256"] == proof["files_sha256"] and review["worker_processes"] == [])
        _require(all(review[k] is True for k in ("no_automatic_retry", "unknown_hold_retained", "money_cap_unchanged")))
        _require(review["repair_recheck_case_ids"] == list(REPAIR_RECHECK_IDS))
        _require(_hash(review["repair_evidence_sha256"]))
        _require(datetime.fromisoformat(review["checked_at"]).tzinfo is not None)
        _require(receipt["authorization_text"] == AUTHORIZATION and receipt["source_thread"] == SOURCE_THREAD)
        _require(receipt["orphan_reservation_id"] == ORPHAN_ID and receipt["additional_money_cny"] == "0")
        _require(receipt["budget_cny"] == "28.00000000" and receipt["cap_cny"] == "22.40000000")
        _require(receipt["original_absolute_limit"] == ORIGINAL_ABSOLUTE_LIMIT)
        _require(receipt["max_additional_requests"] == MAX_ADDITIONAL_REQUESTS == 3 * len(receipt["pending_plan"]))
        _require(receipt["absolute_limit"] == ABSOLUTE_LIMIT and receipt["unknown_costs_released"] is False)
        _require(receipt["baseline_sha256"] == digest(b))
        _require(receipt["id"] == digest({k: v for k, v in receipt.items() if k != "id"}))
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        raise BudgetError("stage20_recovery_receipt_invalid") from None
    return receipt


def verify_history(db, receipt):
    current = snapshot(db)
    ids = {r["reservation_id"] for r in receipt["baseline"]["reservation"]}
    current["reservation"] = [r for r in current["reservation"] if r["reservation_id"] in ids]
    for key in ("frozen", "freeze_code"):
        current["campaign"][0][key] = receipt["baseline"]["campaign"][0][key]
    _require(digest(current) == receipt["baseline_sha256"], "stage20_recovery_history_changed")


def register(book, receipt):
    """Explicit one-time registration, never an automatic failure/retry path."""
    validate(receipt)
    with book._transaction() as db:
        status = book._summary(db)
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone()
        if exists:
            rows = db.execute(f"SELECT * FROM {TABLE}").fetchall()
            _require(len(rows) == 1 and rows[0]["id"] == receipt["id"]
                     and json.loads(rows[0]["receipt_json"]) == receipt, "stage20_recovery_already_registered")
            verify_history(db, receipt)
            return {"id": receipt["id"], "reused": True, **status}
        _require(status["frozen"] and status["freeze_code"] == "interrupted", "stage20_recovery_campaign_not_eligible")
        _require(status["inflight_count"] == 0, "request_inflight")
        _require(status["price_verification_status"] == "ready", status["price_verification_status"])
        _require(status["reservation_count"] == BASELINE_COUNT, "stage20_recovery_baseline_changed")
        _require(snapshot(db) == receipt["baseline"], "stage20_recovery_baseline_changed")
        _require(status["budget_cny"] == receipt["budget_cny"] and status["cap_cny"] == receipt["cap_cny"], "stage20_recovery_cap_changed")
        checked = datetime.fromisoformat(json.loads(receipt["independent_review_json"])["checked_at"])
        age = book.clock().astimezone(UTC) - checked.astimezone(UTC)
        _require(timedelta(0) <= age < REVIEW_TTL, "stage20_recovery_review_expired")
        next_hold = _cost(BOUNDED_INPUT_TOKENS, 1024, INPUT_PRICE_FLOOR, OUTPUT_PRICE_FLOOR)
        _require(Decimal(status["remaining_cny"]) >= next_hold, "budget_exhausted")
        db.execute(f"CREATE TABLE {TABLE} (id TEXT PRIMARY KEY, review_sha256 TEXT NOT NULL UNIQUE, receipt_json TEXT NOT NULL, authorized_at TEXT NOT NULL)")
        db.execute(f"INSERT INTO {TABLE} VALUES (?,?,?,?)", (
            receipt["id"], receipt["review_sha256"], json.dumps(receipt, ensure_ascii=False, sort_keys=True),
            book.clock().astimezone(UTC).isoformat(),
        ))
        db.execute("UPDATE campaign SET frozen=0,freeze_code=NULL WHERE campaign_id=?", (status["campaign_id"],))
        return {"id": receipt["id"], "reused": False, **book._summary(db)}


def guard(db, bounded_request):
    """Guard the exact remaining/repair plan; return its bounded request limit."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone():
        return
    rows = db.execute(f"SELECT * FROM {TABLE}").fetchall()
    _require(len(rows) == 1, "stage20_recovery_registration_invalid")
    receipt = validate(json.loads(rows[0]["receipt_json"]))
    _require(rows[0]["id"] == receipt["id"] and rows[0]["review_sha256"] == receipt["review_sha256"], "stage20_recovery_registration_invalid")
    verify_history(db, receipt)
    _require(bounded_request is not None and bounded_request.profile_id == STAGE16_PROFILE_ID,
             "stage20_recovery_request_outside_plan")
    bounded_request.validate()
    _require(bounded_request.public_input_sha256 in {public_identity(c) for c in receipt["pending_plan"]},
             "stage20_recovery_request_outside_plan")
    count = db.execute("SELECT count(*) FROM reservation").fetchone()[0]
    _require(count < ABSOLUTE_LIMIT, "campaign_request_limit_reached")
    return ABSOLUTE_LIMIT
