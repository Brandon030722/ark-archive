"""Finite native Stage33 successor; retain the Stage31-origin phase ceiling.

Historical selectors are retired as immutable archives. Shared helpers only
hash/read old data or calculate costs; no old globals/selectors are patched.
Unknown billing keeps the original full-context escalation and freezes.
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
from arknights_agent.runtime import stage32_budget as previous
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

common, r2 = previous.previous, previous.r2
Decimal, sha = common.Decimal, common.sha
TABLE, ATTEMPT_TABLE = "stage33_public_allowance", "stage33_request_attempt"
REVISION_TABLE, SEAL_TABLE = "stage33_reviewed_revision", "stage33_holdout_seal"
NEW_TABLES = {TABLE, ATTEMPT_TABLE, REVISION_TABLE, SEAL_TABLE}
BASELINE_COUNT, OLD_ATTEMPTS_TOTAL = 2243, 208
BASELINE_ACCOUNTED, BASELINE_UNKNOWN = "16.41033240", "9.04076800"
BASELINE_SAFE, INHERITED_PHASE_OCCUPIED = "0.64889960", "0.17027560"
PHASE_CAP, NEW_STAGE33_CHARGE_CAP = "0.50000000", "0.32972440"
PHASE_ORIGIN_COUNT, PHASE_ORIGIN_ACCOUNTED = 2210, "16.24005680"
SEND_HOLD = common.SEND_HOLD
MAX_ATTEMPTS, MAX_REQUESTS = 3, 233
CUMULATIVE_REQUEST_LIMIT, ABSOLUTE_LIMIT = 441, 2476
TARGETED_COUNT, HOLDOUT_COUNT, CORRECTION_CAPACITY = 2, 2, 8
OLD_RUNTIME_HASH = "c790bb0d958df34646f57a5c8cf79fcc9fc640f1ed94073f1153164af2955fdf"
OLD_FREEZE_SHA256 = "99ddc3bf999649614bf0ada70b5875d19ed594478f94d6eb4556be5b55c5ef19"
OLD_ARCHIVE_SHA256 = "09bffc5468eef2440327d66fecb0267ba3625f004213e020fca8aa6e4c4fd46e"
OLD_RESULT_MANIFEST_SHA256 = "e12b2b243f218bcfb9bfff7707fd68a96089de6bd18f2fff90fab9e374e19dcb"
OLD_RESULT_ARTIFACT_COUNT = 155
ARCHIVE_BACKED_RESULT_PATHS = frozenset({"src/arknights_agent/roguelike_interpretation.py"})
AUTHORIZED_PLAN_IDENTITIES_SHA256 = (
    "4430258a409c79c33f8d7a88298a498076a0f74798c91b2cd8be6b570580ea85"
)
TASK_FILE_SHA256 = "47a4efabebe83cb466ec0960c464cc50c7eecfdb5e7d446576cd61adcdec1e0d"
TASK_CANONICAL_SHA256 = "0f09ab31272d69a3588bed93ce1a5b240c2ecf2ff01dba0824f1ab18a2f7275b"
PLAN_SHA256 = "9fae1d2338b6dd32d78ad889aea27b646bc9e89aaceab33e81cc5c120855931d"
REVIEW_TTL = common.REVIEW_TTL
FIXED = dict(
    campaign_id=CAMPAIGN_ID,
    baseline_count=BASELINE_COUNT,
    budget_cny="32.00000000",
    cap_cny="26.10000000",
    phase_origin_count=PHASE_ORIGIN_COUNT,
    phase_origin_accounted_cny=PHASE_ORIGIN_ACCOUNTED,
    shared_phase_cap_cny=PHASE_CAP,
    inherited_phase_occupied_cny=INHERITED_PHASE_OCCUPIED,
    new_stage33_charge_cap_cny=NEW_STAGE33_CHARGE_CAP,
    max_attempts_per_turn=MAX_ATTEMPTS,
    max_additional_requests=MAX_REQUESTS,
    cumulative_stage23_request_limit=CUMULATIVE_REQUEST_LIMIT,
    absolute_limit=ABSOLUTE_LIMIT,
    unknown_costs_released=False,
)
REVIEW_SAFEGUARDS = (
    "direct_current_task_and_inherited_half_yuan_scope_verified",
    "old_stage32_archive_result_and_native_rows_unchanged",
    "old_159_scope_and_exhausted_original_identity_retained",
    "four_genuinely_new_inputs_have_zero_prior_attempts",
    "source_oracle_split_and_current_model_sources_verified",
    "all_revisions_share_original_2210_phase_baseline",
    "same_final_revision_development_then_blind_enforced",
    "original_full_send_and_unknown_escalation_retained",
    "all_reachable_correction_slots_forecast_without_guarantee",
    "nominal_32_safety_26_10_and_no_recharge_unchanged",
    "no_automatic_retry_or_counter_reset",
)
PROTECTIONS = {
    f"stage33_{table}_{op.lower()}": f"CREATE TRIGGER stage33_{table}_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT, 'stage33_append_only'); END"
    for table in NEW_TABLES
    for op in ("UPDATE", "DELETE")
}
SCHEMAS = {
    TABLE: "id TEXT PRIMARY KEY, review_sha256 TEXT NOT NULL UNIQUE, receipt_json TEXT NOT NULL, authorized_at TEXT NOT NULL",
    REVISION_TABLE: f"id TEXT PRIMARY KEY, revision_number INTEGER NOT NULL UNIQUE, previous_revision_id TEXT REFERENCES {REVISION_TABLE}(id), allowance_id TEXT NOT NULL REFERENCES {TABLE}(id), review_json TEXT NOT NULL, authorized_at TEXT NOT NULL",
    ATTEMPT_TABLE: f"reservation_id TEXT PRIMARY KEY REFERENCES reservation(reservation_id), allowance_id TEXT NOT NULL REFERENCES {TABLE}(id), revision_id TEXT NOT NULL REFERENCES {REVISION_TABLE}(id), public_input_sha256 TEXT NOT NULL",
    SEAL_TABLE: f"id TEXT PRIMARY KEY, revision_id TEXT NOT NULL UNIQUE REFERENCES {REVISION_TABLE}(id), runtime_implementation_hash TEXT NOT NULL, review_json TEXT NOT NULL, sealed_at TEXT NOT NULL",
}


def require(value, code="stage33_receipt_invalid"):
    if not value:
        raise BudgetError(code)


def _equal(value, expected, code="stage33_receipt_invalid"):
    require(
        all(
            type(value[key]) is type(item) and value[key] == item for key, item in expected.items()
        ),
        code,
    )


def implementation_paths():
    return (
        *previous.implementation_paths(),
        "src/arknights_agent/runtime/stage33_budget.py",
        "tests/test_stage33_budget.py",
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


def _verify_retired_code_archive():
    root = r2.r1.prior.SOURCE_ROOT
    raw = (root / ".local/stage32-initial-code-freeze.json").read_text()
    require(sha(raw) == OLD_FREEZE_SHA256, "stage33_retired_freeze_changed")
    frozen = json.loads(raw)
    _equal(
        frozen,
        dict(runtime_implementation_hash=OLD_RUNTIME_HASH, archive_sha256=OLD_ARCHIVE_SHA256),
    )
    require(
        hashlib.sha256((root / frozen["archive_path"]).read_bytes()).hexdigest()
        == OLD_ARCHIVE_SHA256,
        "stage33_retired_archive_changed",
    )
    with tarfile.open(root / frozen["archive_path"], "r:gz") as packed:
        members = {item.name: item for item in packed.getmembers() if item.isfile()}
        require(set(members) == set(frozen["file_hashes"]))
        for name, expected in frozen["file_hashes"].items():
            stream = packed.extractfile(members[name])
            require(
                stream is not None and hashlib.sha256(stream.read()).hexdigest() == expected,
                "stage33_retired_archive_source_changed",
            )
    return frozen


def _verify_old_results():
    root = r2.r1.prior.SOURCE_ROOT
    raw = (root / ".local/stage32-result-freeze.json").read_text()
    require(sha(raw) == OLD_RESULT_MANIFEST_SHA256, "stage33_retired_result_changed")
    result = json.loads(raw)
    require(
        result["runtime_implementation_hash"] == OLD_RUNTIME_HASH
        and len(result["artifact_sha256"]) == OLD_RESULT_ARTIFACT_COUNT
    )
    archived_source = _verify_retired_code_archive()["file_hashes"]
    require(
        ARCHIVE_BACKED_RESULT_PATHS <= result["artifact_sha256"].keys()
        and ARCHIVE_BACKED_RESULT_PATHS <= archived_source.keys()
    )
    for relative, expected in result["artifact_sha256"].items():
        # Explicitly retired code may be edited by the current repair. Its old
        # bytes remain independently verified inside the immutable archive.
        if relative in ARCHIVE_BACKED_RESULT_PATHS:
            require(archived_source[relative] == expected, "stage33_retired_result_source_mismatch")
            continue
        path = (root / relative).resolve()
        require(
            path.is_relative_to(root)
            and path.is_file()
            and hashlib.sha256(path.read_bytes()).hexdigest() == expected,
            "stage33_retired_artifact_changed",
        )
    return OLD_RESULT_MANIFEST_SHA256


def validate_native_chain(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone():
        receipt = _receipt_from_native_blob(
            db.execute(f"SELECT receipt_json FROM {TABLE}").fetchone()[0]
        )
        require(
            manifest(db, receipt["baseline_manifest"]) == receipt["baseline_manifest"],
            "stage33_history_changed",
        )
        _verify_old_results()
        return receipt["native_chain_summary"]
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
        review["reviewed_files_sha256"] == previous.implementation_hashes()
        and review["runtime_implementation_hash"] == OLD_RUNTIME_HASH
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
        len(attempts) == 17
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
        if case["case_id"] not in old["development_case_ids"]
    ]
    all_attempts = old["native_chain_summary"]["previous_attempts"] + [
        {key: item[key] for key in ("reservation_id", "allowance_id", "public_input_sha256")}
        for item in attempts
    ]
    require(
        len(identities) == 159
        and digest(identities) == AUTHORIZED_PLAN_IDENTITIES_SHA256
        and len(all_attempts) == OLD_ATTEMPTS_TOTAL
    )
    return dict(
        previous_receipt_id=old["id"],
        previous_receipt_json_sha256=sha(row["receipt_json"]),
        previous_runtime_hash=OLD_RUNTIME_HASH,
        previous_code_freeze_sha256=OLD_FREEZE_SHA256,
        previous_code_archive_sha256=OLD_ARCHIVE_SHA256,
        previous_result_manifest_sha256=_verify_old_results(),
        authorized_plan_identities=identities,
        previous_attempts=sorted(
            all_attempts, key=lambda item: (item["allowance_id"], item["reservation_id"])
        ),
        inherited_phase_occupied_cny=INHERITED_PHASE_OCCUPIED,
    )


def _validate_plan(plan, development_ids, chain):
    require(
        digest(plan) == PLAN_SHA256 and len(plan) == 4 and len(development_ids) == TARGETED_COUNT
    )
    require(
        all(
            set(case) == {"case_id", "question", "module", "release_id"}
            and isinstance(case["case_id"], str)
            and 1 <= len(case["case_id"]) <= 128
            and r2.r1.prior._public_question(case["question"])
            and case["module"] == "auto"
            and case["release_id"] == r2.r1.prior.RELEASE_ID
            for case in plan
        )
    )
    require(
        len({case["case_id"] for case in plan})
        == len({public_identity(case) for case in plan})
        == 4
        and set(development_ids) <= {case["case_id"] for case in plan}
    )
    old_ids = {item["public_input_sha256"] for item in chain["authorized_plan_identities"]}
    counts = Counter(item["public_input_sha256"] for item in chain["previous_attempts"])
    require(
        all(
            public_identity(case) not in old_ids and counts[public_identity(case)] == 0
            for case in plan
        ),
        "stage33_new_scope_invalid",
    )
    development = [case for case in plan if case["case_id"] in development_ids]
    holdout = [case for case in plan if case["case_id"] not in development_ids]
    require(
        common._dialogue_lengths(development) == [2] and common._dialogue_lengths(holdout) == [2]
    )
    return development, holdout


def _review_valid(review, current=False):
    _equal(
        review,
        dict(reviewer="independent_agent", passed=True, **dict.fromkeys(REVIEW_SAFEGUARDS, True)),
    )
    require(datetime.fromisoformat(review["checked_at"]).tzinfo is not None)
    require(
        set(review["reviewed_files_sha256"]) == set(implementation_paths())
        and all(r2.r1.prior._hash(value) for value in review["reviewed_files_sha256"].values())
    )
    require(
        all(
            r2.r1.prior._hash(review[key])
            for key in (
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
            "stage33_implementation_changed",
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
    require(sha(task_raw) == TASK_FILE_SHA256, "stage33_task_evidence_changed")
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
            and len(chain["authorized_plan_identities"]) == 159
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
        common._validate_forecast(
            review["forecast"],
            stage_occupied=INHERITED_PHASE_OCCUPIED,
            correction_capacity=CORRECTION_CAPACITY,
        )
        require(
            receipt["id"] == digest({key: value for key, value in receipt.items() if key != "id"})
        )
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        raise BudgetError("stage33_receipt_invalid") from None
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
        "stage33_attempt_ownership_invalid",
    )
    counts = Counter(
        item["public_input_sha256"]
        for item in attempts + receipt["native_chain_summary"]["previous_attempts"]
    )
    require(
        len(attempts) <= MAX_REQUESTS
        and sum(counts.values()) <= CUMULATIVE_REQUEST_LIMIT
        and all(number <= MAX_ATTEMPTS for number in counts.values()),
        "stage33_attempt_limit_invalid",
    )
    return attempts, rows


def _phase_occupied(rows):
    return _money(Decimal(INHERITED_PHASE_OCCUPIED) + Decimal(common._stage_occupied(rows)))


def _revision(db):
    rows = db.execute(f"SELECT * FROM {REVISION_TABLE} ORDER BY revision_number").fetchall()
    require(bool(rows), "stage33_revision_missing")
    previous_id = None
    for number, row in enumerate(rows):
        review = json.loads(row["review_json"])
        require(
            row["revision_number"] == number
            and row["previous_revision_id"] == previous_id
            and row["id"] == digest(dict(previous_revision_id=previous_id, review=review)),
            "stage33_revision_chain_invalid",
        )
        previous_id = row["id"]
    return dict(rows[-1]), review


def registered(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone():
        return None
    rows = db.execute(f"SELECT * FROM {TABLE}").fetchall()
    require(len(rows) == 1)
    row, receipt = rows[0], _receipt_from_native_blob(rows[0]["receipt_json"])
    _equal(row, dict(id=receipt["id"], review_sha256=receipt["review_sha256"]))
    require(
        {key: value for table in NEW_TABLES for key, value in r2.r1._triggers(db, table).items()}
        == PROTECTIONS,
        "stage33_protection_invalid",
    )
    require(
        manifest(db, receipt["baseline_manifest"]) == receipt["baseline_manifest"],
        "stage33_history_changed",
    )
    attempts, _ = _ownership(db, receipt)
    revision, review = _revision(db)
    _review_valid(review, current=True)
    revision_ids = {item["id"] for item in db.execute(f"SELECT id FROM {REVISION_TABLE}")}
    require(all(item["revision_id"] in revision_ids for item in attempts))
    seals = db.execute(f"SELECT * FROM {SEAL_TABLE}").fetchall()
    require(len(seals) <= 1)
    if seals:
        _equal(
            seals[0],
            dict(
                revision_id=revision["id"],
                runtime_implementation_hash=review["runtime_implementation_hash"],
                id=digest(json.loads(seals[0]["review_json"])),
            ),
        )
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


class Stage33BudgetLedger(BudgetLedger):
    def _summary(self, db, reservation=None):
        value, receipt = super()._summary(db, reservation), registered(db)
        if receipt is None:
            old = db.execute(f"SELECT receipt_json FROM {previous.TABLE}").fetchone()
            require(old is not None, "stage33_inherited_authorization_missing")
            previous._receipt_from_native_blob(old["receipt_json"])
        rows = _ownership(db, receipt)[1] if receipt else []
        value.update(
            cap_cny="26.10000000",
            safety_cap_basis="inherited_stage31_explicit_human_authorization",
            stage33_new_occupied_cny=common._stage_occupied(rows),
            shared_stage31_and_successors_occupied_cny=_phase_occupied(rows),
            shared_phase_charge_cap_cny=PHASE_CAP,
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
        require(timedelta(0) <= age < REVIEW_TTL, "stage33_review_expired")

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

    def authorize_stage33_public_plan(self, receipt):
        validate(receipt)
        with self._transaction() as db:
            status, old = self._summary(db), registered(db)
            if old:
                require(old == receipt, "stage33_already_registered")
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
                "stage33_baseline_finance_changed",
            )
            require(
                manifest(db) == receipt["baseline_manifest"]
                and validate_native_chain(db) == receipt["native_chain_summary"],
                "stage33_baseline_changed",
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
            require(receipt is not None, "stage33_not_registered")
            return _state(db, receipt)

    def append_reviewed_revision(self, review):
        with self._transaction() as db:
            row = db.execute(f"SELECT receipt_json FROM {TABLE}").fetchone()
            require(row is not None, "stage33_not_registered")
            receipt = _receipt_from_native_blob(row[0])
            require(
                manifest(db, receipt["baseline_manifest"]) == receipt["baseline_manifest"],
                "stage33_history_changed",
            )
            require(
                not db.execute(f"SELECT 1 FROM {SEAL_TABLE}").fetchone(),
                "stage33_holdout_revision_already_sealed",
            )
            _equal(review, _state(db, receipt), "stage33_revision_baseline_changed")
            _equal(review, dict(development_case_ids=receipt["development_case_ids"]))
            self._ready(super()._summary(db))
            attempts, rows = _ownership(db, receipt)
            development, holdout = _validate_plan(
                receipt["plan"], receipt["development_case_ids"], receipt["native_chain_summary"]
            )
            require(
                all(
                    item["public_input_sha256"] not in {public_identity(case) for case in holdout}
                    for item in attempts
                ),
                "stage33_holdout_already_attempted",
            )
            counts = Counter(item["public_input_sha256"] for item in attempts)
            require(
                all(counts[public_identity(case)] < MAX_ATTEMPTS for case in development),
                "stage33_development_identity_exhausted",
            )
            capacity = sum(
                max(0, MAX_ATTEMPTS - counts[public_identity(case)] - 1) for case in receipt["plan"]
            )
            common._validate_forecast(
                review["forecast"],
                stage_occupied=_phase_occupied(rows),
                correction_capacity=capacity,
            )
            self._review_fresh(review)
            ident = self._append_version(db, receipt, review, _revision(db)[0])
            return dict(id=ident, **self._summary(db))

    def seal_holdout_revision(self, review):
        with self._transaction() as db:
            receipt = registered(db)
            require(receipt is not None, "stage33_not_registered")
            state = _state(db, receipt)
            _equal(review, state, "stage33_seal_baseline_changed")
            _equal(
                review,
                dict(
                    reviewer="independent_agent",
                    passed=True,
                    all_development_dialogues_passed_on_current_revision=True,
                    holdout_unseen_and_zero_prior_attempts=True,
                    runtime_implementation_hash=services.implementation_hash(),
                ),
                "stage33_seal_review_invalid",
            )
            require(r2.r1.prior._hash(review["development_report_sha256"]))
            age = self.clock().astimezone(UTC) - datetime.fromisoformat(
                review["checked_at"]
            ).astimezone(UTC)
            require(timedelta(0) <= age < REVIEW_TTL, "stage33_review_expired")
            self._ready(self._summary(db))
            development, holdout = _validate_plan(
                receipt["plan"], receipt["development_case_ids"], receipt["native_chain_summary"]
            )
            attempts, rows = _ownership(db, receipt)
            require(
                all(
                    item["public_input_sha256"] not in {public_identity(case) for case in holdout}
                    for item in attempts
                ),
                "stage33_holdout_already_attempted",
            )
            settled = {row["reservation_id"] for row in rows if row["state"] == "settled"}
            completed = {
                item["public_input_sha256"]
                for item in attempts
                if item["revision_id"] == state["current_revision_id"]
                and item["reservation_id"] in settled
            }
            require(
                {public_identity(case) for case in development} <= completed,
                "stage33_final_development_incomplete",
            )
            ident, existing = digest(review), db.execute(f"SELECT * FROM {SEAL_TABLE}").fetchall()
            if existing:
                require(len(existing) == 1 and existing[0]["id"] == ident)
                return dict(id=ident, reused=True)
            db.execute(
                f"INSERT INTO {SEAL_TABLE} VALUES (?,?,?,?,?)",
                (
                    ident,
                    state["current_revision_id"],
                    review["runtime_implementation_hash"],
                    json.dumps(review, ensure_ascii=False, sort_keys=True),
                    self.clock().astimezone(UTC).isoformat(),
                ),
            )
            return dict(id=ident, reused=False)

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
            "stage33_current_model_proof_required",
        )
        bounded_request.validate_current()
        bounded_request.validate()
        ip, op = (
            _decimal(input_price, minimum=INPUT_PRICE_FLOOR),
            _decimal(output_price, minimum=OUTPUT_PRICE_FLOOR),
        )
        held = _cost(input_tokens, output_tokens, ip, op)
        with self._transaction() as db:
            status = self._summary(db)
            self._ready(status)
            receipt = registered(db)
            require(receipt is not None, "stage33_not_registered")
            r2.bounded_proof_gate(self, db, bounded_request, continuation_id)
            require(
                bounded_request.profile_id == STAGE16_PROFILE_ID
                and bounded_request.public_input_sha256
                in {public_identity(case) for case in receipt["plan"]},
                "stage33_request_outside_plan",
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
                "stage33_turn_request_limit_reached",
            )
            require(
                len(attempts) < MAX_REQUESTS
                and len(attempts) + OLD_ATTEMPTS_TOTAL < CUMULATIVE_REQUEST_LIMIT
                and status["reservation_count"] < ABSOLUTE_LIMIT,
                "campaign_request_limit_reached",
            )
            _, holdout = _validate_plan(
                receipt["plan"], receipt["development_case_ids"], receipt["native_chain_summary"]
            )
            is_holdout = bounded_request.public_input_sha256 in {
                public_identity(case) for case in holdout
            }
            seal = db.execute(f"SELECT * FROM {SEAL_TABLE}").fetchone()
            require(
                seal is not None if is_holdout else seal is None,
                "stage33_holdout_revision_not_sealed"
                if is_holdout
                else "stage33_development_closed_after_blind_seal",
            )
            require(
                Decimal(_phase_occupied(rows)) + held <= Decimal(PHASE_CAP),
                "stage33_shared_phase_charge_cap_exhausted",
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
