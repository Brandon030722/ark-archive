"""Compact reviewed Stage23 repair successor: keep all native history in SQLite.

This module performs no registration or HTTP on import. The receipt pins exact
old row content and schema by SHA-256, rather than recursively embedding native
receipts. Registration validates the native chain independently; every live
operation rehashes all old rows. Only an explicitly selected Stage23 collector
uses its new fixed-plan selector. Native earlier interfaces remain unchanged.
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
from arknights_agent.runtime import stage23_r2_budget as previous
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

r2 = previous.r2

TABLE = "stage23_r3_public_allowance"
ATTEMPT_TABLE = "stage23_r3_request_attempt"
BASELINE_COUNT = 2163
BASELINE_ACCOUNTED = "16.03829040"
BASELINE_UNKNOWN = "9.04076800"
BASELINE_SAFE = "0.52094160"
SEND_HOLD = "0.29736960"
MAX_PROJECTED_SPEND = "0.22357200"
MAX_ATTEMPTS = 3
MAX_REQUESTS = 313
CUMULATIVE_REQUEST_LIMIT = 441
ABSOLUTE_LIMIT = 2476
PLAN_SHA256 = "5d3dd530fbd23a6ac1ea51aee53f3e548908aa900f447c16972b5c8c6f36b863"
AUTHORIZED_PLAN_IDENTITIES_SHA256 = (
    "0096334820842d6a9193f1b961711769c0cf76cbdb01f4f51bc92d64726f0f38"
)
OLD_FREEZE_SHA256 = "ee935a8728367a7e4d84adb70ed8568367d38f6d701285e471900ab0f8c6e53c"
OLD_ARCHIVE_SHA256 = "18c8c8535b17e171fb6b52103977e5acaae5977f4a4663036239c923b9f4a3ba"
OLD_RUNTIME_HASH = "dd3fd209cae7c427c5aa96920b01a2004d4692732286eba657debf69bcc19e00"
TASK_FILE_SHA256 = "1e07d744ad638eadd4ed4b86257189d262ebb2cad554ecd993e4e6935531dac6"
TASK_TEXT = (
    "那就继续修剩下的6轮失败，重点解决意图误读和参数纠正后无法提交。"
    "别补本地口语规则，修完用开发集回归，再建一组新保留集做真实API复测，"
    "结果和费用分开统计，累计预算仍控制在32元内，查不到的别编。"
)
REVIEW_TTL = timedelta(minutes=10)
REVIEW_SAFEGUARDS = (
    "direct_stage23_r3_task_verified",
    "no_money_increase_or_recharge",
    "previous_stage23_r2_development103_seen7_quiescent_and_settled",
    "previous_stage23_r2_holdout37_never_executed",
    "targeted8_seen4_blind14_are_not_full_development103_scores",
    "previous_stage23_partial8_and_r2_results_and_code_archives_frozen",
    "cumulative_old_and_new_per_identity_counters_retained",
    "old_native_chain_independently_verified",
    "exact_row_and_schema_hash_manifest_verified",
    "new_exact_public_plan_verified",
    "new_heldout_unseen_until_freeze",
    "new_heldout_source_oracle_verified",
    "bounded_source_and_continuation_gates_unchanged",
    "explicit_internal_selector_migration_reviewed",
    "original_send_hold_and_unknown_freeze_retained",
    "forecast_includes_final_send_and_corrections",
    "forecast_is_estimate_and_live_cap_stops_exhaustion",
    "no_automatic_retry_or_counter_reset",
)
PROTECTIONS = {
    f"stage23_r3_{table}_{op.lower()}": (
        f"CREATE TRIGGER stage23_r3_{table}_{op.lower()} BEFORE {op} ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'stage23_r3_append_only'); END"
    )
    for table in (TABLE, ATTEMPT_TABLE)
    for op in ("UPDATE", "DELETE")
}


def require(value, code="stage23_r3_receipt_invalid"):
    if not value:
        raise BudgetError(code)


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def implementation_paths():
    module, source = Path(__file__).resolve(), r2.r1.prior.SOURCE_ROOT
    if module.parent == source / "src/arknights_agent/runtime":
        test = source / "tests/test_stage23_r3_budget.py"
    else:
        require(
            module.parent == source / ".local/stage23-r3-preparation/budget",
            "stage23_r3_location_invalid",
        )
        test = module.parent / "tests/test_stage23_r3_budget.py"
    return (
        *previous.implementation_paths(),
        str(module.relative_to(source)),
        str(test.relative_to(source)),
    )


def implementation_hashes():
    return {
        name: hashlib.sha256((r2.r1.prior.SOURCE_ROOT / name).read_bytes()).hexdigest()
        for name in implementation_paths()
    }


def _q(name):
    return '"' + name.replace('"', '""') + '"'


def _row_sha(row):
    """Length-prefixed typed values hash original TEXT without JSON reescaping."""
    result = hashlib.sha256(b"stage23-exact-sqlite-row-v1\0")
    for key, value in sorted(row.items()):
        field = key.encode("utf-8")
        result.update(str(len(field)).encode() + b":" + field)
        if value is None:
            payload, kind = b"", b"N"
        elif type(value) is int:
            payload, kind = str(value).encode(), b"I"
        elif isinstance(value, str):
            payload, kind = value.encode("utf-8"), b"S"
        else:
            raise BudgetError("stage23_r3_historical_value_type_invalid")
        result.update(kind + str(len(payload)).encode() + b":" + payload)
    return result.hexdigest()


def manifest(db, baseline=None, *, excluded_tables=None):
    """Read and hash all old table rows and exact schema on every operation."""
    excluded = {TABLE, ATTEMPT_TABLE} if excluded_tables is None else excluded_tables
    schema_rows = [
        dict(row)
        for row in db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
        if row["tbl_name"] not in excluded
    ]
    table_names = [row["name"] for row in schema_rows if row["type"] == "table"]
    tables, finance = {}, None
    for name in table_names:
        info = [dict(row) for row in db.execute(f"PRAGMA table_info({_q(name)})")]
        primary = [row["name"] for row in sorted(info, key=lambda row: row["pk"]) if row["pk"]]
        require(primary, "stage23_r3_historical_primary_key_missing")
        pinned_keys = (
            None
            if baseline is None or name != "reservation"
            else {tuple(row["key"]) for row in baseline["tables"]["reservation"]["rows"]}
        )
        rows, old_reservations = [], []
        for raw in db.execute(f"SELECT * FROM {_q(name)}"):
            row = dict(raw)
            key = [row[column] for column in primary]
            if pinned_keys is not None and tuple(key) not in pinned_keys:
                continue
            # A new unknown may freeze the campaign, without altering old costs.
            if name == "campaign":
                row["frozen"], row["freeze_code"] = 0, None
            item = {"key": key, "sha256": _row_sha(row)}
            if "receipt_json" in row:
                item["native_receipt_json_sha256"] = sha(row["receipt_json"])
            rows.append(item)
            if name == "reservation":
                old_reservations.append(row)
        rows.sort(key=lambda row: json.dumps(row["key"], ensure_ascii=False, sort_keys=True))
        tables[name] = {"primary_key": primary, "rows": rows, "rows_sha256": digest(rows)}
        if name == "reservation":
            finance = {
                "reservation_count": len(old_reservations),
                "accounted_cny": _money(
                    sum(
                        (
                            Decimal(row["accounted_cny"])
                            for row in old_reservations
                            if row["state"] == "settled"
                        ),
                        Decimal(0),
                    )
                ),
                "unknown_hold_cny": _money(
                    sum(
                        (
                            Decimal(row["reserved_cny"])
                            for row in old_reservations
                            if row["state"] == "unknown"
                        ),
                        Decimal(0),
                    )
                ),
                "unknown_count": sum(row["state"] == "unknown" for row in old_reservations),
                "inflight_count": sum(row["state"] == "reserved" for row in old_reservations),
            }
    schema = [
        {key: row[key] for key in ("type", "name", "tbl_name")}
        | {"sql_sha256": sha(row["sql"] or "")}
        for row in schema_rows
    ]
    return {
        "encoding": "exact_sqlite_typed_row_sha256_v1",
        "tables": tables,
        "schema": schema,
        "schema_sha256": digest(schema),
        "finance": finance,
    }


def _verify_retired_code_archive():
    """Verify the immutable old archive, without treating its source as current."""
    root = r2.r1.prior.SOURCE_ROOT
    raw = (root / ".local/stage23-r2-live-code-freeze.json").read_text()
    require(sha(raw) == OLD_FREEZE_SHA256, "stage23_r3_previous_code_freeze_changed")
    frozen = json.loads(raw)
    archive = root / frozen["archive_path"]
    require(
        frozen["runtime_implementation_hash"] == OLD_RUNTIME_HASH
        and frozen["archive_sha256"] == OLD_ARCHIVE_SHA256
        and hashlib.sha256(archive.read_bytes()).hexdigest() == OLD_ARCHIVE_SHA256,
        "stage23_r3_previous_code_archive_changed",
    )
    with tarfile.open(archive, "r:gz") as packed:
        members = {item.name: item for item in packed.getmembers() if item.isfile()}
        require(set(members) == set(frozen["file_hashes"]), "stage23_r3_archive_inventory_changed")
        for name, expected in frozen["file_hashes"].items():
            stream = packed.extractfile(members[name])
            require(
                stream is not None and hashlib.sha256(stream.read()).hexdigest() == expected,
                "stage23_r3_archive_source_changed",
            )
    previous._verify_retired_code_archive()
    return frozen


def validate_native_chain(db):
    """Initial migration only: retain original partial8 and R2 dev103+seen7."""
    rows = db.execute(f"SELECT * FROM {previous.TABLE}").fetchall()
    require(len(rows) == 1, "stage23_r3_previous_registration_invalid")
    row = rows[0]
    old = previous._receipt_from_native_blob(row["receipt_json"])
    require(row["id"] == old["id"] and row["review_sha256"] == old["review_sha256"])
    require(
        {**r2.r1._triggers(db, previous.TABLE), **r2.r1._triggers(db, previous.ATTEMPT_TABLE)}
        == previous.PROTECTIONS,
        "stage23_r3_previous_protection_invalid",
    )
    prior_excluded = {TABLE, ATTEMPT_TABLE, previous.TABLE, previous.ATTEMPT_TABLE}
    require(
        manifest(db, old["baseline_manifest"], excluded_tables=prior_excluded)
        == old["baseline_manifest"],
        "stage23_r3_previous_history_changed",
    )
    prior_review = json.loads(old["independent_review_json"])
    require(
        prior_review["reviewed_files_sha256"] == previous.implementation_hashes(),
        "stage23_r3_previous_budget_implementation_changed",
    )
    original = previous.previous
    originals = db.execute(f"SELECT * FROM {original.TABLE}").fetchall()
    require(len(originals) == 1, "stage23_r3_original_registration_invalid")
    first_row = originals[0]
    first = original._receipt_from_native_blob(first_row["receipt_json"])
    require(
        first_row["id"] == first["id"]
        and first_row["review_sha256"] == first["review_sha256"]
        and first["id"] == old["native_chain_summary"]["previous_receipt_id"]
        and sha(first_row["receipt_json"])
        == old["native_chain_summary"]["previous_receipt_json_sha256"],
        "stage23_r3_original_receipt_changed",
    )
    require(
        {**r2.r1._triggers(db, original.TABLE), **r2.r1._triggers(db, original.ATTEMPT_TABLE)}
        == original.PROTECTIONS,
        "stage23_r3_original_protection_invalid",
    )
    require(
        manifest(
            db,
            first["baseline_manifest"],
            excluded_tables=prior_excluded | {original.TABLE, original.ATTEMPT_TABLE},
        )
        == first["baseline_manifest"],
        "stage23_r3_original_history_changed",
    )
    original_ids = {
        item["key"][0] for item in first["baseline_manifest"]["tables"]["reservation"]["rows"]
    }
    require(
        previous._validate_earlier_native_chain(db, original_ids) == first["native_chain_summary"],
        "stage23_r3_earlier_native_chain_changed",
    )
    first_attempts = sorted(
        (dict(item) for item in db.execute(f"SELECT * FROM {original.ATTEMPT_TABLE}")),
        key=lambda item: item["reservation_id"],
    )
    require(
        first_attempts == old["native_chain_summary"]["previous_attempts"]
        and len(first_attempts) == 8,
        "stage23_r3_original_eight_attempts_changed",
    )
    attempts, partial_rows = previous._ownership(db, old)
    for item in partial_rows:
        r2.r1._reservation_shape(item, "settled")
    require(
        len(attempts) == len(partial_rows) == 120
        and {item["public_input_sha256"] for item in attempts}
        == {public_identity(case) for case in old["plan"][:110]},
        "stage23_r3_previous_development_seen_scope_changed",
    )
    frozen = _verify_retired_code_archive()
    require(
        prior_review["runtime_implementation_hash"] == frozen["runtime_implementation_hash"],
        "stage23_r3_previous_runtime_archive_mismatch",
    )
    return {
        "previous_receipt_id": old["id"],
        "previous_receipt_json_sha256": sha(row["receipt_json"]),
        "previous_runtime_hash": prior_review["runtime_implementation_hash"],
        "previous_baseline_manifest_sha256": old["baseline_manifest_sha256"],
        "previous_native_chain_summary_sha256": old["native_chain_summary_sha256"],
        "previous_code_freeze_sha256": OLD_FREEZE_SHA256,
        "previous_code_archive_sha256": OLD_ARCHIVE_SHA256,
        "authorized_plan_identities": [
            {"case_id": case["case_id"], "public_input_sha256": public_identity(case)}
            for case in old["plan"]
        ],
        "previous_attempts": sorted(
            first_attempts + attempts,
            key=lambda item: (item["allowance_id"], item["reservation_id"]),
        ),
    }


def review_template(
    baseline,
    plan,
    chain,
    forecast,
    *,
    split_evidence_sha256,
    previous_result_manifest_sha256,
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
        "forecast": copy.deepcopy(forecast),
        "forecast_evidence_sha256": forecast_evidence_sha256,
        "split_evidence_sha256": split_evidence_sha256,
        "previous_result_manifest_sha256": previous_result_manifest_sha256,
        "reviewed_files_sha256": implementation_hashes(),
        "runtime_implementation_hash": services.implementation_hash(),
        **dict.fromkeys(REVIEW_SAFEGUARDS, False),
    }


def build_receipt(baseline, plan, chain, human_task_path, independent_review_path):
    task_raw, review_raw = (
        Path(human_task_path).read_text(),
        Path(independent_review_path).read_text(),
    )
    require(sha(task_raw) == TASK_FILE_SHA256, "stage23_r3_task_evidence_changed")
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
        "development_count": 8,
        "seen_regression_count": 4,
        "fresh_holdout_count": 14,
        "baseline_count": BASELINE_COUNT,
        "max_attempts_per_turn": 3,
        "max_additional_requests": MAX_REQUESTS,
        "cumulative_stage23_request_limit": CUMULATIVE_REQUEST_LIMIT,
        "absolute_limit": ABSOLUTE_LIMIT,
        "additional_money_cny": "0",
        "budget_cny": "32.00000000",
        "cap_cny": "25.60000000",
        "unknown_costs_released": False,
        "independent_review_json": review_raw,
        "review_sha256": sha(review_raw),
    }
    value["id"] = digest(value)
    return validate(value)


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
                "development_count",
                "seen_regression_count",
                "fresh_holdout_count",
                "baseline_count",
                "max_attempts_per_turn",
                "max_additional_requests",
                "cumulative_stage23_request_limit",
                "absolute_limit",
                "additional_money_cny",
                "budget_cny",
                "cap_cny",
                "unknown_costs_released",
                "independent_review_json",
                "review_sha256",
                "id",
            }
        )
        require(
            receipt["campaign_id"] == CAMPAIGN_ID
            and receipt["baseline_count"] == BASELINE_COUNT
            and receipt["max_attempts_per_turn"] == 3
            and receipt["max_additional_requests"] == MAX_REQUESTS
            and receipt["cumulative_stage23_request_limit"] == CUMULATIVE_REQUEST_LIMIT
            and receipt["absolute_limit"] == ABSOLUTE_LIMIT
            and receipt["development_count"] == 8
            and receipt["seen_regression_count"] == 4
            and receipt["fresh_holdout_count"] == 14
            and receipt["additional_money_cny"] == "0"
            and receipt["budget_cny"] == "32.00000000"
            and receipt["cap_cny"] == "25.60000000"
            and receipt["unknown_costs_released"] is False
        )
        task = receipt["task_evidence"]
        require(
            task["source"] == "direct_user_message_current_chat"
            and task["human_task"] == TASK_TEXT
            and task["nominal_cap_cny"] == "32"
            and task["effective_cap_cny"] == "25.60000000"
            and task["old_unknowns_retained"] is True
            and task["recharge_authorized"] is False
            and receipt["task_evidence_file_sha256"] == TASK_FILE_SHA256
        )
        b, chain, plan = (
            receipt["baseline_manifest"],
            receipt["native_chain_summary"],
            receipt["plan"],
        )
        require(
            b["encoding"] == "exact_sqlite_typed_row_sha256_v1"
            and b["schema_sha256"] == digest(b["schema"])
            and b["finance"]
            == {
                "reservation_count": BASELINE_COUNT,
                "accounted_cny": BASELINE_ACCOUNTED,
                "unknown_hold_cny": BASELINE_UNKNOWN,
                "unknown_count": 3,
                "inflight_count": 0,
            }
        )
        for table in b["tables"].values():
            require(table["rows_sha256"] == digest(table["rows"]) and table["primary_key"])
            require(
                len({json.dumps(row["key"], sort_keys=True) for row in table["rows"]})
                == len(table["rows"])
            )
            require(all(r2.r1.prior._hash(row["sha256"]) for row in table["rows"]))
        require(len(b["tables"]["reservation"]["rows"]) == BASELINE_COUNT)
        require(
            set(chain)
            == {
                "previous_receipt_id",
                "previous_receipt_json_sha256",
                "authorized_plan_identities",
                "previous_runtime_hash",
                "previous_baseline_manifest_sha256",
                "previous_native_chain_summary_sha256",
                "previous_code_freeze_sha256",
                "previous_code_archive_sha256",
                "previous_attempts",
            }
        )
        require(
            all(
                r2.r1.prior._hash(chain[k])
                for k in (
                    "previous_receipt_id",
                    "previous_receipt_json_sha256",
                    "previous_runtime_hash",
                )
            )
        )
        authorized = chain["authorized_plan_identities"]
        require(len(authorized) == 147 and digest(authorized) == AUTHORIZED_PLAN_IDENTITIES_SHA256)
        require(len(plan) == 26 and digest(plan) == PLAN_SHA256)
        for selected, allowed in (
            (plan[:8], authorized[:103]),
            (plan[8:12], authorized[103:110]),
            (plan[12:], authorized[110:]),
        ):
            require(
                all(
                    {"case_id": case["case_id"], "public_input_sha256": public_identity(case)}
                    in allowed
                    for case in selected
                ),
                "stage23_r3_request_subset_scope_invalid",
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
            == 26
        )
        require(all(case["case_id"].startswith("stage23-holdout-") for case in plan[12:]))
        require(
            chain["previous_runtime_hash"] == OLD_RUNTIME_HASH
            and chain["previous_code_freeze_sha256"] == OLD_FREEZE_SHA256
            and chain["previous_code_archive_sha256"] == OLD_ARCHIVE_SHA256
            and all(
                r2.r1.prior._hash(chain[k])
                for k in (
                    "previous_baseline_manifest_sha256",
                    "previous_native_chain_summary_sha256",
                )
            )
            and len(chain["previous_attempts"]) == 128
            and len(b["tables"][previous.ATTEMPT_TABLE]["rows"]) == 120
            and len(b["tables"][previous.TABLE]["rows"]) == 1
            and b["tables"][previous.TABLE]["rows"][0]["key"] == [chain["previous_receipt_id"]]
            and b["tables"][previous.TABLE]["rows"][0]["native_receipt_json_sha256"]
            == chain["previous_receipt_json_sha256"]
        )
        old_attempts = chain["previous_attempts"]
        original_table = previous.previous.ATTEMPT_TABLE
        expected_old_ids = {item["key"][0] for item in b["tables"][previous.ATTEMPT_TABLE]["rows"]}
        expected_old_ids.update(item["key"][0] for item in b["tables"][original_table]["rows"])
        require(
            len(b["tables"][original_table]["rows"]) == 8
            and len({item["reservation_id"] for item in old_attempts}) == 128
            and {item["reservation_id"] for item in old_attempts} == expected_old_ids
            and all(
                set(item) == {"reservation_id", "allowance_id", "public_input_sha256"}
                and item["public_input_sha256"]
                in {case["public_input_sha256"] for case in authorized[:110]}
                for item in old_attempts
            )
        )
        old_counts = Counter(item["public_input_sha256"] for item in old_attempts)
        require(
            all(n <= 3 for n in old_counts.values())
            and all(old_counts[public_identity(case)] < 3 for case in plan),
            "stage23_r3_selected_identity_request_limit_exhausted",
        )
        require(
            receipt["baseline_manifest_sha256"] == digest(b)
            and receipt["plan_sha256"] == digest(plan)
            and receipt["native_chain_summary_sha256"] == digest(chain)
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
                "forecast",
                "forecast_evidence_sha256",
                "split_evidence_sha256",
                "previous_result_manifest_sha256",
                "reviewed_files_sha256",
                "runtime_implementation_hash",
                *REVIEW_SAFEGUARDS,
            }
        )
        require(
            review["reviewer"] == "independent_agent"
            and review["passed"] is True
            and all(review[k] is True for k in REVIEW_SAFEGUARDS)
        )
        require(
            all(
                review[k] == receipt[k]
                for k in (
                    "baseline_manifest_sha256",
                    "plan_sha256",
                    "native_chain_summary_sha256",
                    "task_evidence_file_sha256",
                )
            )
        )
        require(
            set(review["reviewed_files_sha256"]) == set(implementation_paths())
            and all(r2.r1.prior._hash(v) for v in review["reviewed_files_sha256"].values())
        )
        require(
            all(
                r2.r1.prior._hash(review[k])
                for k in (
                    "forecast_evidence_sha256",
                    "split_evidence_sha256",
                    "previous_result_manifest_sha256",
                    "runtime_implementation_hash",
                )
            )
        )
        forecast = review["forecast"]
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
            and forecast["parameter_correction_calls_included"] >= 9
            and forecast["estimate_not_guarantee"] is True
        )
        require(
            _decimal(forecast["projected_full_round_conservative_cny"])
            <= Decimal(MAX_PROJECTED_SPEND),
            "stage23_r3_complete_round_projection_exceeds_safe_budget",
        )
        require(datetime.fromisoformat(review["checked_at"]).tzinfo is not None)
        require(receipt["id"] == digest({k: v for k, v in receipt.items() if k != "id"}))
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        raise BudgetError("stage23_r3_receipt_invalid") from None
    return receipt


@lru_cache(maxsize=1)
def _validated_native_blob(raw):
    return validate(json.loads(raw))


def _receipt_from_native_blob(raw):
    return copy.deepcopy(_validated_native_blob(raw))


def _ownership(db, receipt):
    attempts = [dict(row) for row in db.execute(f"SELECT * FROM {ATTEMPT_TABLE}")]
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
        "stage23_r3_attempt_ownership_invalid",
    )
    counts = Counter(row["public_input_sha256"] for row in attempts)
    counts.update(
        item["public_input_sha256"] for item in receipt["native_chain_summary"]["previous_attempts"]
    )
    require(
        len(attempts) <= MAX_REQUESTS
        and sum(counts.values()) <= CUMULATIVE_REQUEST_LIMIT
        and all(n <= 3 for n in counts.values()),
        "stage23_r3_attempt_limit_invalid",
    )
    return attempts, new_rows


def registered(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone():
        return None
    rows = db.execute(f"SELECT * FROM {TABLE}").fetchall()
    require(len(rows) == 1, "stage23_r3_registration_invalid")
    row = rows[0]
    receipt = _receipt_from_native_blob(row["receipt_json"])
    require(
        row["id"] == receipt["id"] and row["review_sha256"] == receipt["review_sha256"],
        "stage23_r3_registration_invalid",
    )
    require(
        {**r2.r1._triggers(db, TABLE), **r2.r1._triggers(db, ATTEMPT_TABLE)} == PROTECTIONS,
        "stage23_r3_protection_invalid",
    )
    require(
        manifest(db, receipt["baseline_manifest"]) == receipt["baseline_manifest"],
        "stage23_r3_history_changed",
    )
    review = json.loads(receipt["independent_review_json"])
    require(
        review["reviewed_files_sha256"] == implementation_hashes(),
        "stage23_r3_implementation_changed",
    )
    require(
        review["runtime_implementation_hash"] == services.implementation_hash(),
        "stage23_r3_runtime_changed",
    )
    _ownership(db, receipt)
    return receipt


class Stage23R3BudgetLedger(BudgetLedger):
    def _summary(self, db, reservation=None):
        value = super()._summary(db, reservation)
        registered(db)
        return value

    def authorize_stage23_r3_public_plan(self, receipt):
        validate(receipt)
        with self._transaction() as db:
            status = self._summary(db)
            old = registered(db)
            if old is not None:
                require(old == receipt, "stage23_r3_already_registered")
                return {"id": receipt["id"], "reused": True, **status}
            require(not status["frozen"], "campaign_frozen")
            require(status["inflight_count"] == 0, "request_inflight")
            require(
                status["price_verification_status"] == "ready", status["price_verification_status"]
            )
            require(
                status["budget_cny"] == "32.00000000"
                and status["cap_cny"] == "25.60000000"
                and status["remaining_cny"] == BASELINE_SAFE,
                "stage23_r3_baseline_finance_changed",
            )
            require(manifest(db) == receipt["baseline_manifest"], "stage23_r3_baseline_changed")
            require(
                validate_native_chain(db) == receipt["native_chain_summary"],
                "stage23_r3_native_chain_changed",
            )
            review = json.loads(receipt["independent_review_json"])
            age = self.clock().astimezone(UTC) - datetime.fromisoformat(
                review["checked_at"]
            ).astimezone(UTC)
            require(timedelta(0) <= age < REVIEW_TTL, "stage23_r3_review_expired")
            require(
                review["reviewed_files_sha256"] == implementation_hashes(),
                "stage23_r3_implementation_changed",
            )
            require(
                review["runtime_implementation_hash"] == services.implementation_hash(),
                "stage23_r3_runtime_changed",
            )
            db.execute(
                f"CREATE TABLE {TABLE} (id TEXT PRIMARY KEY, review_sha256 TEXT NOT NULL UNIQUE, receipt_json TEXT NOT NULL, authorized_at TEXT NOT NULL)"
            )
            db.execute(
                f"CREATE TABLE {ATTEMPT_TABLE} (reservation_id TEXT PRIMARY KEY REFERENCES reservation(reservation_id), allowance_id TEXT NOT NULL REFERENCES {TABLE}(id), public_input_sha256 TEXT NOT NULL)"
            )
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
            return {"id": receipt["id"], "reused": False, **self._summary(db)}

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
            and 1 <= output_tokens <= MAX_OUTPUT_TOKENS,
            "reservation_token_bound_invalid",
        )
        bounded_request.validate()
        ip, op = (
            _decimal(input_price, minimum=INPUT_PRICE_FLOOR),
            _decimal(output_price, minimum=OUTPUT_PRICE_FLOOR),
        )
        held = _cost(input_tokens, output_tokens, ip, op)
        with self._transaction() as db:
            status = super()._summary(db)
            require(not status["frozen"], "campaign_frozen")
            require(
                status["price_verification_status"] == "ready", status["price_verification_status"]
            )
            require(status["inflight_count"] == 0, "request_inflight")
            receipt = registered(db)
            require(receipt is not None, "stage23_r3_not_registered")
            r2.bounded_proof_gate(self, db, bounded_request, continuation_id)
            require(
                bounded_request.profile_id == STAGE16_PROFILE_ID
                and bounded_request.public_input_sha256
                in {public_identity(case) for case in receipt["plan"]},
                "stage23_r3_request_outside_plan",
            )
            attempts, new_rows = _ownership(db, receipt)
            for row in new_rows:
                r2.r1._reservation_shape(row, "settled")
            require(
                sum(
                    row["public_input_sha256"] == bounded_request.public_input_sha256
                    for row in attempts + receipt["native_chain_summary"]["previous_attempts"]
                )
                < 3,
                "stage23_r3_turn_request_limit_reached",
            )
            require(
                len(attempts) < MAX_REQUESTS
                and len(attempts) + len(receipt["native_chain_summary"]["previous_attempts"])
                < CUMULATIVE_REQUEST_LIMIT
                and status["reservation_count"] < ABSOLUTE_LIMIT,
                "campaign_request_limit_reached",
            )
            require(held <= Decimal(status["remaining_cny"]), "budget_exhausted")
            ident = uuid4().hex
            db.execute(
                "INSERT INTO reservation (reservation_id,campaign_id,input_tokens,output_tokens,input_price,output_price,reserved_cny,state) VALUES (?,?,?,?,?,?,?,'reserved')",
                (ident, CAMPAIGN_ID, input_tokens, output_tokens, str(ip), str(op), _money(held)),
            )
            db.execute(
                f"INSERT INTO {ATTEMPT_TABLE} VALUES (?,?,?)",
                (ident, receipt["id"], bounded_request.public_input_sha256),
            )
            return ident
