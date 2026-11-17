"""One explicit additional authorization; retain all historical costs and holds."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from decimal import Decimal

from arknights_agent.runtime.budget import CAMPAIGN_ID, BudgetError

AUTHORIZATION_TEXT = "现在不用担心费用，我再给你十块钱，你把这个做好"
TABLES = ("campaign", "reservation", "campaign_review", "reviewed_reservation", "bounded_continuation")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def snapshot(db):
    return {name: [dict(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY 1")]
            for name in TABLES}


def build_receipt(baseline, review_sha256, *, authorization_text=AUTHORIZATION_TEXT):
    if authorization_text != AUTHORIZATION_TEXT:
        raise BudgetError("extension_authorization_invalid")
    receipt = {"authorization_text": authorization_text, "additional_budget_cny": "10",
               "additional_cap_cny": "8.00000000", "total_budget_cny": "28",
               "total_cap_cny": "22.40000000", "campaign_id": CAMPAIGN_ID,
               "review_sha256": review_sha256, "baseline": copy.deepcopy(baseline),
               "baseline_sha256": digest(baseline), "unknown_costs_released": False}
    receipt["extension_id"] = digest(receipt)
    return validate(receipt)


def validate(receipt):
    try:
        baseline = receipt["baseline"]
        campaign = baseline["campaign"][0]
        valid = (
            set(receipt) == {"authorization_text", "additional_budget_cny", "additional_cap_cny",
                             "total_budget_cny", "total_cap_cny", "campaign_id", "review_sha256",
                             "baseline", "baseline_sha256", "unknown_costs_released", "extension_id"}
            and receipt["authorization_text"] == AUTHORIZATION_TEXT
            and receipt["additional_budget_cny"] == "10"
            and receipt["additional_cap_cny"] == "8.00000000"
            and receipt["total_budget_cny"] == "28" and receipt["total_cap_cny"] == "22.40000000"
            and receipt["campaign_id"] == CAMPAIGN_ID and receipt["unknown_costs_released"] is False
            and re.fullmatch(r"[0-9a-f]{64}", receipt["review_sha256"])
            and set(baseline) == set(TABLES) and len(baseline["campaign"]) == 1
            and campaign["campaign_id"] == CAMPAIGN_ID and campaign["budget_cny"] == "18"
            and campaign["safety_fraction"] == "0.8" and campaign["cap_cny"] == "14.40000000"
            and campaign["frozen"] == 1 and campaign["freeze_code"] == "unknown_failure"
            and len(baseline["reservation"]) == 251
            and len({r["reservation_id"] for r in baseline["reservation"]}) == 251
            and sum(r["state"] == "unknown" for r in baseline["reservation"]) == 2
            and sum(r["state"] == "settled" for r in baseline["reservation"]) == 249
            and len(baseline["bounded_continuation"]) == 2
            and digest(baseline) == receipt["baseline_sha256"]
            and digest({k: v for k, v in receipt.items() if k != "extension_id"}) == receipt["extension_id"]
        )
    except (KeyError, TypeError, ValueError, IndexError):
        valid = False
    if not valid:
        raise BudgetError("extension_receipt_invalid")
    return receipt


def effective_limits(db, campaign):
    """Default caps stay unchanged unless the one reviewed grant is registered."""
    budget, cap = Decimal(campaign["budget_cny"]), Decimal(campaign["cap_cny"])
    from arknights_agent.runtime.stage22_money_grant import effective_limits as stage22_limits

    exists = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='budget_extension'").fetchone()
    if not exists:
        return stage22_limits(db, budget, cap)
    rows = db.execute("SELECT * FROM budget_extension").fetchall()
    if len(rows) != 1:
        raise BudgetError("extension_registration_invalid")
    row = rows[0]
    receipt = validate(json.loads(row["receipt_json"]))
    if row["extension_id"] != receipt["extension_id"] or row["campaign_id"] != CAMPAIGN_ID:
        raise BudgetError("extension_registration_invalid")
    baseline = receipt["baseline"]
    current = snapshot(db)
    old_ids = {r["reservation_id"] for r in baseline["reservation"]}
    current["reservation"] = [r for r in current["reservation"] if r["reservation_id"] in old_ids]
    # A new unknown may freeze again. Reusing the grant must never clear it.
    for key in ("frozen", "freeze_code"):
        current["campaign"][0][key] = baseline["campaign"][0][key]
    if digest(current) != receipt["baseline_sha256"]:
        raise BudgetError("extension_historical_state_changed")
    return stage22_limits(db, budget + Decimal(receipt["additional_budget_cny"]),
                          cap + Decimal(receipt["additional_cap_cny"]))
