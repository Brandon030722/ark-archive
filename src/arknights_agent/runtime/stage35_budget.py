"""Native, finite Stage35 development-only budget successor.

Only the four unchanged Stage34 development-001 identities are added. Retired
receipts, costs and per-input counts remain immutable; all new revisions share
one twelve-call allowance. Unknown outcomes retain BudgetLedger's full-context
hold and freeze behavior, even when that hold exceeds a normal-send ceiling.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tarfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from arknights_agent import services
from arknights_agent.runtime import stage33_budget as previous
from arknights_agent.runtime.bounded_profile import (
    BOUNDED_INPUT_TOKENS,
    STAGE16_PROFILE_ID,
    BoundedRequest,
)
from arknights_agent.runtime.budget import (
    CAMPAIGN_ID,
    INPUT_PRICE_FLOOR,
    MAX_OUTPUT_TOKENS,
    OUTPUT_PRICE_FLOOR,
    BudgetError,
    BudgetLedger,
    _cost,
    _decimal,
    _money,
)
from arknights_agent.runtime.request_allowance import digest, public_identity
from arknights_agent.runtime.stage26_model_profile import CurrentModelBoundedRequest

common, r2 = previous.common, previous.r2
Decimal, sha = common.Decimal, common.sha
TABLE, ATTEMPT_TABLE = "stage35_public_allowance", "stage35_request_attempt"
REVISION_TABLE = "stage35_reviewed_revision"
NEW_TABLES = {TABLE, ATTEMPT_TABLE, REVISION_TABLE}
BASELINE_COUNT, OLD_ATTEMPTS_TOTAL = 2248, 213
BASELINE_ACCOUNTED, BASELINE_UNKNOWN = "16.44069240", "9.04076800"
BASELINE_SAFE = "3.86853960"
INHERITED_PHASE_OCCUPIED, PHASE_CAP = "0.20063560", "4.07000000"
NEW_STAGE35_CHARGE_CAP = "3.57000000"
PHASE_ORIGIN_COUNT, PHASE_ORIGIN_ACCOUNTED = 2210, "16.24005680"
SEND_HOLD = common.SEND_HOLD
MAX_ATTEMPTS, MAX_REQUESTS, CORRECTION_CAPACITY = 3, 12, 8
CUMULATIVE_REQUEST_LIMIT, ABSOLUTE_LIMIT = 441, 2476
OLD_RUNTIME_HASH = "9f6a9fb4776f7e8b53a9ba5c3cd164bb536f7b45e18ed8ea533ce907349d320e"
OLD_FREEZE_SHA256 = "fb716fa6d83513138037d59919a949b4ffedf450b3934c83d4e40706b3432e64"
OLD_ARCHIVE_SHA256 = "2ab22649b64137efbc88102704586f0efd5f3ff584d425a24f74318706846eb7"
OLD_RESULT_MANIFEST_SHA256 = "e4dcaad35379a098a5b1f0ef2bacdcef0c86cbc9e746093fbbb6c03e783c8dc7"
PREPARATION_FREEZE_SHA256 = "01f4022d57b0b9e6a33984cd1e8a4ab6701c3ee2e4f62206870a2045f9eeb3f8"
AUTHORIZED_PLAN_IDENTITIES_SHA256 = (
    "8bcc2d948f5eb6f00c17503223d7a0e7990b9bf2354bafffa23697a1970c5097"
)
TASK_FILE_SHA256 = "50b3bc4f5c906dc41094784d68530a57ebf7e154eb39b73b7a7208170fe72f79"
TASK_CANONICAL_SHA256 = "7f833d64dc77e7a31d342574a7997a8166e16bf4811464686276b86750ec64ab"
PLAN_SHA256 = "a23195ff33d3c152d3f694d92f9c967eed36e35c93a6a61be37c0758104293ad"
ARCHIVE_BACKED_SOURCE_PATHS = frozenset(
    {
        "src/arknights_agent/runtime/stage26_current_model.py",
        "tests/test_stage26_current_model.py",
    }
)
REVIEW_TTL = common.REVIEW_TTL
FIXED = dict(
    campaign_id=CAMPAIGN_ID,
    baseline_count=BASELINE_COUNT,
    budget_cny="32.00000000",
    cap_cny="29.35000000",
    phase_origin_count=PHASE_ORIGIN_COUNT,
    phase_origin_accounted_cny=PHASE_ORIGIN_ACCOUNTED,
    shared_phase_cap_cny=PHASE_CAP,
    inherited_phase_occupied_cny=INHERITED_PHASE_OCCUPIED,
    new_stage35_charge_cap_cny=NEW_STAGE35_CHARGE_CAP,
    max_attempts_per_turn=MAX_ATTEMPTS,
    max_additional_requests=MAX_REQUESTS,
    cumulative_stage23_request_limit=CUMULATIVE_REQUEST_LIMIT,
    absolute_limit=ABSOLUTE_LIMIT,
    unknown_costs_released=False,
    development_only=True,
    holdout_stays_sealed=True,
)
REVIEW_SAFEGUARDS = (
    "direct_current_task_and_exact_four_input_scope_verified",
    "old_stage33_archive_and_150_results_and_stage34_81_artifacts_unchanged",
    "old_163_scope_213_attempts_and_exhausted_identities_retained",
    "four_original_stage34_development001_inputs_have_zero_prior_attempts",
    "source_oracle_scoring_and_fresh_current_model_price_sources_verified",
    "all_revisions_share_original_2210_phase_baseline_and_12_call_limit",
    "original_full_send_and_unknown_escalation_freeze_retained",
    "all_eight_reachable_correction_slots_forecast_without_guarantee",
    "nominal32_safety29_35_shared4_07_and_normal3_57_verified",
    "no_holdout_calls_recharge_counter_reset_or_automatic_retry",
)
SCHEMAS = {
    TABLE: "id TEXT PRIMARY KEY, review_sha256 TEXT NOT NULL UNIQUE, receipt_json TEXT NOT NULL, authorized_at TEXT NOT NULL",
    REVISION_TABLE: f"id TEXT PRIMARY KEY, revision_number INTEGER NOT NULL UNIQUE, previous_revision_id TEXT REFERENCES {REVISION_TABLE}(id), allowance_id TEXT NOT NULL REFERENCES {TABLE}(id), review_json TEXT NOT NULL, authorized_at TEXT NOT NULL",
    ATTEMPT_TABLE: f"reservation_id TEXT PRIMARY KEY REFERENCES reservation(reservation_id), allowance_id TEXT NOT NULL REFERENCES {TABLE}(id), revision_id TEXT NOT NULL REFERENCES {REVISION_TABLE}(id), public_input_sha256 TEXT NOT NULL",
}
PROTECTIONS = {
    f"stage35_{table}_{op.lower()}": f"CREATE TRIGGER stage35_{table}_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT, 'stage35_append_only'); END"
    for table in NEW_TABLES
    for op in ("UPDATE", "DELETE")
}


def require(value, code="stage35_receipt_invalid"):
    if not value:
        raise BudgetError(code)


def _equal(value, expected, code="stage35_receipt_invalid"):
    require(
        all(
            type(value[key]) is type(item) and value[key] == item for key, item in expected.items()
        ),
        code,
    )


def implementation_paths():
    return (
        *previous.implementation_paths(),
        "src/arknights_agent/runtime/stage26_current_model.py",
        "tests/test_stage26_current_model.py",
        "tests/test_stage35_price_sources.py",
        "src/arknights_agent/runtime/stage35_budget.py",
        "tests/test_stage35_budget.py",
    )


def implementation_hashes():
    return {
        name: hashlib.sha256((r2.r1.prior.SOURCE_ROOT / name).read_bytes()).hexdigest()
        for name in implementation_paths()
    }


def manifest(db, baseline=None, *, excluded_tables=None):
    return previous.manifest(
        db, baseline, excluded_tables=NEW_TABLES if excluded_tables is None else excluded_tables
    )


def _verify_retired_artifacts():
    root = r2.r1.prior.SOURCE_ROOT
    freeze_path = root / ".local/stage33-initial-code-freeze.json"
    require(
        hashlib.sha256(freeze_path.read_bytes()).hexdigest() == OLD_FREEZE_SHA256,
        "stage35_retired_freeze_changed",
    )
    frozen = json.loads(freeze_path.read_text())
    _equal(
        frozen,
        dict(runtime_implementation_hash=OLD_RUNTIME_HASH, archive_sha256=OLD_ARCHIVE_SHA256),
    )
    require(len(frozen["file_hashes"]) == 312)
    require(
        hashlib.sha256((root / frozen["archive_path"]).read_bytes()).hexdigest()
        == OLD_ARCHIVE_SHA256,
        "stage35_retired_archive_changed",
    )
    with tarfile.open(root / frozen["archive_path"], "r:gz") as packed:
        files = [m for m in packed.getmembers() if m.isfile()]
        members = {m.name.removeprefix("./"): m for m in files}
        require(len(files) == len(members) == 312 and set(members) == set(frozen["file_hashes"]))
        for path, expected in frozen["file_hashes"].items():
            require(
                hashlib.sha256(packed.extractfile(members[path]).read()).hexdigest() == expected,
                "stage35_retired_archive_source_changed",
            )
            if path not in ARCHIVE_BACKED_SOURCE_PATHS:
                require(
                    hashlib.sha256((root / path).read_bytes()).hexdigest() == expected,
                    "stage35_unauthorized_retired_source_change",
                )
    for path, pinned, count, key in (
        (".local/stage33-result-freeze.json", OLD_RESULT_MANIFEST_SHA256, 150, "artifact_sha256"),
        (".local/stage34-result-freeze.json", PREPARATION_FREEZE_SHA256, 81, "artifact_sha256"),
    ):
        raw = (root / path).read_bytes()
        require(hashlib.sha256(raw).hexdigest() == pinned, "stage35_retired_result_changed")
        result = json.loads(raw)
        require(len(result[key]) == count)
        for name, expected in result[key].items():
            if name in ARCHIVE_BACKED_SOURCE_PATHS:
                require(frozen["file_hashes"][name] == expected)
            else:
                item = (root / name).resolve()
                require(
                    item.is_relative_to(root)
                    and item.is_file()
                    and hashlib.sha256(item.read_bytes()).hexdigest() == expected,
                    "stage35_retired_artifact_changed",
                )
    return frozen


def validate_native_chain(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone():
        receipt = _receipt_from_native_blob(
            db.execute(f"SELECT receipt_json FROM {TABLE}").fetchone()[0]
        )
        require(
            manifest(db, receipt["baseline_manifest"]) == receipt["baseline_manifest"],
            "stage35_history_changed",
        )
        _verify_retired_artifacts()
        return receipt["native_chain_summary"]
    frozen = _verify_retired_artifacts()
    rows = db.execute(f"SELECT * FROM {previous.TABLE}").fetchall()
    require(len(rows) == 1)
    row, old = rows[0], previous._receipt_from_native_blob(rows[0]["receipt_json"])
    _equal(row, dict(id=old["id"], review_sha256=old["review_sha256"]))
    require(previous.validate_native_chain(db) == old["native_chain_summary"])
    require(previous.manifest(db, old["baseline_manifest"]) == old["baseline_manifest"])
    require(
        {
            key: value
            for table in previous.NEW_TABLES
            for key, value in r2.r1._triggers(db, table).items()
        }
        == previous.PROTECTIONS
    )
    revision, review = previous._revision(db)
    require(
        review["runtime_implementation_hash"] == OLD_RUNTIME_HASH
        and review["reviewed_files_sha256"]
        == {path: frozen["file_hashes"][path] for path in previous.implementation_paths()}
    )
    seals = db.execute(f"SELECT * FROM {previous.SEAL_TABLE}").fetchall()
    require(
        len(seals) == 1
        and seals[0]["revision_id"] == revision["id"]
        and seals[0]["runtime_implementation_hash"] == OLD_RUNTIME_HASH
        and seals[0]["id"] == digest(json.loads(seals[0]["review_json"]))
    )
    attempts, reservations = previous._ownership(db, old)
    require(
        len(attempts) == 5
        and previous._phase_occupied(reservations) == INHERITED_PHASE_OCCUPIED
        and all(item["state"] == "settled" for item in reservations)
    )
    require(
        {item["public_input_sha256"] for item in attempts}
        == {public_identity(case) for case in old["plan"]}
    )
    identities = old["native_chain_summary"]["authorized_plan_identities"] + [
        {"case_id": case["case_id"], "public_input_sha256": public_identity(case)}
        for case in old["plan"]
    ]
    all_attempts = old["native_chain_summary"]["previous_attempts"] + [
        {key: item[key] for key in ("reservation_id", "allowance_id", "public_input_sha256")}
        for item in attempts
    ]
    require(
        len(identities) == 163
        and digest(identities) == AUTHORIZED_PLAN_IDENTITIES_SHA256
        and len(all_attempts) == OLD_ATTEMPTS_TOTAL
    )
    return dict(
        previous_receipt_id=old["id"],
        previous_receipt_json_sha256=sha(row["receipt_json"]),
        previous_runtime_hash=OLD_RUNTIME_HASH,
        previous_code_freeze_sha256=OLD_FREEZE_SHA256,
        previous_code_archive_sha256=OLD_ARCHIVE_SHA256,
        previous_result_manifest_sha256=OLD_RESULT_MANIFEST_SHA256,
        preparation_result_manifest_sha256=PREPARATION_FREEZE_SHA256,
        authorized_plan_identities=identities,
        previous_attempts=sorted(
            all_attempts, key=lambda item: (item["allowance_id"], item["reservation_id"])
        ),
        inherited_phase_occupied_cny=INHERITED_PHASE_OCCUPIED,
    )


def _validate_plan(plan, development_ids, chain):
    require(
        digest(plan) == PLAN_SHA256
        and len(plan) == 4
        and development_ids == [case["case_id"] for case in plan]
        and development_ids == [f"stage34-dev-001-t{i}" for i in range(1, 5)]
    )
    require(
        all(
            set(case) == {"case_id", "question", "module", "release_id"}
            and r2.r1.prior._public_question(case["question"])
            and case["module"] == "auto"
            and case["release_id"] == r2.r1.prior.RELEASE_ID
            for case in plan
        )
    )
    ids = {public_identity(case) for case in plan}
    old_ids = {item["public_input_sha256"] for item in chain["authorized_plan_identities"]}
    counts = Counter(item["public_input_sha256"] for item in chain["previous_attempts"])
    require(
        len(ids) == 4 and not ids & old_ids and all(counts[item] == 0 for item in ids),
        "stage35_new_scope_invalid",
    )


def _forecast_valid(
    forecast,
    *,
    occupied="0.00000000",
    correction_capacity=CORRECTION_CAPACITY,
    remaining_slots=MAX_REQUESTS,
):
    require(
        set(forecast)
        == {
            "projected_full_round_conservative_cny",
            "final_send_reservation_cny",
            "parameter_correction_calls_included",
            "estimate_not_guarantee",
        }
    )
    require(
        forecast["final_send_reservation_cny"] == SEND_HOLD
        and type(forecast["parameter_correction_calls_included"]) is int
        and forecast["parameter_correction_calls_included"] == correction_capacity
        and forecast["estimate_not_guarantee"] is True,
        "stage35_correction_forecast_invalid",
    )
    projected, occupied = (
        _decimal(forecast["projected_full_round_conservative_cny"]),
        _decimal(occupied),
    )
    require(
        projected == Decimal(SEND_HOLD) * remaining_slots,
        "stage35_full_slot_projection_required",
    )
    require(
        occupied + projected <= Decimal(NEW_STAGE35_CHARGE_CAP),
        "stage35_normal_projection_exceeds_cap",
    )
    require(
        Decimal(INHERITED_PHASE_OCCUPIED) + occupied + projected + Decimal(SEND_HOLD)
        <= Decimal(PHASE_CAP),
        "stage35_shared_projection_exceeds_cap",
    )
    require(
        occupied + projected + Decimal(SEND_HOLD) <= Decimal(BASELINE_SAFE),
        "stage35_complete_round_projection_exceeds_safe_budget",
    )


def _review_valid(review, current=False):
    _equal(
        review,
        dict(reviewer="independent_agent", passed=True, **dict.fromkeys(REVIEW_SAFEGUARDS, True)),
    )
    require(datetime.fromisoformat(review["checked_at"]).tzinfo is not None)
    require(
        set(review["reviewed_files_sha256"]) == set(implementation_paths())
        and all(r2.r1.prior._hash(v) for v in review["reviewed_files_sha256"].values())
    )
    require(
        all(
            r2.r1.prior._hash(review[k])
            for k in (
                "runtime_implementation_hash",
                "split_evidence_sha256",
                "forecast_evidence_sha256",
            )
        )
    )
    if current:
        _equal(
            review,
            dict(
                reviewed_files_sha256=implementation_hashes(),
                runtime_implementation_hash=services.implementation_hash(),
            ),
            "stage35_implementation_changed",
        )


def _review_base(forecast, development_case_ids, split_evidence_sha256, forecast_evidence_sha256):
    return dict(
        reviewer="independent_agent",
        passed=False,
        checked_at=None,
        development_case_ids=list(development_case_ids),
        forecast=copy.deepcopy(forecast),
        split_evidence_sha256=split_evidence_sha256,
        forecast_evidence_sha256=forecast_evidence_sha256,
        reviewed_files_sha256=implementation_hashes(),
        runtime_implementation_hash=services.implementation_hash(),
        **dict.fromkeys(REVIEW_SAFEGUARDS, False),
    )


def review_template(
    baseline,
    plan,
    chain,
    forecast,
    *,
    development_case_ids,
    split_evidence_sha256,
    forecast_evidence_sha256,
):
    return dict(
        **_review_base(
            forecast, development_case_ids, split_evidence_sha256, forecast_evidence_sha256
        ),
        baseline_manifest_sha256=digest(baseline),
        plan_sha256=digest(plan),
        native_chain_summary_sha256=digest(chain),
        task_evidence_file_sha256=TASK_FILE_SHA256,
    )


def build_receipt(
    baseline, plan, chain, human_task_path, independent_review_path, *, development_case_ids
):
    task_raw, review_raw = (
        Path(human_task_path).read_text(),
        Path(independent_review_path).read_text(),
    )
    require(sha(task_raw) == TASK_FILE_SHA256, "stage35_task_evidence_changed")
    value = dict(
        **FIXED,
        task_evidence=json.loads(task_raw),
        task_evidence_file_sha256=TASK_FILE_SHA256,
        baseline_manifest=copy.deepcopy(baseline),
        baseline_manifest_sha256=digest(baseline),
        native_chain_summary=copy.deepcopy(chain),
        native_chain_summary_sha256=digest(chain),
        plan=copy.deepcopy(plan),
        plan_sha256=digest(plan),
        development_case_ids=list(development_case_ids),
        independent_review_json=review_raw,
        review_sha256=sha(review_raw),
    )
    value["id"] = digest(value)
    return validate(value)


def validate(receipt):
    try:
        _equal(receipt, FIXED)
        require(
            receipt["task_evidence_file_sha256"] == TASK_FILE_SHA256
            and digest(receipt["task_evidence"]) == TASK_CANONICAL_SHA256
        )
        baseline, chain = receipt["baseline_manifest"], receipt["native_chain_summary"]
        require(
            baseline["encoding"] == "exact_sqlite_typed_row_sha256_v1"
            and baseline["schema_sha256"] == digest(baseline["schema"])
        )
        _equal(
            baseline["finance"],
            dict(
                reservation_count=BASELINE_COUNT,
                accounted_cny=BASELINE_ACCOUNTED,
                unknown_hold_cny=BASELINE_UNKNOWN,
                unknown_count=3,
                inflight_count=0,
            ),
        )
        require(
            all(
                table["rows_sha256"] == digest(table["rows"])
                and table["primary_key"]
                and all(r2.r1.prior._hash(row["sha256"]) for row in table["rows"])
                for table in baseline["tables"].values()
            )
        )
        require(
            len(baseline["tables"]["reservation"]["rows"]) == BASELINE_COUNT
            and len(chain["authorized_plan_identities"]) == 163
            and digest(chain["authorized_plan_identities"]) == AUTHORIZED_PLAN_IDENTITIES_SHA256
            and len(chain["previous_attempts"]) == OLD_ATTEMPTS_TOTAL
            and len({item["reservation_id"] for item in chain["previous_attempts"]})
            == OLD_ATTEMPTS_TOTAL
        )
        _equal(
            chain,
            dict(
                previous_runtime_hash=OLD_RUNTIME_HASH,
                previous_code_freeze_sha256=OLD_FREEZE_SHA256,
                previous_code_archive_sha256=OLD_ARCHIVE_SHA256,
                previous_result_manifest_sha256=OLD_RESULT_MANIFEST_SHA256,
                preparation_result_manifest_sha256=PREPARATION_FREEZE_SHA256,
                inherited_phase_occupied_cny=INHERITED_PHASE_OCCUPIED,
            ),
        )
        _validate_plan(receipt["plan"], receipt["development_case_ids"], chain)
        _equal(
            receipt,
            dict(
                baseline_manifest_sha256=digest(baseline),
                native_chain_summary_sha256=digest(chain),
                plan_sha256=digest(receipt["plan"]),
            ),
        )
        review = json.loads(receipt["independent_review_json"])
        require(sha(receipt["independent_review_json"]) == receipt["review_sha256"])
        _review_valid(review)
        _equal(
            review,
            {
                key: receipt[key]
                for key in (
                    "baseline_manifest_sha256",
                    "native_chain_summary_sha256",
                    "plan_sha256",
                    "task_evidence_file_sha256",
                    "development_case_ids",
                )
            },
        )
        _forecast_valid(review["forecast"])
        require(receipt["id"] == digest({key: v for key, v in receipt.items() if key != "id"}))
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        raise BudgetError("stage35_receipt_invalid") from None
    return receipt


@lru_cache(maxsize=4)
def _validated_native_blob(raw):
    return validate(json.loads(raw))


def _receipt_from_native_blob(raw):
    return copy.deepcopy(_validated_native_blob(raw))


def _ownership(db, receipt):
    attempts = [
        dict(row) for row in db.execute(f"SELECT * FROM {ATTEMPT_TABLE} ORDER BY reservation_id")
    ]
    old_ids = {
        row["key"][0] for row in receipt["baseline_manifest"]["tables"]["reservation"]["rows"]
    }
    rows = [
        dict(row)
        for row in db.execute("SELECT * FROM reservation")
        if row["reservation_id"] not in old_ids
    ]
    allowed = {public_identity(case) for case in receipt["plan"]}
    require(
        {item["reservation_id"] for item in attempts} == {row["reservation_id"] for row in rows}
        and all(
            item["allowance_id"] == receipt["id"] and item["public_input_sha256"] in allowed
            for item in attempts
        ),
        "stage35_attempt_ownership_invalid",
    )
    counts = Counter(
        item["public_input_sha256"]
        for item in attempts + receipt["native_chain_summary"]["previous_attempts"]
    )
    require(
        len(attempts) <= MAX_REQUESTS
        and sum(counts.values()) <= CUMULATIVE_REQUEST_LIMIT
        and all(number <= MAX_ATTEMPTS for number in counts.values()),
        "stage35_attempt_limit_invalid",
    )
    return attempts, rows


def _phase_occupied(rows):
    return _money(Decimal(INHERITED_PHASE_OCCUPIED) + Decimal(common._stage_occupied(rows)))


def _revision(db):
    rows = db.execute(f"SELECT * FROM {REVISION_TABLE} ORDER BY revision_number").fetchall()
    require(bool(rows), "stage35_revision_missing")
    prior = None
    for number, row in enumerate(rows):
        review = json.loads(row["review_json"])
        require(
            row["revision_number"] == number
            and row["previous_revision_id"] == prior
            and row["id"] == digest(dict(previous_revision_id=prior, review=review)),
            "stage35_revision_chain_invalid",
        )
        prior = row["id"]
    return dict(rows[-1]), review


def registered(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone():
        return None
    rows = db.execute(f"SELECT * FROM {TABLE}").fetchall()
    require(len(rows) == 1)
    row, receipt = rows[0], _receipt_from_native_blob(rows[0]["receipt_json"])
    _equal(row, dict(id=receipt["id"], review_sha256=receipt["review_sha256"]))
    require(
        {k: v for table in NEW_TABLES for k, v in r2.r1._triggers(db, table).items()}
        == PROTECTIONS,
        "stage35_protection_invalid",
    )
    require(
        manifest(db, receipt["baseline_manifest"]) == receipt["baseline_manifest"],
        "stage35_history_changed",
    )
    attempts, _ = _ownership(db, receipt)
    revision, review = _revision(db)
    _review_valid(review, current=True)
    versions = [dict(item) for item in db.execute(f"SELECT * FROM {REVISION_TABLE}")]
    require(all(item["allowance_id"] == receipt["id"] for item in versions))
    require(all(item["revision_id"] in {r["id"] for r in versions} for item in attempts))
    require(revision["allowance_id"] == receipt["id"])
    return receipt


def _state(db, receipt):
    attempts, rows = _ownership(db, receipt)
    return dict(
        allowance_id=receipt["id"],
        current_revision_id=_revision(db)[0]["id"],
        stage_attempts_sha256=digest(attempts),
        stage_occupied_cny=_phase_occupied(rows),
    )


def revision_review_template(
    state, forecast, *, development_case_ids, split_evidence_sha256, forecast_evidence_sha256
):
    return dict(
        **_review_base(
            forecast, development_case_ids, split_evidence_sha256, forecast_evidence_sha256
        ),
        **copy.deepcopy(state),
    )


class Stage35BudgetLedger(BudgetLedger):
    def _summary(self, db, reservation=None):
        value, receipt = super()._summary(db, reservation), registered(db)
        if receipt is None:
            old = db.execute(f"SELECT receipt_json FROM {previous.TABLE}").fetchone()
            require(old is not None, "stage35_inherited_authorization_missing")
            previous._receipt_from_native_blob(old[0])
        rows = _ownership(db, receipt)[1] if receipt else []
        value.update(
            cap_cny="29.35000000",
            safety_cap_basis="stage35_explicit_human_authorization",
            stage35_new_occupied_cny=common._stage_occupied(rows),
            shared_stage31_and_successors_occupied_cny=_phase_occupied(rows),
            shared_phase_charge_cap_cny=PHASE_CAP,
            stage35_normal_charge_cap_cny=NEW_STAGE35_CHARGE_CAP,
        )
        value["remaining_cny"] = _money(
            max(
                Decimal(0),
                Decimal(value["cap_cny"])
                - Decimal(value["accounted_cny"])
                - Decimal(value["reserved_cny"]),
            )
        )
        return value

    def _review_fresh(self, review):
        _review_valid(review, current=True)
        age = self.clock().astimezone(UTC) - datetime.fromisoformat(
            review["checked_at"]
        ).astimezone(UTC)
        require(timedelta(0) <= age < REVIEW_TTL, "stage35_review_expired")

    def _ready(self, status):
        require(not status["frozen"], "campaign_frozen")
        require(status["inflight_count"] == 0, "request_inflight")
        require(status["price_verification_status"] == "ready", status["price_verification_status"])

    def _append_version(self, db, receipt, review, prior=None):
        ident = digest(dict(previous_revision_id=prior["id"] if prior else None, review=review))
        db.execute(
            f"INSERT INTO {REVISION_TABLE} VALUES (?,?,?,?,?,?)",
            (
                ident,
                prior["revision_number"] + 1 if prior else 0,
                prior["id"] if prior else None,
                receipt["id"],
                json.dumps(review, ensure_ascii=False, sort_keys=True),
                self.clock().astimezone(UTC).isoformat(),
            ),
        )
        return ident

    def authorize_stage35_public_plan(self, receipt):
        validate(receipt)
        with self._transaction() as db:
            status, old = self._summary(db), registered(db)
            if old:
                require(old == receipt, "stage35_already_registered")
                return dict(id=receipt["id"], reused=True, **status)
            self._ready(status)
            _equal(
                status,
                dict(
                    reservation_count=BASELINE_COUNT,
                    accounted_cny=BASELINE_ACCOUNTED,
                    reserved_cny=BASELINE_UNKNOWN,
                    remaining_cny=BASELINE_SAFE,
                ),
                "stage35_baseline_finance_changed",
            )
            require(
                manifest(db) == receipt["baseline_manifest"]
                and validate_native_chain(db) == receipt["native_chain_summary"],
                "stage35_baseline_changed",
            )
            review = json.loads(receipt["independent_review_json"])
            self._review_fresh(review)
            for table, schema in SCHEMAS.items():
                db.execute(f"CREATE TABLE {table} ({schema})")
            for sql in PROTECTIONS.values():
                db.execute(sql)
            db.execute(
                f"INSERT INTO {TABLE} VALUES (?,?,?,?)",
                (
                    receipt["id"],
                    receipt["review_sha256"],
                    json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                    self.clock().astimezone(UTC).isoformat(),
                ),
            )
            self._append_version(db, receipt, review)
            return dict(id=receipt["id"], reused=False, **self._summary(db))

    def revision_state(self):
        with self._transaction() as db:
            receipt = registered(db)
            require(receipt is not None, "stage35_not_registered")
            return _state(db, receipt)

    def append_reviewed_revision(self, review):
        with self._transaction() as db:
            row = db.execute(f"SELECT receipt_json FROM {TABLE}").fetchone()
            require(row is not None, "stage35_not_registered")
            receipt = _receipt_from_native_blob(row[0])
            require(
                manifest(db, receipt["baseline_manifest"]) == receipt["baseline_manifest"],
                "stage35_history_changed",
            )
            _equal(review, _state(db, receipt), "stage35_revision_baseline_changed")
            _equal(review, dict(development_case_ids=receipt["development_case_ids"]))
            self._ready(super()._summary(db))
            attempts, rows = _ownership(db, receipt)
            counts = Counter(item["public_input_sha256"] for item in attempts)
            require(
                any(counts[public_identity(case)] < MAX_ATTEMPTS for case in receipt["plan"]),
                "stage35_all_identities_exhausted",
            )
            capacity = sum(
                max(0, MAX_ATTEMPTS - counts[public_identity(case)] - 1) for case in receipt["plan"]
            )
            _forecast_valid(
                review["forecast"],
                occupied=common._stage_occupied(rows),
                correction_capacity=capacity,
                remaining_slots=MAX_REQUESTS - len(attempts),
            )
            self._review_fresh(review)
            ident = self._append_version(db, receipt, review, _revision(db)[0])
            return dict(id=ident, **self._summary(db))

    def reserve(
        self,
        input_tokens,
        output_tokens,
        input_price,
        output_price,
        *,
        bounded_request=None,
        continuation_id=None,
    ):
        require(
            isinstance(bounded_request, BoundedRequest)
            and type(input_tokens) is int
            and input_tokens == BOUNDED_INPUT_TOKENS
            and type(output_tokens) is int
            and output_tokens == MAX_OUTPUT_TOKENS,
            "reservation_token_bound_invalid",
        )
        require(
            isinstance(bounded_request, CurrentModelBoundedRequest),
            "stage35_current_model_proof_required",
        )
        bounded_request.validate_current()
        bounded_request.validate()
        ip, op = (
            _decimal(input_price, minimum=INPUT_PRICE_FLOOR),
            _decimal(output_price, minimum=OUTPUT_PRICE_FLOOR),
        )
        held = _cost(input_tokens, output_tokens, ip, op)
        require(held == Decimal(SEND_HOLD), "stage35_full_send_hold_required")
        with self._transaction() as db:
            status = self._summary(db)
            self._ready(status)
            receipt = registered(db)
            require(receipt is not None, "stage35_not_registered")
            r2.bounded_proof_gate(self, db, bounded_request, continuation_id)
            require(
                bounded_request.profile_id == STAGE16_PROFILE_ID
                and bounded_request.public_input_sha256
                in {public_identity(case) for case in receipt["plan"]},
                "stage35_request_outside_plan",
            )
            attempts, rows = _ownership(db, receipt)
            for row in rows:
                r2.r1._reservation_shape(row, "settled")
            require(
                sum(
                    item["public_input_sha256"] == bounded_request.public_input_sha256
                    for item in attempts + receipt["native_chain_summary"]["previous_attempts"]
                )
                < MAX_ATTEMPTS,
                "stage35_turn_request_limit_reached",
            )
            require(
                len(attempts) < MAX_REQUESTS
                and len(attempts) + OLD_ATTEMPTS_TOTAL < CUMULATIVE_REQUEST_LIMIT
                and status["reservation_count"] < ABSOLUTE_LIMIT,
                "campaign_request_limit_reached",
            )
            require(
                Decimal(common._stage_occupied(rows)) + held <= Decimal(NEW_STAGE35_CHARGE_CAP),
                "stage35_normal_charge_cap_exhausted",
            )
            require(
                Decimal(_phase_occupied(rows)) + held <= Decimal(PHASE_CAP),
                "stage35_shared_phase_charge_cap_exhausted",
            )
            require(held <= Decimal(status["remaining_cny"]), "budget_exhausted")
            ident, revision = uuid4().hex, _revision(db)[0]
            db.execute(
                "INSERT INTO reservation (reservation_id,campaign_id,input_tokens,output_tokens,input_price,output_price,reserved_cny,state) VALUES (?,?,?,?,?,?,?,'reserved')",
                (ident, CAMPAIGN_ID, input_tokens, output_tokens, str(ip), str(op), _money(held)),
            )
            db.execute(
                f"INSERT INTO {ATTEMPT_TABLE} VALUES (?,?,?,?)",
                (ident, receipt["id"], revision["id"], bounded_request.public_input_sha256),
            )
            return ident
