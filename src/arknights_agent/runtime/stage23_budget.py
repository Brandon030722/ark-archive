"""Compact reviewed Stage23 successor: keep all native history in SQLite.

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
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from arknights_agent import services
from arknights_agent.runtime import stage22_r2_budget as r2
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

TABLE = "stage23_public_allowance"
ATTEMPT_TABLE = "stage23_request_attempt"
BASELINE_COUNT = 2035
BASELINE_ACCOUNTED = "15.52641200"
BASELINE_UNKNOWN = "9.04076800"
BASELINE_SAFE = "1.03282000"
SEND_HOLD = "0.29736960"
MAX_PROJECTED_SPEND = "0.73545040"
MAX_ATTEMPTS = 3
MAX_REQUESTS = 441
ABSOLUTE_LIMIT = BASELINE_COUNT + MAX_REQUESTS
TASK_FILE_SHA256 = "1e07d744ad638eadd4ed4b86257189d262ebb2cad554ecd993e4e6935531dac6"
TASK_TEXT = (
    "那就继续修剩下的6轮失败，重点解决意图误读和参数纠正后无法提交。"
    "别补本地口语规则，修完用开发集回归，再建一组新保留集做真实API复测，"
    "结果和费用分开统计，累计预算仍控制在32元内，查不到的别编。"
)
REVIEW_TTL = timedelta(minutes=10)
REVIEW_SAFEGUARDS = (
    "direct_stage23_task_verified",
    "no_money_increase_or_recharge",
    "all_previous_collectors_quiescent_and_complete",
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
    f"stage23_{table}_{op.lower()}": (
        f"CREATE TRIGGER stage23_{table}_{op.lower()} BEFORE {op} ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'stage23_append_only'); END"
    )
    for table in (TABLE, ATTEMPT_TABLE)
    for op in ("UPDATE", "DELETE")
}


def require(value, code="stage23_receipt_invalid"):
    if not value:
        raise BudgetError(code)


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def implementation_paths():
    module, source = Path(__file__).resolve(), r2.r1.prior.SOURCE_ROOT
    if module.parent == source / "src/arknights_agent/runtime":
        test = source / "tests/test_stage23_budget.py"
    else:
        require(
            module.parent == source / ".local/stage23-preparation/budget",
            "stage23_location_invalid",
        )
        test = module.parent / "tests/test_stage23_budget.py"
    return (
        *r2.implementation_paths(),
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
            raise BudgetError("stage23_historical_value_type_invalid")
        result.update(kind + str(len(payload)).encode() + b":" + payload)
    return result.hexdigest()


def manifest(db, baseline=None):
    """Read and hash all old table rows and exact schema on every operation."""
    schema_rows = [
        dict(row)
        for row in db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
        if row["tbl_name"] not in {TABLE, ATTEMPT_TABLE}
    ]
    table_names = [row["name"] for row in schema_rows if row["type"] == "table"]
    tables, finance = {}, None
    for name in table_names:
        info = [dict(row) for row in db.execute(f"PRAGMA table_info({_q(name)})")]
        primary = [row["name"] for row in sorted(info, key=lambda row: row["pk"]) if row["pk"]]
        require(primary, "stage23_historical_primary_key_missing")
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


def validate_native_chain(db):
    """Use native validators, and migrate only their past runtime selection."""
    old_rows = db.execute(f"SELECT * FROM {r2.TABLE}").fetchall()
    require(len(old_rows) == 1, "stage23_previous_round_registration_invalid")
    row = old_rows[0]
    old = r2._receipt_from_native_blob(row["receipt_json"])
    require(row["id"] == old["id"] and row["review_sha256"] == old["review_sha256"])
    protections = {**r2.r1._triggers(db, r2.TABLE), **r2.r1._triggers(db, r2.ATTEMPT_TABLE)}
    require(protections == r2.PROTECTIONS, "stage23_previous_round_protection_invalid")
    current = r2.snapshot(db)
    pinned = {row["reservation_id"] for row in old["baseline"]["reservation"]}
    current["reservation"] = [
        row for row in current["reservation"] if row["reservation_id"] in pinned
    ]
    require(current == old["baseline"], "stage23_previous_round_history_changed")
    review = json.loads(old["independent_review_json"])
    require(
        review["reviewed_files_sha256"] == r2.implementation_hashes(),
        "stage23_previous_budget_implementation_changed",
    )
    # The old language/runtime hash is a frozen past measurement, not Stage23.
    # New review binds the new runtime explicitly; old budget files stay exact.
    attempts = [dict(row) for row in db.execute(f"SELECT * FROM {r2.ATTEMPT_TABLE}")]
    later = [
        dict(row)
        for row in db.execute("SELECT * FROM reservation")
        if row["reservation_id"] not in pinned
    ]
    require({row["reservation_id"] for row in attempts} == {row["reservation_id"] for row in later})
    for item in later:
        r2.r1._reservation_shape(item, "settled")
    counts = Counter(row["public_input_sha256"] for row in attempts)
    require(
        set(counts) == {public_identity(case) for case in old["plan"]}
        and all(1 <= n <= 3 for n in counts.values())
        and len(attempts) <= 420
        and all(row["allowance_id"] == old["id"] for row in attempts)
    )
    seen = {public_identity(case) for case in old["plan"]}
    r1_old = json.loads(old["baseline"][r2.r1.TABLE][0]["receipt_json"])
    seen.update(public_identity(case) for case in r1_old["plan"])
    stage21_old = json.loads(r1_old["baseline"][r2.r1.prior.TABLE][0]["receipt_json"])
    seen.update(public_identity(case) for case in stage21_old["plan"])
    failed_prefixes = tuple(f"stage22-r2-holdout-{n:03}-" for n in (3, 6, 11, 18))
    seen_failure_plan = [
        case for case in old["plan"] if case["case_id"].startswith(failed_prefixes)
    ]
    require(len(seen_failure_plan) == 7, "stage23_known_failure_scope_changed")
    return {
        "previous_receipt_id": old["id"],
        "previous_receipt_json_sha256": sha(row["receipt_json"]),
        "development_plan": old["plan"][:103],
        "seen_failure_plan": seen_failure_plan,
        "seen_public_identities": sorted(seen),
        "previous_runtime_hash": review["runtime_implementation_hash"],
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
    require(sha(task_raw) == TASK_FILE_SHA256, "stage23_task_evidence_changed")
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
        "development_count": 103,
        "seen_regression_count": 7,
        "fresh_holdout_count": 37,
        "baseline_count": BASELINE_COUNT,
        "max_attempts_per_turn": 3,
        "max_additional_requests": 441,
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
            and receipt["max_additional_requests"] == 441
            and receipt["absolute_limit"] == ABSOLUTE_LIMIT
            and receipt["development_count"] == 103
            and receipt["seen_regression_count"] == 7
            and receipt["fresh_holdout_count"] == 37
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
                "development_plan",
                "seen_public_identities",
                "seen_failure_plan",
                "previous_runtime_hash",
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
        require(
            len(plan) == 147
            and plan[:103] == chain["development_plan"]
            and len(chain["seen_failure_plan"]) == 7
            and [public_identity(case) for case in plan[103:110]]
            == [public_identity(case) for case in chain["seen_failure_plan"]]
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
            == 147
        )
        require(all(case["case_id"].startswith("stage23-holdout-") for case in plan[110:]))
        require(
            not set(chain["seen_public_identities"]).intersection(
                public_identity(case) for case in plan[110:]
            )
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
            "stage23_complete_round_projection_exceeds_safe_budget",
        )
        require(datetime.fromisoformat(review["checked_at"]).tzinfo is not None)
        require(receipt["id"] == digest({k: v for k, v in receipt.items() if k != "id"}))
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        raise BudgetError("stage23_receipt_invalid") from None
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
        "stage23_attempt_ownership_invalid",
    )
    counts = Counter(row["public_input_sha256"] for row in attempts)
    require(
        len(attempts) <= 441 and all(n <= 3 for n in counts.values()),
        "stage23_attempt_limit_invalid",
    )
    return attempts, new_rows


def registered(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone():
        return None
    rows = db.execute(f"SELECT * FROM {TABLE}").fetchall()
    require(len(rows) == 1, "stage23_registration_invalid")
    row = rows[0]
    receipt = _receipt_from_native_blob(row["receipt_json"])
    require(
        row["id"] == receipt["id"] and row["review_sha256"] == receipt["review_sha256"],
        "stage23_registration_invalid",
    )
    require(
        {**r2.r1._triggers(db, TABLE), **r2.r1._triggers(db, ATTEMPT_TABLE)} == PROTECTIONS,
        "stage23_protection_invalid",
    )
    require(
        manifest(db, receipt["baseline_manifest"]) == receipt["baseline_manifest"],
        "stage23_history_changed",
    )
    review = json.loads(receipt["independent_review_json"])
    require(
        review["reviewed_files_sha256"] == implementation_hashes(), "stage23_implementation_changed"
    )
    require(
        review["runtime_implementation_hash"] == services.implementation_hash(),
        "stage23_runtime_changed",
    )
    _ownership(db, receipt)
    return receipt


class Stage23BudgetLedger(BudgetLedger):
    def _summary(self, db, reservation=None):
        value = super()._summary(db, reservation)
        registered(db)
        return value

    def authorize_stage23_public_plan(self, receipt):
        validate(receipt)
        with self._transaction() as db:
            status = self._summary(db)
            old = registered(db)
            if old is not None:
                require(old == receipt, "stage23_already_registered")
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
                "stage23_baseline_finance_changed",
            )
            require(manifest(db) == receipt["baseline_manifest"], "stage23_baseline_changed")
            require(
                validate_native_chain(db) == receipt["native_chain_summary"],
                "stage23_native_chain_changed",
            )
            review = json.loads(receipt["independent_review_json"])
            age = self.clock().astimezone(UTC) - datetime.fromisoformat(
                review["checked_at"]
            ).astimezone(UTC)
            require(timedelta(0) <= age < REVIEW_TTL, "stage23_review_expired")
            require(
                review["reviewed_files_sha256"] == implementation_hashes(),
                "stage23_implementation_changed",
            )
            require(
                review["runtime_implementation_hash"] == services.implementation_hash(),
                "stage23_runtime_changed",
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
            require(receipt is not None, "stage23_not_registered")
            r2.bounded_proof_gate(self, db, bounded_request, continuation_id)
            require(
                bounded_request.profile_id == STAGE16_PROFILE_ID
                and bounded_request.public_input_sha256
                in {public_identity(case) for case in receipt["plan"]},
                "stage23_request_outside_plan",
            )
            attempts, new_rows = _ownership(db, receipt)
            for row in new_rows:
                r2.r1._reservation_shape(row, "settled")
            require(
                sum(
                    row["public_input_sha256"] == bounded_request.public_input_sha256
                    for row in attempts
                )
                < 3,
                "stage23_turn_request_limit_reached",
            )
            require(
                len(attempts) < 441 and status["reservation_count"] < ABSOLUTE_LIMIT,
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
