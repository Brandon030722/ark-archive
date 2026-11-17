"""Rebind the user's 28 follow-up questions to a new source release.

No money or total request limit is raised. Immutable prior receipts and unknown
holds remain in force. This receipt can spend only within the old 951 attempts.
"""

import copy
import json
from datetime import UTC

from arknights_agent.runtime.budget import BudgetError
from arknights_agent.runtime.request_allowance import BASE_TABLES, digest, public_identity

AUTHORIZATION = "那就继续把这28个实际需求补齐，去找可靠资料，把结局路线、事件选项和奖励做进去，同名事件给我列出可选项，查不到的概率别编。"
TABLE = "rag_recheck_allowance"


def snapshot(db):
    return {
        name: [dict(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY 1")]
        for name in (*BASE_TABLES, "request_allowance")
    }


def build_receipt(baseline, plan):
    old = json.loads(baseline["request_allowance"][0]["receipt_json"])
    receipt = {
        "authorization_text": AUTHORIZATION,
        "baseline": copy.deepcopy(baseline),
        "baseline_sha256": digest(baseline),
        "plan": copy.deepcopy(plan),
        "baseline_count": len(baseline["reservation"]),
        "max_requests": 84,
        "absolute_limit": old["baseline_count"] + old["max_requests"],
        "additional_money_cny": "0",
    }
    receipt["id"] = digest(receipt)
    return validate(receipt)


def validate(receipt):
    from arknights_agent.runtime.request_allowance import validate as validate_old

    try:
        b = receipt["baseline"]
        plan = receipt["plan"]
        old = validate_old(json.loads(b["request_allowance"][0]["receipt_json"]))
        previous = {(c["case_id"], c["question"], c["module"]) for c in old["plan"]}
        valid = (
            set(receipt)
            == {
                "authorization_text",
                "baseline",
                "baseline_sha256",
                "plan",
                "baseline_count",
                "max_requests",
                "absolute_limit",
                "additional_money_cny",
                "id",
            }
            and set(b) == set((*BASE_TABLES, "request_allowance"))
            and receipt["authorization_text"] == AUTHORIZATION
            and len(plan) == 28
            and len({public_identity(c) for c in plan}) == 28
            and len({c["case_id"] for c in plan}) == 28
            and len({c["release_id"] for c in plan}) == 1
            and all(
                set(c) == {"case_id", "question", "module", "release_id"}
                and all(isinstance(v, str) and v for v in c.values())
                and (c["case_id"], c["question"], c["module"]) in previous
                for c in plan
            )
            and len(b["request_allowance"]) == 1
            and b["request_allowance"][0]["id"] == old["id"]
            and b["campaign"][0]["frozen"] == 0
            and sum(r["state"] == "unknown" for r in b["reservation"]) == 2
            and not any(r["state"] in ("reserved", "inflight") for r in b["reservation"])
            and receipt["baseline_count"] == len(b["reservation"])
            and receipt["absolute_limit"] == old["baseline_count"] + old["max_requests"] == 951
            and receipt["max_requests"] == 84
            and receipt["baseline_count"] + 84 <= 951
            and receipt["additional_money_cny"] == "0"
            and receipt["baseline_sha256"] == digest(b)
            and receipt["id"] == digest({k: v for k, v in receipt.items() if k != "id"})
        )
    except (KeyError, TypeError, ValueError, IndexError):
        valid = False
    if not valid:
        raise BudgetError("rag_recheck_receipt_invalid")
    return receipt


def verify_history(db, receipt):
    current = snapshot(db)
    ids = {r["reservation_id"] for r in receipt["baseline"]["reservation"]}
    current["reservation"] = [r for r in current["reservation"] if r["reservation_id"] in ids]
    for key in ("frozen", "freeze_code"):
        current["campaign"][0][key] = receipt["baseline"]["campaign"][0][key]
    if digest(current) != receipt["baseline_sha256"]:
        raise BudgetError("rag_recheck_history_changed")


def register(book, receipt):
    validate(receipt)
    with book._transaction() as db:
        status = book._summary(db)
        if (
            status["frozen"]
            or status["inflight_count"]
            or status["price_verification_status"] != "ready"
        ):
            raise BudgetError("rag_recheck_campaign_not_ready")
        verify_history(db, receipt)
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone()
        if exists:
            rows = db.execute(f"SELECT * FROM {TABLE}").fetchall()
            if (
                len(rows) == 1
                and rows[0]["id"] == receipt["id"]
                and json.loads(rows[0]["receipt_json"]) == receipt
            ):
                return {"id": receipt["id"], "reused": True}
            raise BudgetError("rag_recheck_already_registered")
        if status["reservation_count"] != receipt["baseline_count"]:
            raise BudgetError("rag_recheck_baseline_changed")
        db.execute(
            f"CREATE TABLE {TABLE} (id TEXT PRIMARY KEY, receipt_json TEXT NOT NULL, authorized_at TEXT NOT NULL)"
        )
        db.execute(
            f"INSERT INTO {TABLE} VALUES (?,?,?)",
            (
                receipt["id"],
                json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                book.clock().astimezone(UTC).isoformat(),
            ),
        )
        return {"id": receipt["id"], "reused": False}


def request_limit(db, bound):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone():
        return None
    rows = db.execute(f"SELECT * FROM {TABLE}").fetchall()
    if len(rows) != 1:
        raise BudgetError("rag_recheck_registration_invalid")
    receipt = validate(json.loads(rows[0]["receipt_json"]))
    if rows[0]["id"] != receipt["id"]:
        raise BudgetError("rag_recheck_registration_invalid")
    verify_history(db, receipt)
    if bound.public_input_sha256 in {public_identity(c) for c in receipt["plan"]}:
        return min(receipt["absolute_limit"], receipt["baseline_count"] + receipt["max_requests"])
    return None
