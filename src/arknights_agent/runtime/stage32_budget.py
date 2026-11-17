"""Native successor of sealed Stage31; one inherited phase budget, no resets.

Retired source and results are checked as immutable archives, rather than as
current runtime code. Only pure manifest/cost helpers are shared with Stage31.
Registration and all live requests remain explicit collector operations.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tarfile
from collections import Counter
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from arknights_agent import services
from arknights_agent.runtime import stage31_budget as previous
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

r2 = previous.r2
Decimal, sha = previous.Decimal, previous.sha
TABLE, ATTEMPT_TABLE = "stage32_public_allowance", "stage32_request_attempt"
REVISION_TABLE, SEAL_TABLE = "stage32_reviewed_revision", "stage32_holdout_seal"
NEW_TABLES = {TABLE, ATTEMPT_TABLE, REVISION_TABLE, SEAL_TABLE}
BASELINE_COUNT, OLD_ATTEMPTS_TOTAL = 2226, 191
BASELINE_ACCOUNTED, BASELINE_UNKNOWN = "16.32001580", "9.04076800"
BASELINE_SAFE, INHERITED_PHASE_OCCUPIED = "0.73921620", "0.07995900"
PHASE_CAP, NEW_STAGE32_CHARGE_CAP = "0.50000000", "0.42004100"
PHASE_ORIGIN_COUNT, PHASE_ORIGIN_ACCOUNTED = 2210, "16.24005680"
SEND_HOLD = previous.SEND_HOLD
MAX_ATTEMPTS, MAX_REQUESTS = 3, 250
CUMULATIVE_REQUEST_LIMIT, ABSOLUTE_LIMIT = 441, 2476
TARGETED_COUNT, HOLDOUT_COUNT, CORRECTION_CAPACITY = 8, 6, 20
OLD_RUNTIME_HASH = "29bfb6c78720a1043ba235008d272f2b292434b3508b91fd350e50134127561b"
OLD_FREEZE_SHA256 = "87edf9c54a29c5da872bee77edc37ab21da31dad156dfeb5643cdf9933ca679d"
OLD_ARCHIVE_SHA256 = "00f6ca4b8510349f8f62ddd2fe8c0d0ffb3a6a33e54bf9bc1bc6eab73e76f8a6"
OLD_RESULT_MANIFEST_SHA256 = "69c183298eb43ba4961611f4997179d566c6d27f59ef086efa23d19e7d0917b4"
OLD_RESULT_ARTIFACT_COUNT = 149
AUTHORIZED_PLAN_IDENTITIES_SHA256 = (
    "2f8d23c6436bd3f0b828c13975f9d5bfe98d71e9601387f63be89e3b20c8077d"
)
TASK_FILE_SHA256 = "ff5945712c24c37d9092bdd35c79d987aecb29eb569aec99b25854e669d7239f"
TASK_TEXT = "那就继续修013这两轮，重点解决结局误判为藏品、来源类型冲突后缺少纠错反馈和前轮对象继承失败。别补本地口语规则，修完用开发集和原失败对话回归，再建新保留集做真实API盲测。累计上限仍为32元、安全线26.10元，本阶段新增保守计提仍不超过0.50元，不重置计数，旧挂账和全额预留保留。开发集和保留集的结果、费用分开统计，未测和没修好的如实列出，查不到的别编。"
PLAN_SHA256 = "93db4c1575372234b40e420f5b34c9e56d19bfde8876b69abcd505447726014f"
REVIEW_TTL = previous.REVIEW_TTL
REVIEW_SAFEGUARDS = (
    "direct_current_task_and_inherited_half_yuan_scope_verified",
    "old_stage31_archive_result_and_all_native_rows_unchanged",
    "old_153_scope_and_all_identity_counters_preserved",
    "eight_seen_development_inputs_have_one_prior_attempt_each",
    "six_genuinely_new_blind_inputs_have_zero_prior_attempts",
    "source_oracle_split_and_current_model_sources_verified",
    "all_revisions_share_original_2210_phase_baseline",
    "same_final_revision_development_then_new_blind_enforced",
    "original_full_send_and_unknown_escalation_retained",
    "all_reachable_remaining_correction_slots_forecast",
    "nominal_32_and_safety_26_10_unchanged_without_recharge",
    "no_automatic_retry_or_counter_reset",
)
PROTECTIONS = {
    f"stage32_{table}_{op.lower()}": f"CREATE TRIGGER stage32_{table}_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT, 'stage32_append_only'); END"
    for table in NEW_TABLES
    for op in ("UPDATE", "DELETE")
}


def require(value, code="stage32_receipt_invalid"):
    if not value:
        raise BudgetError(code)


def implementation_paths():
    return (
        *previous.implementation_paths(),
        "src/arknights_agent/runtime/stage32_budget.py",
        "tests/test_stage32_budget.py",
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
    raw = (root / ".local/stage31-initial-code-freeze.json").read_text()
    require(sha(raw) == OLD_FREEZE_SHA256, "stage32_previous_code_freeze_changed")
    frozen = json.loads(raw)
    require(
        frozen["runtime_implementation_hash"] == OLD_RUNTIME_HASH
        and frozen["archive_sha256"] == OLD_ARCHIVE_SHA256
    )
    archive = root / frozen["archive_path"]
    require(
        hashlib.sha256(archive.read_bytes()).hexdigest() == OLD_ARCHIVE_SHA256,
        "stage32_previous_code_archive_changed",
    )
    with tarfile.open(archive, "r:gz") as packed:
        members = {item.name: item for item in packed.getmembers() if item.isfile()}
        require(set(members) == set(frozen["file_hashes"]))
        for name, expected in frozen["file_hashes"].items():
            stream = packed.extractfile(members[name])
            require(
                stream is not None and hashlib.sha256(stream.read()).hexdigest() == expected,
                "stage32_previous_archive_source_changed",
            )
    return frozen


def _verify_old_results():
    root = r2.r1.prior.SOURCE_ROOT
    raw = (root / ".local/stage31-result-freeze.json").read_text()
    require(sha(raw) == OLD_RESULT_MANIFEST_SHA256, "stage32_previous_result_manifest_changed")
    result = json.loads(raw)
    require(
        result["runtime_implementation_hash"] == OLD_RUNTIME_HASH
        and len(result["artifact_sha256"]) == OLD_RESULT_ARTIFACT_COUNT
    )
    for relative, expected in result["artifact_sha256"].items():
        path = (root / relative).resolve()
        require(
            path.is_relative_to(root)
            and path.is_file()
            and hashlib.sha256(path.read_bytes()).hexdigest() == expected,
            "stage32_previous_result_artifact_changed",
        )
    return OLD_RESULT_MANIFEST_SHA256


def validate_native_chain(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone():
        receipt = _receipt_from_native_blob(
            db.execute(f"SELECT receipt_json FROM {TABLE}").fetchone()[0]
        )
        require(
            manifest(db, receipt["baseline_manifest"]) == receipt["baseline_manifest"],
            "stage32_history_changed",
        )
        _verify_retired_code_archive()
        _verify_old_results()
        return receipt["native_chain_summary"]
    old_rows = db.execute(f"SELECT * FROM {previous.TABLE}").fetchall()
    require(len(old_rows) == 1, "stage32_retired_registration_invalid")
    row = old_rows[0]
    old = previous._receipt_from_native_blob(row["receipt_json"])
    require(row["id"] == old["id"] and row["review_sha256"] == old["review_sha256"])
    require(
        previous.validate_native_chain(db) == old["native_chain_summary"],
        "stage32_old_native_chain_changed",
    )
    require(
        previous.manifest(db, old["baseline_manifest"]) == old["baseline_manifest"],
        "stage32_retired_history_changed",
    )
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
        and seals[0]["id"] == digest(json.loads(seals[0]["review_json"])),
        "stage32_retired_seal_invalid",
    )
    attempts, rows = previous._ownership(db, old)
    require(
        len(attempts) == 16
        and previous._stage_occupied(rows) == INHERITED_PHASE_OCCUPIED
        and all(item["state"] == "settled" for item in rows)
    )
    require(
        {item["public_input_sha256"] for item in attempts}
        == {public_identity(case) for case in old["plan"]}
    )
    identities = old["native_chain_summary"]["authorized_plan_identities"] + [
        {"case_id": case["case_id"], "public_input_sha256": public_identity(case)}
        for case in old["plan"]
        if case["case_id"] in old["development_case_ids"]
    ]
    require(len(identities) == 153 and digest(identities) == AUTHORIZED_PLAN_IDENTITIES_SHA256)
    all_attempts = old["native_chain_summary"]["previous_attempts"] + [
        {key: item[key] for key in ("reservation_id", "allowance_id", "public_input_sha256")}
        for item in attempts
    ]
    require(len(all_attempts) == OLD_ATTEMPTS_TOTAL)
    _verify_retired_code_archive()
    return {
        "previous_receipt_id": old["id"],
        "previous_receipt_json_sha256": sha(row["receipt_json"]),
        "previous_runtime_hash": OLD_RUNTIME_HASH,
        "previous_code_freeze_sha256": OLD_FREEZE_SHA256,
        "previous_code_archive_sha256": OLD_ARCHIVE_SHA256,
        "previous_result_manifest_sha256": _verify_old_results(),
        "authorized_plan_identities": identities,
        "previous_attempts": sorted(
            all_attempts, key=lambda item: (item["allowance_id"], item["reservation_id"])
        ),
        "inherited_phase_occupied_cny": INHERITED_PHASE_OCCUPIED,
    }


def _validate_plan(plan, development_ids, chain):
    require(
        digest(plan) == PLAN_SHA256
        and len(plan) == TARGETED_COUNT + HOLDOUT_COUNT
        and len(development_ids) == TARGETED_COUNT
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
        == len(plan)
        and set(development_ids) <= {case["case_id"] for case in plan}
    )
    development = [case for case in plan if case["case_id"] in development_ids]
    holdout = [case for case in plan if case["case_id"] not in development_ids]
    old_ids = {item["public_input_sha256"] for item in chain["authorized_plan_identities"]}
    counts = Counter(item["public_input_sha256"] for item in chain["previous_attempts"])
    require(
        all(
            public_identity(case) in old_ids and counts[public_identity(case)] == 1
            for case in development
        ),
        "stage32_seen_development_scope_invalid",
    )
    require(
        all(
            public_identity(case) not in old_ids and counts[public_identity(case)] == 0
            for case in holdout
        ),
        "stage32_new_holdout_scope_invalid",
    )
    require(
        previous._dialogue_lengths(development) == [2, 3, 3]
        and previous._dialogue_lengths(holdout) == [2, 2, 2],
        "stage32_complete_dialogue_plan_invalid",
    )
    return development, holdout


def _review_valid(review, current=False):
    require(
        review["reviewer"] == "independent_agent"
        and review["passed"] is True
        and all(review[key] is True for key in REVIEW_SAFEGUARDS)
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
        require(
            review["reviewed_files_sha256"] == implementation_hashes(),
            "stage32_implementation_changed",
        )
        require(
            review["runtime_implementation_hash"] == services.implementation_hash(),
            "stage32_runtime_changed",
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
    return {
        "reviewer": "independent_agent",
        "passed": False,
        "checked_at": None,
        "baseline_manifest_sha256": digest(baseline),
        "plan_sha256": digest(plan),
        "native_chain_summary_sha256": digest(chain),
        "task_evidence_file_sha256": TASK_FILE_SHA256,
        "development_case_ids": list(development_case_ids),
        "forecast": copy.deepcopy(forecast),
        "split_evidence_sha256": split_evidence_sha256,
        "forecast_evidence_sha256": forecast_evidence_sha256,
        "reviewed_files_sha256": implementation_hashes(),
        "runtime_implementation_hash": services.implementation_hash(),
        **dict.fromkeys(REVIEW_SAFEGUARDS, False),
    }


def build_receipt(
    baseline, plan, chain, human_task_path, independent_review_path, *, development_case_ids
):
    task_raw, review_raw = (
        Path(human_task_path).read_text(),
        Path(independent_review_path).read_text(),
    )
    require(sha(task_raw) == TASK_FILE_SHA256, "stage32_task_evidence_changed")
    value = {
        "campaign_id": CAMPAIGN_ID,
        "task_evidence": json.loads(task_raw),
        "task_evidence_file_sha256": TASK_FILE_SHA256,
        "baseline_manifest": copy.deepcopy(baseline),
        "baseline_manifest_sha256": digest(baseline),
        "native_chain_summary": copy.deepcopy(chain),
        "native_chain_summary_sha256": digest(chain),
        "plan": copy.deepcopy(plan),
        "plan_sha256": digest(plan),
        "development_case_ids": list(development_case_ids),
        "baseline_count": BASELINE_COUNT,
        "budget_cny": "32.00000000",
        "cap_cny": "26.10000000",
        "phase_origin_count": PHASE_ORIGIN_COUNT,
        "phase_origin_accounted_cny": PHASE_ORIGIN_ACCOUNTED,
        "shared_phase_cap_cny": PHASE_CAP,
        "inherited_phase_occupied_cny": INHERITED_PHASE_OCCUPIED,
        "new_stage32_charge_cap_cny": NEW_STAGE32_CHARGE_CAP,
        "unknown_costs_released": False,
        "max_attempts_per_turn": MAX_ATTEMPTS,
        "max_additional_requests": MAX_REQUESTS,
        "cumulative_stage23_request_limit": CUMULATIVE_REQUEST_LIMIT,
        "absolute_limit": ABSOLUTE_LIMIT,
        "independent_review_json": review_raw,
        "review_sha256": sha(review_raw),
    }
    value["id"] = digest(value)
    return validate(value)


def validate(receipt):
    try:
        require(
            receipt["campaign_id"] == CAMPAIGN_ID
            and receipt["baseline_count"] == BASELINE_COUNT
            and receipt["budget_cny"] == "32.00000000"
            and receipt["cap_cny"] == "26.10000000"
            and receipt["phase_origin_count"] == PHASE_ORIGIN_COUNT
            and receipt["phase_origin_accounted_cny"] == PHASE_ORIGIN_ACCOUNTED
            and receipt["shared_phase_cap_cny"] == PHASE_CAP
            and receipt["inherited_phase_occupied_cny"] == INHERITED_PHASE_OCCUPIED
            and receipt["new_stage32_charge_cap_cny"] == NEW_STAGE32_CHARGE_CAP
            and receipt["unknown_costs_released"] is False
            and receipt["max_attempts_per_turn"] == MAX_ATTEMPTS
            and receipt["max_additional_requests"] == MAX_REQUESTS
            and receipt["cumulative_stage23_request_limit"] == CUMULATIVE_REQUEST_LIMIT
            and receipt["absolute_limit"] == ABSOLUTE_LIMIT
        )
        task = receipt["task_evidence"]
        require(
            task["source"] == "direct_current_user_message"
            and task["task"] == TASK_TEXT
            and task["shared_stage31_and_successors_conservative_charge_cap_cny"] == PHASE_CAP
            and task["shared_phase_baseline_reservation_count"] == PHASE_ORIGIN_COUNT
            and task["shared_phase_baseline_accounted_cny"] == PHASE_ORIGIN_ACCOUNTED
            and task["stage31_already_conservative_cny"] == INHERITED_PHASE_OCCUPIED
            and task["new_stage32_conservative_available_cny"] == NEW_STAGE32_CHARGE_CAP
            and task["nominal_cumulative_cap_cny"] == "32.00000000"
            and task["effective_safety_cap_cny"] == "26.10000000"
            and task["no_counter_reset"] is True
            and task["no_recharge"] is True
            and task["preserve_old_unknowns_and_full_request_reservation"] is True
            and receipt["task_evidence_file_sha256"] == TASK_FILE_SHA256
            and task["previous_result_manifest"]
            == {"path": ".local/stage31-result-freeze.json", "sha256": OLD_RESULT_MANIFEST_SHA256}
        )
        baseline, chain = receipt["baseline_manifest"], receipt["native_chain_summary"]
        require(
            baseline["encoding"] == "exact_sqlite_typed_row_sha256_v1"
            and baseline["schema_sha256"] == digest(baseline["schema"])
            and baseline["finance"]
            == {
                "reservation_count": BASELINE_COUNT,
                "accounted_cny": BASELINE_ACCOUNTED,
                "unknown_hold_cny": BASELINE_UNKNOWN,
                "unknown_count": 3,
                "inflight_count": 0,
            }
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
            and len(chain["authorized_plan_identities"]) == 153
            and digest(chain["authorized_plan_identities"]) == AUTHORIZED_PLAN_IDENTITIES_SHA256
            and len(chain["previous_attempts"]) == OLD_ATTEMPTS_TOTAL
            and len({item["reservation_id"] for item in chain["previous_attempts"]})
            == OLD_ATTEMPTS_TOTAL
        )
        require(
            chain["previous_runtime_hash"] == OLD_RUNTIME_HASH
            and chain["previous_code_freeze_sha256"] == OLD_FREEZE_SHA256
            and chain["previous_code_archive_sha256"] == OLD_ARCHIVE_SHA256
            and chain["previous_result_manifest_sha256"] == OLD_RESULT_MANIFEST_SHA256
            and chain["inherited_phase_occupied_cny"] == INHERITED_PHASE_OCCUPIED
        )
        _validate_plan(receipt["plan"], receipt["development_case_ids"], chain)
        require(
            receipt["baseline_manifest_sha256"] == digest(baseline)
            and receipt["native_chain_summary_sha256"] == digest(chain)
            and receipt["plan_sha256"] == digest(receipt["plan"])
        )
        raw, review = (
            receipt["independent_review_json"],
            json.loads(receipt["independent_review_json"]),
        )
        require(sha(raw) == receipt["review_sha256"])
        _review_valid(review)
        require(
            all(
                review[key] == receipt[key]
                for key in (
                    "baseline_manifest_sha256",
                    "plan_sha256",
                    "native_chain_summary_sha256",
                    "task_evidence_file_sha256",
                    "development_case_ids",
                )
            )
        )
        previous._validate_forecast(
            review["forecast"],
            stage_occupied=INHERITED_PHASE_OCCUPIED,
            correction_capacity=CORRECTION_CAPACITY,
        )
        require(
            receipt["id"] == digest({key: value for key, value in receipt.items() if key != "id"})
        )
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        raise BudgetError("stage32_receipt_invalid") from None
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
        "stage32_attempt_ownership_invalid",
    )
    counts = Counter(
        item["public_input_sha256"]
        for item in attempts + receipt["native_chain_summary"]["previous_attempts"]
    )
    require(
        len(attempts) <= MAX_REQUESTS
        and sum(counts.values()) <= CUMULATIVE_REQUEST_LIMIT
        and all(number <= MAX_ATTEMPTS for number in counts.values()),
        "stage32_attempt_limit_invalid",
    )
    return attempts, rows


def _phase_occupied(rows):
    return _money(Decimal(INHERITED_PHASE_OCCUPIED) + Decimal(previous._stage_occupied(rows)))


def _revision(db):
    rows = db.execute(f"SELECT * FROM {REVISION_TABLE} ORDER BY revision_number").fetchall()
    require(bool(rows), "stage32_revision_missing")
    previous_id = None
    for number, row in enumerate(rows):
        review = json.loads(row["review_json"])
        require(
            row["revision_number"] == number
            and row["previous_revision_id"] == previous_id
            and row["id"] == digest({"previous_revision_id": previous_id, "review": review}),
            "stage32_revision_chain_invalid",
        )
        previous_id = row["id"]
    return dict(rows[-1]), review


def registered(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone():
        return None
    rows = db.execute(f"SELECT * FROM {TABLE}").fetchall()
    require(len(rows) == 1, "stage32_registration_invalid")
    row = rows[0]
    receipt = _receipt_from_native_blob(row["receipt_json"])
    require(row["id"] == receipt["id"] and row["review_sha256"] == receipt["review_sha256"])
    require(
        {key: value for table in NEW_TABLES for key, value in r2.r1._triggers(db, table).items()}
        == PROTECTIONS,
        "stage32_protection_invalid",
    )
    require(
        manifest(db, receipt["baseline_manifest"]) == receipt["baseline_manifest"],
        "stage32_history_changed",
    )
    attempts, _ = _ownership(db, receipt)
    revision, review = _revision(db)
    _review_valid(review, current=True)
    revision_ids = {item["id"] for item in db.execute(f"SELECT id FROM {REVISION_TABLE}")}
    require(all(item["revision_id"] in revision_ids for item in attempts))
    seals = db.execute(f"SELECT * FROM {SEAL_TABLE}").fetchall()
    require(len(seals) <= 1)
    if seals:
        seal = seals[0]
        require(
            seal["revision_id"] == revision["id"]
            and seal["runtime_implementation_hash"] == review["runtime_implementation_hash"]
            and seal["id"] == digest(json.loads(seal["review_json"])),
            "stage32_holdout_seal_invalid",
        )
    return receipt


def _state(db, receipt):
    attempts, rows = _ownership(db, receipt)
    revision, _ = _revision(db)
    return {
        "allowance_id": receipt["id"],
        "current_revision_id": revision["id"],
        "stage_attempts_sha256": digest(attempts),
        "stage_occupied_cny": _phase_occupied(rows),
    }


def revision_review_template(
    state, forecast, *, development_case_ids, split_evidence_sha256, forecast_evidence_sha256
):
    return {
        "reviewer": "independent_agent",
        "passed": False,
        "checked_at": None,
        **copy.deepcopy(state),
        "development_case_ids": list(development_case_ids),
        "forecast": copy.deepcopy(forecast),
        "split_evidence_sha256": split_evidence_sha256,
        "forecast_evidence_sha256": forecast_evidence_sha256,
        "reviewed_files_sha256": implementation_hashes(),
        "runtime_implementation_hash": services.implementation_hash(),
        **dict.fromkeys(REVIEW_SAFEGUARDS, False),
    }


class Stage32BudgetLedger(BudgetLedger):
    def _summary(self, db, reservation=None):
        value = super()._summary(db, reservation)
        receipt = registered(db)
        if receipt is None:
            old = db.execute(f"SELECT receipt_json FROM {previous.TABLE}").fetchone()
            require(old is not None, "stage32_inherited_safety_authorization_missing")
            previous._receipt_from_native_blob(old["receipt_json"])
        value.update(
            cap_cny="26.10000000", safety_cap_basis="inherited_stage31_explicit_human_authorization"
        )
        value["remaining_cny"] = _money(
            max(
                Decimal(0),
                Decimal(value["cap_cny"])
                - Decimal(value["accounted_cny"])
                - Decimal(value["reserved_cny"]),
            )
        )
        occupied = (
            _phase_occupied(_ownership(db, receipt)[1])
            if receipt is not None
            else INHERITED_PHASE_OCCUPIED
        )
        value.update(
            stage32_new_occupied_cny=previous._stage_occupied(_ownership(db, receipt)[1])
            if receipt is not None
            else "0.00000000",
            shared_stage31_and_successors_occupied_cny=occupied,
            shared_phase_charge_cap_cny=PHASE_CAP,
        )
        return value

    def _review_fresh(self, review):
        _review_valid(review, current=True)
        age = self.clock().astimezone(UTC) - datetime.fromisoformat(
            review["checked_at"]
        ).astimezone(UTC)
        require(previous.timedelta(0) <= age < REVIEW_TTL, "stage32_review_expired")

    def authorize_stage32_public_plan(self, receipt):
        validate(receipt)
        with self._transaction() as db:
            status = self._summary(db)
            old = registered(db)
            if old is not None:
                require(old == receipt, "stage32_already_registered")
                return {"id": receipt["id"], "reused": True, **status}
            require(
                not status["frozen"] and status["inflight_count"] == 0,
                "stage32_registration_not_quiescent",
            )
            require(
                status["price_verification_status"] == "ready", status["price_verification_status"]
            )
            require(
                status["reservation_count"] == BASELINE_COUNT
                and status["accounted_cny"] == BASELINE_ACCOUNTED
                and status["reserved_cny"] == BASELINE_UNKNOWN
                and status["remaining_cny"] == BASELINE_SAFE,
                "stage32_baseline_finance_changed",
            )
            require(
                manifest(db) == receipt["baseline_manifest"]
                and validate_native_chain(db) == receipt["native_chain_summary"],
                "stage32_baseline_changed",
            )
            review = json.loads(receipt["independent_review_json"])
            self._review_fresh(review)
            db.execute(
                f"CREATE TABLE {TABLE} (id TEXT PRIMARY KEY, review_sha256 TEXT NOT NULL UNIQUE, receipt_json TEXT NOT NULL, authorized_at TEXT NOT NULL)"
            )
            db.execute(
                f"CREATE TABLE {REVISION_TABLE} (id TEXT PRIMARY KEY, revision_number INTEGER NOT NULL UNIQUE, previous_revision_id TEXT REFERENCES {REVISION_TABLE}(id), allowance_id TEXT NOT NULL REFERENCES {TABLE}(id), review_json TEXT NOT NULL, authorized_at TEXT NOT NULL)"
            )
            db.execute(
                f"CREATE TABLE {ATTEMPT_TABLE} (reservation_id TEXT PRIMARY KEY REFERENCES reservation(reservation_id), allowance_id TEXT NOT NULL REFERENCES {TABLE}(id), revision_id TEXT NOT NULL REFERENCES {REVISION_TABLE}(id), public_input_sha256 TEXT NOT NULL)"
            )
            db.execute(
                f"CREATE TABLE {SEAL_TABLE} (id TEXT PRIMARY KEY, revision_id TEXT NOT NULL UNIQUE REFERENCES {REVISION_TABLE}(id), runtime_implementation_hash TEXT NOT NULL, review_json TEXT NOT NULL, sealed_at TEXT NOT NULL)"
            )
            for sql in PROTECTIONS.values():
                db.execute(sql)
            now = self.clock().astimezone(UTC).isoformat()
            db.execute(
                f"INSERT INTO {TABLE} VALUES (?,?,?,?)",
                (
                    receipt["id"],
                    receipt["review_sha256"],
                    json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            revision_id = digest({"previous_revision_id": None, "review": review})
            db.execute(
                f"INSERT INTO {REVISION_TABLE} VALUES (?,?,?,?,?,?)",
                (
                    revision_id,
                    0,
                    None,
                    receipt["id"],
                    json.dumps(review, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            return {"id": receipt["id"], "reused": False, **self._summary(db)}

    def revision_state(self):
        with self._transaction() as db:
            receipt = registered(db)
            require(receipt is not None, "stage32_not_registered")
            return _state(db, receipt)

    def append_reviewed_revision(self, review):
        with self._transaction() as db:
            receipt = _receipt_from_native_blob(
                db.execute(f"SELECT receipt_json FROM {TABLE}").fetchone()[0]
            )
            require(
                manifest(db, receipt["baseline_manifest"]) == receipt["baseline_manifest"],
                "stage32_history_changed",
            )
            require(
                not db.execute(f"SELECT 1 FROM {SEAL_TABLE}").fetchone(),
                "stage32_holdout_revision_already_sealed",
            )
            require(
                all(review[key] == value for key, value in _state(db, receipt).items()),
                "stage32_revision_baseline_changed",
            )
            require(review["development_case_ids"] == receipt["development_case_ids"])
            status = super()._summary(db)
            require(
                not status["frozen"] and status["inflight_count"] == 0,
                "stage32_revision_not_quiescent",
            )
            require(
                status["price_verification_status"] == "ready", status["price_verification_status"]
            )
            attempts, rows = _ownership(db, receipt)
            development, holdout = _validate_plan(
                receipt["plan"], receipt["development_case_ids"], receipt["native_chain_summary"]
            )
            require(
                all(
                    item["public_input_sha256"] not in {public_identity(case) for case in holdout}
                    for item in attempts
                ),
                "stage32_holdout_already_attempted",
            )
            counts = Counter(
                item["public_input_sha256"]
                for item in attempts + receipt["native_chain_summary"]["previous_attempts"]
            )
            require(
                all(counts[public_identity(case)] < MAX_ATTEMPTS for case in development),
                "stage32_development_identity_exhausted",
            )
            capacity = sum(
                max(0, MAX_ATTEMPTS - counts[public_identity(case)] - 1) for case in receipt["plan"]
            )
            previous._validate_forecast(
                review["forecast"],
                stage_occupied=_phase_occupied(rows),
                correction_capacity=capacity,
            )
            self._review_fresh(review)
            prior, _ = _revision(db)
            ident = digest({"previous_revision_id": prior["id"], "review": review})
            db.execute(
                f"INSERT INTO {REVISION_TABLE} VALUES (?,?,?,?,?,?)",
                (
                    ident,
                    prior["revision_number"] + 1,
                    prior["id"],
                    receipt["id"],
                    json.dumps(review, ensure_ascii=False, sort_keys=True),
                    self.clock().astimezone(UTC).isoformat(),
                ),
            )
            return {"id": ident, **self._summary(db)}

    def seal_holdout_revision(self, review):
        with self._transaction() as db:
            receipt = registered(db)
            require(receipt is not None, "stage32_not_registered")
            state = _state(db, receipt)
            require(
                all(review[key] == value for key, value in state.items()),
                "stage32_seal_baseline_changed",
            )
            require(
                review["reviewer"] == "independent_agent"
                and review["passed"] is True
                and review["all_development_dialogues_passed_on_current_revision"] is True
                and review["holdout_unseen_and_zero_prior_attempts"] is True
                and r2.r1.prior._hash(review["development_report_sha256"])
                and review["runtime_implementation_hash"] == services.implementation_hash(),
                "stage32_seal_review_invalid",
            )
            age = self.clock().astimezone(UTC) - datetime.fromisoformat(
                review["checked_at"]
            ).astimezone(UTC)
            require(previous.timedelta(0) <= age < REVIEW_TTL, "stage32_review_expired")
            status = self._summary(db)
            require(
                not status["frozen"] and status["inflight_count"] == 0, "stage32_seal_not_quiescent"
            )
            development, holdout = _validate_plan(
                receipt["plan"], receipt["development_case_ids"], receipt["native_chain_summary"]
            )
            attempts, rows = _ownership(db, receipt)
            require(
                all(
                    item["public_input_sha256"] not in {public_identity(case) for case in holdout}
                    for item in attempts
                ),
                "stage32_holdout_already_attempted",
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
                "stage32_final_development_incomplete",
            )
            ident = digest(review)
            existing = db.execute(f"SELECT * FROM {SEAL_TABLE}").fetchall()
            if existing:
                require(len(existing) == 1 and existing[0]["id"] == ident)
                return {"id": ident, "reused": True}
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
            return {"id": ident, "reused": False}

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
            "stage32_current_model_proof_required",
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
            require(not status["frozen"], "campaign_frozen")
            require(
                status["price_verification_status"] == "ready", status["price_verification_status"]
            )
            require(status["inflight_count"] == 0, "request_inflight")
            receipt = registered(db)
            require(receipt is not None, "stage32_not_registered")
            r2.bounded_proof_gate(self, db, bounded_request, continuation_id)
            require(
                bounded_request.profile_id == STAGE16_PROFILE_ID
                and bounded_request.public_input_sha256
                in {public_identity(case) for case in receipt["plan"]},
                "stage32_request_outside_plan",
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
                "stage32_turn_request_limit_reached",
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
            seal = db.execute(f"SELECT * FROM {SEAL_TABLE}").fetchone()
            require(
                seal is not None
                if bounded_request.public_input_sha256
                in {public_identity(case) for case in holdout}
                else seal is None,
                "stage32_holdout_revision_not_sealed"
                if bounded_request.public_input_sha256
                in {public_identity(case) for case in holdout}
                else "stage32_development_closed_after_blind_seal",
            )
            require(
                Decimal(_phase_occupied(rows)) + held <= Decimal(PHASE_CAP),
                "stage32_shared_phase_charge_cap_exhausted",
            )
            require(held <= Decimal(status["remaining_cny"]), "budget_exhausted")
            revision, _ = _revision(db)
            ident = uuid4().hex
            db.execute(
                "INSERT INTO reservation (reservation_id,campaign_id,input_tokens,output_tokens,input_price,output_price,reserved_cny,state) VALUES (?,?,?,?,?,?,?,'reserved')",
                (ident, CAMPAIGN_ID, input_tokens, output_tokens, str(ip), str(op), _money(held)),
            )
            db.execute(
                f"INSERT INTO {ATTEMPT_TABLE} VALUES (?,?,?,?)",
                (ident, receipt["id"], revision["id"], bounded_request.public_input_sha256),
            )
            return ident
