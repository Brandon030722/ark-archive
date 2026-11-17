"""Explicit Stage31 safety migration and one shared, finite evaluation phase.

No import-time registration, HTTP, campaign mutation, old-row mutation or quota
reset. This selector alone can use the human-authorized 26.10 CNY safety line.
All revisions share the same baseline and 0.50 CNY send-time spending gate.
An unknown response retains the inherited full-context hold and freezes even
when that unavoidable hold exceeds the phase ceiling.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tarfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from arknights_agent import services
from arknights_agent.runtime import stage30_budget as previous
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
TABLE = "stage31_public_allowance"
ATTEMPT_TABLE = "stage31_request_attempt"
REVISION_TABLE = "stage31_reviewed_revision"
SEAL_TABLE = "stage31_holdout_seal"
NEW_TABLES = {TABLE, ATTEMPT_TABLE, REVISION_TABLE, SEAL_TABLE}
BASELINE_COUNT = 2210
BASELINE_ACCOUNTED = "16.24005680"
BASELINE_UNKNOWN = "9.04076800"
BASELINE_SAFE = "0.81917520"
SEND_HOLD = "0.29736960"
PHASE_CAP = "0.50000000"
MAX_PROJECTED_SPEND = PHASE_CAP
MAX_ATTEMPTS = 3
MAX_REQUESTS = 266
CUMULATIVE_REQUEST_LIMIT = 441
ABSOLUTE_LIMIT = 2476
TARGETED_COUNT = 6
HOLDOUT_COUNT = 10
CORRECTION_CAPACITY = 32
PLAN_SHA256 = "f97baf60973de6212397bb84679a61bbc1df397bd2b6bc6cc2e4c0e3f2f23e89"
OLD_ATTEMPTS_TOTAL = 175
OLD_ATTEMPT_COUNTS = (8, 120, 27, 9, 4, 2, 1, 2, 1, 1)
OLD_RESULT_ARTIFACT_COUNT = 145
OLD_RUNTIME_HASH = "e1f3718eba9fd68555a69e6fa92af3f8cb08aa496e9ba72f8ec8bc4b3db38ded"
OLD_FREEZE_SHA256 = "3f8d38a2d5afdca41b504cac0c9dd6e5e3ea63e0171a7ef734d9642acd0ac823"
OLD_ARCHIVE_SHA256 = "ccb1f4420db7623d6c0422343846f18d107a6c4bf6de2f9cf9f314884b4eb436"
OLD_RESULT_MANIFEST_SHA256 = "20c97103ab02ca5c432378d9cd6fccc745effedb8baf8bc12b539fa30d5bb320"
AUTHORIZED_PLAN_IDENTITIES_SHA256 = previous.AUTHORIZED_PLAN_IDENTITIES_SHA256
TASK_FILE_SHA256 = "b1d45ebd2fcabae9a677d5b0f74c3860c7612b36d41188873e3e6bfe83c1b304"
TASK_TEXT = "那就按你说的继续，允许把内部安全线从25.60元调到26.10元，累计上限仍是32元，本阶段新增保守计提最多0.50元，不充值、不重置计数，旧挂账和全额预留保留。先做2段正向完整多轮开发测试，修好后在同一冻结版本盲测剩余5段10轮。都用真实API，不补本地口语规则，开发集和保留集的结果、费用分开统计，查不到的别编。"
REVIEW_TTL = timedelta(minutes=10)
REVIEW_SAFEGUARDS = (
    "direct_new_safety_and_phase_authorization_verified",
    "nominal_32_cap_and_no_recharge_retained",
    "all_previous_collectors_quiescent_and_frozen",
    "old_stage30_result_and_native_chain_unchanged",
    "old_147_identity_allowlist_unchanged",
    "six_genuinely_new_development_inputs_explicitly_reviewed",
    "five_original_complete_holdout_dialogues_unseen_and_zero_prior_attempts",
    "source_oracle_and_split_freezes_verified",
    "all_revisions_share_phase_baseline_and_identity_counters",
    "same_final_revision_development_and_blind_enforced",
    "bounded_source_and_continuation_gates_unchanged",
    "current_model_dual_source_proof_required_for_every_request",
    "all_remaining_correction_slots_forecast",
    "full_send_reservation_and_unknown_escalation_retained",
    "forecast_is_estimate_and_runtime_caps_stop_exhaustion",
    "no_automatic_retry_or_counter_reset",
)
PROTECTIONS = {
    f"stage31_{table}_{op.lower()}": (
        f"CREATE TRIGGER stage31_{table}_{op.lower()} BEFORE {op} ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'stage31_append_only'); END"
    )
    for table in NEW_TABLES
    for op in ("UPDATE", "DELETE")
}


def require(value, code="stage31_receipt_invalid"):
    if not value:
        raise BudgetError(code)


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def implementation_paths():
    root = r2.r1.prior.SOURCE_ROOT
    return (
        *previous.implementation_paths(),
        str(Path(__file__).resolve().relative_to(root)),
        "tests/test_stage31_budget.py",
    )


def implementation_hashes():
    root = r2.r1.prior.SOURCE_ROOT
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in implementation_paths()
    }


def manifest(db, baseline=None, *, excluded_tables=None):
    return previous.manifest(
        db, baseline, excluded_tables=NEW_TABLES if excluded_tables is None else excluded_tables
    )


def _old_modules():
    result, module = [], previous
    while True:
        result.append(module)
        if module.TABLE == "stage23_public_allowance":
            return list(reversed(result))
        module = module.previous


def _verify_retired_code_archive():
    root = r2.r1.prior.SOURCE_ROOT
    raw = (root / ".local/stage30-live-code-freeze.json").read_text()
    require(sha(raw) == OLD_FREEZE_SHA256, "stage31_previous_code_freeze_changed")
    frozen = json.loads(raw)
    archive = root / frozen["archive_path"]
    require(
        frozen["runtime_implementation_hash"] == OLD_RUNTIME_HASH
        and frozen["archive_sha256"] == OLD_ARCHIVE_SHA256
        and hashlib.sha256(archive.read_bytes()).hexdigest() == OLD_ARCHIVE_SHA256,
        "stage31_previous_code_archive_changed",
    )
    with tarfile.open(archive, "r:gz") as packed:
        members = {item.name: item for item in packed.getmembers() if item.isfile()}
        require(set(members) == set(frozen["file_hashes"]), "stage31_archive_inventory_changed")
        for name, expected in frozen["file_hashes"].items():
            stream = packed.extractfile(members[name])
            require(
                stream is not None and hashlib.sha256(stream.read()).hexdigest() == expected,
                "stage31_archive_source_changed",
            )
    previous._verify_retired_code_archive()
    return frozen


def _verify_old_results():
    root = r2.r1.prior.SOURCE_ROOT
    raw = (root / ".local/stage30-result-freeze.json").read_text()
    require(sha(raw) == OLD_RESULT_MANIFEST_SHA256, "stage31_previous_result_manifest_changed")
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
            "stage31_previous_result_artifact_changed",
        )
    return OLD_RESULT_MANIFEST_SHA256


def validate_native_chain(db):
    """Verify all ten immutable native phases before appending this selector."""
    phases = _old_modules()
    require(len(phases) == len(OLD_ATTEMPT_COUNTS))
    receipts = []
    excluded = set(NEW_TABLES)
    for module in reversed(phases):
        excluded |= {module.TABLE, module.ATTEMPT_TABLE}
        rows = db.execute(f"SELECT * FROM {module.TABLE}").fetchall()
        require(len(rows) == 1, "stage31_previous_registration_invalid")
        row = rows[0]
        native = module._receipt_from_native_blob(row["receipt_json"])
        require(row["id"] == native["id"] and row["review_sha256"] == native["review_sha256"])
        require(
            {**r2.r1._triggers(db, module.TABLE), **r2.r1._triggers(db, module.ATTEMPT_TABLE)}
            == module.PROTECTIONS,
            "stage31_previous_protection_invalid",
        )
        require(
            manifest(db, native["baseline_manifest"], excluded_tables=excluded)
            == native["baseline_manifest"],
            "stage31_previous_history_changed",
        )
        review = json.loads(native["independent_review_json"])
        require(
            review["reviewed_files_sha256"] == module.implementation_hashes(),
            "stage31_previous_budget_implementation_changed",
        )
        receipts.append((module, row, native, review))
    receipts.reverse()
    original = receipts[0][2]
    require(
        digest(original["plan"])
        == "b6bbfc61aa213bd9abd1e5c6f6873d56315a2df06a71d4037db9f74d2ff00701"
    )
    original_ids = {
        item["key"][0] for item in original["baseline_manifest"]["tables"]["reservation"]["rows"]
    }
    require(
        phases[1]._validate_earlier_native_chain(db, original_ids)
        == original["native_chain_summary"],
        "stage31_earlier_native_chain_changed",
    )
    current = {row["reservation_id"]: dict(row) for row in db.execute("SELECT * FROM reservation")}
    if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone():
        own = db.execute(f"SELECT receipt_json FROM {TABLE}").fetchone()
        pinned = json.loads(own["receipt_json"])["baseline_manifest"]["tables"]["reservation"][
            "rows"
        ]
        old_ids = {item["key"][0] for item in pinned}
        current = {key: value for key, value in current.items() if key in old_ids}
    old_attempts = []
    for index, (module, row, native, review) in enumerate(receipts):
        baseline_ids = {
            item["key"][0] for item in native["baseline_manifest"]["tables"]["reservation"]["rows"]
        }
        end_ids = (
            {
                item["key"][0]
                for item in receipts[index + 1][2]["baseline_manifest"]["tables"]["reservation"][
                    "rows"
                ]
            }
            if index + 1 < len(receipts)
            else set(current)
        )
        attempts = [dict(item) for item in db.execute(f"SELECT * FROM {module.ATTEMPT_TABLE}")]
        require(
            len(attempts) == OLD_ATTEMPT_COUNTS[index]
            and {item["reservation_id"] for item in attempts} == end_ids - baseline_ids
            and all(item["allowance_id"] == native["id"] for item in attempts),
            "stage31_previous_attempt_ownership_invalid",
        )
        expected = (
            original["plan"][:8]
            if index == 0
            else native["plan"][:110]
            if index == 1
            else native["plan"]
        )
        require(
            {item["public_input_sha256"] for item in attempts}
            == {public_identity(case) for case in expected},
            "stage31_previous_plan_coverage_invalid",
        )
        for item in attempts:
            r2.r1._reservation_shape(current[item["reservation_id"]], "settled")
        if index:
            preceding = receipts[index - 1]
            chain = native["native_chain_summary"]
            require(
                chain["previous_receipt_id"] == preceding[2]["id"]
                and chain["previous_receipt_json_sha256"] == sha(preceding[1]["receipt_json"])
                and chain["previous_native_chain_summary_sha256"]
                == preceding[2]["native_chain_summary_sha256"]
                and sorted(chain["previous_attempts"], key=lambda item: item["reservation_id"])
                == sorted(old_attempts, key=lambda item: item["reservation_id"]),
                "stage31_previous_native_link_invalid",
            )
        old_attempts.extend(attempts)
    require(
        len(old_attempts) == OLD_ATTEMPTS_TOTAL
        and all(
            n <= 3 for n in Counter(item["public_input_sha256"] for item in old_attempts).values()
        )
    )
    frozen = _verify_retired_code_archive()
    last = receipts[-1]
    require(
        last[3]["runtime_implementation_hash"] == frozen["runtime_implementation_hash"],
        "stage31_previous_runtime_archive_mismatch",
    )
    return {
        "previous_receipt_id": last[2]["id"],
        "previous_receipt_json_sha256": sha(last[1]["receipt_json"]),
        "previous_runtime_hash": OLD_RUNTIME_HASH,
        "previous_baseline_manifest_sha256": last[2]["baseline_manifest_sha256"],
        "previous_native_chain_summary_sha256": last[2]["native_chain_summary_sha256"],
        "previous_code_freeze_sha256": OLD_FREEZE_SHA256,
        "previous_code_archive_sha256": OLD_ARCHIVE_SHA256,
        "previous_result_manifest_sha256": _verify_old_results(),
        "authorized_plan_identities": [
            {"case_id": case["case_id"], "public_input_sha256": public_identity(case)}
            for case in original["plan"]
        ],
        "previous_attempts": sorted(
            old_attempts, key=lambda item: (item["allowance_id"], item["reservation_id"])
        ),
    }


def _dialogue_lengths(cases):
    groups = {}
    for case in cases:
        try:
            question = json.loads(case["question"])
        except ValueError:
            question = {"current_question": case["question"], "previous_questions": []}
        require(set(question) == {"current_question", "previous_questions"})
        literal, history = question["current_question"], question["previous_questions"]
        require(
            isinstance(literal, str)
            and literal
            and isinstance(history, list)
            and all(isinstance(item, str) and item for item in history)
        )
        key = history[0] if history else literal
        group = groups.setdefault(key, [])
        require(history == group, "stage31_incomplete_dialogue_plan")
        group.append(literal)
    return sorted(len(group) for group in groups.values())


def _validate_plan(plan, development_case_ids, chain):
    require(
        len(plan) == TARGETED_COUNT + HOLDOUT_COUNT
        and len(development_case_ids) == TARGETED_COUNT
        and digest(plan) == PLAN_SHA256
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
    )
    require(set(development_case_ids) <= {case["case_id"] for case in plan})
    development = [case for case in plan if case["case_id"] in development_case_ids]
    holdout = [case for case in plan if case["case_id"] not in development_case_ids]
    old = {item["public_input_sha256"] for item in chain["authorized_plan_identities"]}
    counts = Counter(item["public_input_sha256"] for item in chain["previous_attempts"])
    require(
        all(
            public_identity(case) not in old and counts[public_identity(case)] == 0
            for case in development
        ),
        "stage31_new_development_scope_invalid",
    )
    require(
        all(
            public_identity(case) in old and counts[public_identity(case)] == 0 for case in holdout
        ),
        "stage31_holdout_scope_invalid",
    )
    require(
        _dialogue_lengths(development) == [3, 3] and _dialogue_lengths(holdout) == [2] * 5,
        "stage31_complete_dialogue_counts_invalid",
    )
    return development, holdout


def _validate_forecast(
    forecast, *, stage_occupied="0.00000000", correction_capacity=CORRECTION_CAPACITY
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
        "stage31_correction_forecast_invalid",
    )
    projected, occupied = (
        _decimal(forecast["projected_full_round_conservative_cny"]),
        _decimal(stage_occupied),
    )
    require(occupied + projected <= Decimal(PHASE_CAP), "stage31_phase_projection_exceeds_cap")
    require(
        occupied + projected + Decimal(SEND_HOLD) <= Decimal(BASELINE_SAFE),
        "stage31_complete_round_projection_exceeds_safe_budget",
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
        "forecast_evidence_sha256": forecast_evidence_sha256,
        "split_evidence_sha256": split_evidence_sha256,
        "previous_result_manifest_sha256": OLD_RESULT_MANIFEST_SHA256,
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
    require(sha(task_raw) == TASK_FILE_SHA256, "stage31_task_evidence_changed")
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
        "baseline_accounted_cny": BASELINE_ACCOUNTED,
        "budget_cny": "32.00000000",
        "previous_cap_cny": "25.60000000",
        "cap_cny": "26.10000000",
        "phase_new_conservative_charge_cap_cny": PHASE_CAP,
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


def _validate_review(review):
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
                "forecast_evidence_sha256",
                "split_evidence_sha256",
            )
        )
    )


def validate(receipt):
    try:
        require(
            set(receipt)
            == {
                "campaign_id",
                "task_evidence",
                "task_evidence_file_sha256",
                "baseline_manifest",
                "baseline_manifest_sha256",
                "native_chain_summary",
                "native_chain_summary_sha256",
                "plan",
                "plan_sha256",
                "development_case_ids",
                "baseline_count",
                "baseline_accounted_cny",
                "budget_cny",
                "previous_cap_cny",
                "cap_cny",
                "phase_new_conservative_charge_cap_cny",
                "unknown_costs_released",
                "max_attempts_per_turn",
                "max_additional_requests",
                "cumulative_stage23_request_limit",
                "absolute_limit",
                "independent_review_json",
                "review_sha256",
                "id",
            }
        )
        require(
            receipt["campaign_id"] == CAMPAIGN_ID
            and receipt["baseline_count"] == BASELINE_COUNT
            and receipt["baseline_accounted_cny"] == BASELINE_ACCOUNTED
            and receipt["budget_cny"] == "32.00000000"
            and receipt["previous_cap_cny"] == "25.60000000"
            and receipt["cap_cny"] == "26.10000000"
            and receipt["phase_new_conservative_charge_cap_cny"] == PHASE_CAP
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
            and task["nominal_cumulative_cap_cny"] == "32.00000000"
            and task["previous_effective_safety_cap_cny"] == "25.60000000"
            and task["effective_safety_cap_cny"] == "26.10000000"
            and task["phase_new_conservative_charge_cap_cny"] == PHASE_CAP
            and task["no_counter_reset"] is True
            and task["no_recharge"] is True
            and task["preserve_old_unknowns_and_full_request_reservation"] is True
            and task["source_release_id"] == r2.r1.prior.RELEASE_ID
            and task["previous_result_manifest"]
            == {"path": ".local/stage30-result-freeze.json", "sha256": OLD_RESULT_MANIFEST_SHA256}
            and receipt["task_evidence_file_sha256"] == TASK_FILE_SHA256
        )
        baseline, chain, plan = (
            receipt["baseline_manifest"],
            receipt["native_chain_summary"],
            receipt["plan"],
        )
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
        for table in baseline["tables"].values():
            require(
                table["rows_sha256"] == digest(table["rows"])
                and table["primary_key"]
                and len({json.dumps(row["key"], sort_keys=True) for row in table["rows"]})
                == len(table["rows"])
                and all(r2.r1.prior._hash(row["sha256"]) for row in table["rows"])
            )
        require(len(baseline["tables"]["reservation"]["rows"]) == BASELINE_COUNT)
        require(
            len(chain["authorized_plan_identities"]) == 147
            and digest(chain["authorized_plan_identities"]) == AUTHORIZED_PLAN_IDENTITIES_SHA256
            and len(chain["previous_attempts"]) == OLD_ATTEMPTS_TOTAL
        )
        require(
            chain["previous_runtime_hash"] == OLD_RUNTIME_HASH
            and chain["previous_code_freeze_sha256"] == OLD_FREEZE_SHA256
            and chain["previous_code_archive_sha256"] == OLD_ARCHIVE_SHA256
            and chain["previous_result_manifest_sha256"] == OLD_RESULT_MANIFEST_SHA256
        )
        require(
            tuple(
                len(baseline["tables"][module.ATTEMPT_TABLE]["rows"]) for module in _old_modules()
            )
            == OLD_ATTEMPT_COUNTS
        )
        require(
            {item["reservation_id"] for item in chain["previous_attempts"]}
            == {
                item["key"][0]
                for module in _old_modules()
                for item in baseline["tables"][module.ATTEMPT_TABLE]["rows"]
            }
        )
        require(
            all(
                number <= MAX_ATTEMPTS
                for number in Counter(
                    item["public_input_sha256"] for item in chain["previous_attempts"]
                ).values()
            )
        )
        _validate_plan(plan, receipt["development_case_ids"], chain)
        require(
            receipt["baseline_manifest_sha256"] == digest(baseline)
            and receipt["native_chain_summary_sha256"] == digest(chain)
            and receipt["plan_sha256"] == digest(plan)
        )
        raw = receipt["independent_review_json"]
        review = json.loads(raw)
        require(sha(raw) == receipt["review_sha256"])
        require(
            set(review)
            == {
                "reviewer",
                "passed",
                "checked_at",
                "baseline_manifest_sha256",
                "plan_sha256",
                "native_chain_summary_sha256",
                "task_evidence_file_sha256",
                "development_case_ids",
                "forecast",
                "forecast_evidence_sha256",
                "split_evidence_sha256",
                "previous_result_manifest_sha256",
                "reviewed_files_sha256",
                "runtime_implementation_hash",
                *REVIEW_SAFEGUARDS,
            }
        )
        _validate_review(review)
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
            and review["previous_result_manifest_sha256"] == OLD_RESULT_MANIFEST_SHA256
        )
        _validate_forecast(review["forecast"])
        require(
            receipt["id"] == digest({key: value for key, value in receipt.items() if key != "id"})
        )
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        raise BudgetError("stage31_receipt_invalid") from None
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
    new_rows = [
        dict(row)
        for row in db.execute("SELECT * FROM reservation")
        if row["reservation_id"] not in old_ids
    ]
    allowed = {public_identity(case) for case in receipt["plan"]}
    require(
        {row["reservation_id"] for row in attempts} == {row["reservation_id"] for row in new_rows}
        and all(
            row["allowance_id"] == receipt["id"] and row["public_input_sha256"] in allowed
            for row in attempts
        ),
        "stage31_attempt_ownership_invalid",
    )
    counts = Counter(row["public_input_sha256"] for row in attempts)
    counts.update(
        item["public_input_sha256"] for item in receipt["native_chain_summary"]["previous_attempts"]
    )
    require(
        len(attempts) <= MAX_REQUESTS
        and sum(counts.values()) <= CUMULATIVE_REQUEST_LIMIT
        and all(number <= MAX_ATTEMPTS for number in counts.values())
        and BASELINE_COUNT + len(attempts) <= ABSOLUTE_LIMIT,
        "stage31_attempt_limit_invalid",
    )
    return attempts, new_rows


def _stage_occupied(rows):
    return _money(
        sum(
            (
                Decimal(row["accounted_cny"] if row["state"] == "settled" else row["reserved_cny"])
                for row in rows
            ),
            Decimal(0),
        )
    )


def _revision(db):
    rows = db.execute(f"SELECT * FROM {REVISION_TABLE} ORDER BY revision_number").fetchall()
    require(bool(rows), "stage31_revision_missing")
    previous_id = None
    for number, row in enumerate(rows):
        review = json.loads(row["review_json"])
        require(
            row["revision_number"] == number
            and row["previous_revision_id"] == previous_id
            and row["id"] == digest({"previous_revision_id": previous_id, "review": review}),
            "stage31_revision_chain_invalid",
        )
        previous_id = row["id"]
    return dict(rows[-1]), json.loads(rows[-1]["review_json"])


def registered(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone():
        return None
    rows = db.execute(f"SELECT * FROM {TABLE}").fetchall()
    require(len(rows) == 1, "stage31_registration_invalid")
    row = rows[0]
    receipt = _receipt_from_native_blob(row["receipt_json"])
    require(
        row["id"] == receipt["id"] and row["review_sha256"] == receipt["review_sha256"],
        "stage31_registration_invalid",
    )
    actual = {
        key: value for table in NEW_TABLES for key, value in r2.r1._triggers(db, table).items()
    }
    require(actual == PROTECTIONS, "stage31_protection_invalid")
    require(
        manifest(db, receipt["baseline_manifest"]) == receipt["baseline_manifest"],
        "stage31_history_changed",
    )
    attempts, _ = _ownership(db, receipt)
    revision, review = _revision(db)
    require(
        review["reviewed_files_sha256"] == implementation_hashes(), "stage31_implementation_changed"
    )
    require(
        review["runtime_implementation_hash"] == services.implementation_hash(),
        "stage31_runtime_changed",
    )
    revision_ids = {row["id"] for row in db.execute(f"SELECT id FROM {REVISION_TABLE}")}
    require(
        all(row["revision_id"] in revision_ids for row in attempts),
        "stage31_attempt_revision_invalid",
    )
    seals = db.execute(f"SELECT * FROM {SEAL_TABLE}").fetchall()
    require(len(seals) <= 1, "stage31_holdout_seal_invalid")
    if seals:
        seal = dict(seals[0])
        require(
            seal["revision_id"] == revision["id"]
            and seal["runtime_implementation_hash"] == review["runtime_implementation_hash"]
            and seal["id"] == digest(json.loads(seal["review_json"])),
            "stage31_holdout_seal_invalid",
        )
    return receipt


def _state(db, receipt):
    attempts, rows = _ownership(db, receipt)
    revision, _ = _revision(db)
    return {
        "allowance_id": receipt["id"],
        "current_revision_id": revision["id"],
        "stage_attempts_sha256": digest(attempts),
        "stage_occupied_cny": _stage_occupied(rows),
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


class Stage31BudgetLedger(BudgetLedger):
    def _summary(self, db, reservation=None):
        value = super()._summary(db, reservation)
        receipt = registered(db)
        if receipt is not None:
            value["cap_cny"] = receipt["cap_cny"]
            value["remaining_cny"] = _money(
                max(
                    Decimal(0),
                    Decimal(value["cap_cny"])
                    - Decimal(value["accounted_cny"])
                    - Decimal(value["reserved_cny"]),
                )
            )
            _, rows = _ownership(db, receipt)
            value.update(
                stage31_new_occupied_cny=_stage_occupied(rows),
                stage31_charge_cap_cny=PHASE_CAP,
                safety_cap_basis="explicit_stage31_human_authorization",
            )
        return value

    def _review_fresh(self, review):
        age = self.clock().astimezone(UTC) - datetime.fromisoformat(
            review["checked_at"]
        ).astimezone(UTC)
        require(timedelta(0) <= age < REVIEW_TTL, "stage31_review_expired")
        require(
            review["reviewed_files_sha256"] == implementation_hashes(),
            "stage31_implementation_changed",
        )
        require(
            review["runtime_implementation_hash"] == services.implementation_hash(),
            "stage31_runtime_changed",
        )

    def authorize_stage31_public_plan(self, receipt):
        validate(receipt)
        with self._transaction() as db:
            status = self._summary(db)
            old = registered(db)
            if old is not None:
                require(old == receipt, "stage31_already_registered")
                return {"id": receipt["id"], "reused": True, **status}
            require(not status["frozen"], "campaign_frozen")
            require(status["inflight_count"] == 0, "request_inflight")
            require(
                status["price_verification_status"] == "ready", status["price_verification_status"]
            )
            require(
                status["budget_cny"] == "32.00000000"
                and status["cap_cny"] == "25.60000000"
                and status["remaining_cny"] == "0.31917520",
                "stage31_baseline_finance_changed",
            )
            require(manifest(db) == receipt["baseline_manifest"], "stage31_baseline_changed")
            require(
                validate_native_chain(db) == receipt["native_chain_summary"],
                "stage31_native_chain_changed",
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
            require(receipt is not None, "stage31_not_registered")
            return _state(db, receipt)

    def append_reviewed_revision(self, review):
        """Append reviewed code after dev repair; never reset phase/case counts."""
        _validate_review(review)
        with self._transaction() as db:
            # The old revision deliberately pins retired product code; read its
            # immutable receipt/history without requiring it to be current.
            row = db.execute(f"SELECT receipt_json FROM {TABLE}").fetchone()
            require(row is not None, "stage31_not_registered")
            receipt = _receipt_from_native_blob(row["receipt_json"])
            require(
                manifest(db, receipt["baseline_manifest"]) == receipt["baseline_manifest"],
                "stage31_history_changed",
            )
            require(
                not db.execute(f"SELECT 1 FROM {SEAL_TABLE}").fetchone(),
                "stage31_holdout_revision_already_sealed",
            )
            state = _state(db, receipt)
            require(
                all(review[key] == value for key, value in state.items()),
                "stage31_revision_baseline_changed",
            )
            require(review["development_case_ids"] == receipt["development_case_ids"])
            status = super()._summary(db)
            require(
                not status["frozen"] and status["inflight_count"] == 0,
                "stage31_revision_not_quiescent",
            )
            require(
                status["price_verification_status"] == "ready", status["price_verification_status"]
            )
            attempts, rows = _ownership(db, receipt)
            development, holdout = _validate_plan(
                receipt["plan"], receipt["development_case_ids"], receipt["native_chain_summary"]
            )
            held_ids = {public_identity(case) for case in holdout}
            require(
                all(item["public_input_sha256"] not in held_ids for item in attempts),
                "stage31_holdout_already_attempted",
            )
            counts = Counter(item["public_input_sha256"] for item in attempts)
            require(
                all(counts[public_identity(case)] < MAX_ATTEMPTS for case in development),
                "stage31_development_identity_exhausted",
            )
            correction_capacity = sum(
                max(0, MAX_ATTEMPTS - counts[public_identity(case)] - 1) for case in receipt["plan"]
            )
            _validate_forecast(
                review["forecast"],
                stage_occupied=_stage_occupied(rows),
                correction_capacity=correction_capacity,
            )
            self._review_fresh(review)
            previous_revision, _ = _revision(db)
            ident = digest({"previous_revision_id": previous_revision["id"], "review": review})
            db.execute(
                f"INSERT INTO {REVISION_TABLE} VALUES (?,?,?,?,?,?)",
                (
                    ident,
                    previous_revision["revision_number"] + 1,
                    previous_revision["id"],
                    receipt["id"],
                    json.dumps(review, ensure_ascii=False, sort_keys=True),
                    self.clock().astimezone(UTC).isoformat(),
                ),
            )
            registered(db)
            return {"id": ident, **self._summary(db)}

    def seal_holdout_revision(self, review):
        """Lock a proven final dev revision before the first blind request."""
        with self._transaction() as db:
            receipt = registered(db)
            require(receipt is not None, "stage31_not_registered")
            state = _state(db, receipt)
            require(
                all(review[key] == value for key, value in state.items()),
                "stage31_seal_baseline_changed",
            )
            require(
                review["reviewer"] == "independent_agent"
                and review["passed"] is True
                and review["all_development_dialogues_passed_on_current_revision"] is True
                and review["holdout_unseen_and_zero_prior_attempts"] is True
                and r2.r1.prior._hash(review["development_report_sha256"])
                and review["runtime_implementation_hash"] == services.implementation_hash(),
                "stage31_seal_review_invalid",
            )
            age = self.clock().astimezone(UTC) - datetime.fromisoformat(
                review["checked_at"]
            ).astimezone(UTC)
            require(timedelta(0) <= age < REVIEW_TTL, "stage31_review_expired")
            status = self._summary(db)
            require(
                not status["frozen"] and status["inflight_count"] == 0, "stage31_seal_not_quiescent"
            )
            development, holdout = _validate_plan(
                receipt["plan"], receipt["development_case_ids"], receipt["native_chain_summary"]
            )
            attempts, rows = _ownership(db, receipt)
            held_ids = {public_identity(case) for case in holdout}
            require(
                all(item["public_input_sha256"] not in held_ids for item in attempts),
                "stage31_holdout_already_attempted",
            )
            settled_ids = {row["reservation_id"] for row in rows if row["state"] == "settled"}
            final_dev = {
                item["public_input_sha256"]
                for item in attempts
                if item["revision_id"] == state["current_revision_id"]
                and item["reservation_id"] in settled_ids
            }
            require(
                {public_identity(case) for case in development} <= final_dev,
                "stage31_final_development_incomplete",
            )
            ident = digest(review)
            existing = db.execute(f"SELECT * FROM {SEAL_TABLE}").fetchall()
            if existing:
                require(
                    len(existing) == 1 and existing[0]["id"] == ident,
                    "stage31_holdout_already_sealed",
                )
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
            "stage31_current_model_proof_required",
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
            require(receipt is not None, "stage31_not_registered")
            r2.bounded_proof_gate(self, db, bounded_request, continuation_id)
            require(
                bounded_request.profile_id == STAGE16_PROFILE_ID
                and bounded_request.public_input_sha256
                in {public_identity(case) for case in receipt["plan"]},
                "stage31_request_outside_plan",
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
                "stage31_turn_request_limit_reached",
            )
            require(
                len(attempts) < MAX_REQUESTS
                and len(attempts) + OLD_ATTEMPTS_TOTAL < CUMULATIVE_REQUEST_LIMIT
                and status["reservation_count"] < ABSOLUTE_LIMIT,
                "campaign_request_limit_reached",
            )
            development, holdout = _validate_plan(
                receipt["plan"], receipt["development_case_ids"], receipt["native_chain_summary"]
            )
            seal = db.execute(f"SELECT * FROM {SEAL_TABLE}").fetchone()
            if bounded_request.public_input_sha256 in {public_identity(case) for case in holdout}:
                require(seal is not None, "stage31_holdout_revision_not_sealed")
            else:
                require(seal is None, "stage31_development_closed_after_blind_seal")
            require(
                Decimal(_stage_occupied(rows)) + held <= Decimal(PHASE_CAP),
                "stage31_phase_charge_cap_exhausted",
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
