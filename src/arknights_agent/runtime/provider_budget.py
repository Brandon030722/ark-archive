"""New-provider reservations share the existing append-only counter and freeze.

Old DeepSeek reservations, receipts and holds are never migrated or repriced.
New rates are owner-configured conservative CNY estimates, not invoices.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from arknights_agent import services

from .budget import CAMPAIGN_ID, BudgetError, _cost, _money
from .stage40_budget import Stage40BudgetLedger, require


@dataclass(frozen=True)
class ProviderBound:
    input_tokens: int
    output_tokens: int
    wire_sha256: str
    public_input_sha256: str
    connection: dict

    def audit(self):
        return {
            "profile_id": "configured-provider-v1",
            "input_token_upper_bound": self.input_tokens,
            "output_token_upper_bound": self.output_tokens,
            "wire_sha256": self.wire_sha256,
            "provider": self.connection["protocol"],
            "model": self.connection["model"],
        }


class ProviderProfile:
    continuation_id = "configured-provider-v1"
    profile_id = continuation_id

    def __init__(self, settings):
        self.settings = settings

    def bind_request(self, provider, messages, tools, *, max_output_tokens):
        payload = provider._request(messages, tools, max_output_tokens)
        identity = services.content_digest([m for m in messages if m["role"] == "user"])
        cfg = self.settings
        return ProviderBound(
            cfg.agent_context_tokens,
            max_output_tokens,
            services.content_digest(payload),
            identity,
            {
                "protocol": cfg.api_protocol,
                "model": cfg.deepseek_model,
                "base_url": cfg.api_base_url,
                "input_price": str(cfg.model_input_cny_per_million),
                "output_price": str(cfg.model_output_cny_per_million),
                "context_tokens": cfg.agent_context_tokens,
                "rate_basis": cfg.model_price_source,
            },
        )


class ProviderLedger(Stage40BudgetLedger):
    def __init__(self, settings, task_id):
        self.path = Path(settings.model_budget_path).resolve()
        require(self.path.is_file(), "fixed_ledger_absent")
        require(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id), "stage40_task_identity_invalid")
        self.task_id, self.settings = task_id, settings
        self.pause_campaign_on_unknown = not settings.web_task_failure_isolation
        self.unlimited, self.request_limit = True, 27
        self.clock = lambda: datetime.now(UTC)
        self.price_verified_at = settings.model_price_verified_at
        self.model_id = settings.deepseek_model
        with self._readonly() as db:
            require(
                db.execute("SELECT 1 FROM campaign WHERE campaign_id=?", (CAMPAIGN_ID,)).fetchone(),
                "fixed_campaign_absent",
            )

    def _price_status(self):
        verified = self.settings.model_price_verified_at
        if not verified or not verified.tzinfo or not timedelta(0) <= datetime.now(UTC) - verified < timedelta(days=7):
            return "price_verification_expired"
        return (
            "ready"
            if all(
                v is not None and Decimal(v) > 0
                for v in (
                    self.settings.model_input_cny_per_million,
                    self.settings.model_output_cny_per_million,
                )
            )
            else "price_not_verified"
        )

    def _summary(self, db, reservation=None):
        result = super()._summary(db, reservation)
        result.update(
            cost_basis=self.settings.model_price_source + "_times1_10_not_invoice",
            provider=self.settings.api_protocol,
            model=self.model_id,
        )
        return result

    def _reservation(self, db, ident):
        row = super()._reservation(db, ident)
        try:
            owned = db.execute(
                "SELECT task_id FROM provider_attempt WHERE reservation_id=?", (ident,)
            ).fetchone()
        except Exception:
            raise BudgetError("provider_reservation_not_owned") from None
        require(owned and owned["task_id"] == self.task_id, "provider_reservation_not_owned")
        return row

    def _hold_bounded_unknown(self, db, row, reported_usage):
        self._reservation(db, row["reservation_id"])
        # We already reserved the configured complete context. If a usable
        # receipt exceeds it, increase only this request's hold; never release.
        held = Decimal(row["reserved_cny"])
        if isinstance(reported_usage, dict):
            prompt, completion = (
                reported_usage.get("prompt_tokens"),
                reported_usage.get("completion_tokens"),
            )
            if all(type(v) is int and 0 <= v <= 2**63 - 1 for v in (prompt, completion)):
                held = max(
                    held,
                    _cost(
                        prompt,
                        completion,
                        Decimal(row["input_price"]),
                        Decimal(row["output_price"]),
                    ),
                )
        if held > Decimal(row["reserved_cny"]):
            db.execute(
                "UPDATE reservation SET reserved_cny=? WHERE reservation_id=?",
                (_money(held), row["reservation_id"]),
            )
        return True

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
        bound = bounded_request
        require(
            type(bound) is ProviderBound and continuation_id == ProviderProfile.continuation_id,
            "bounded_request_proof_invalid",
        )
        cfg = self.settings
        require(
            bound.input_tokens == input_tokens == cfg.agent_context_tokens
            and bound.output_tokens == output_tokens
            and 0 < output_tokens <= cfg.agent_max_output_tokens
            and bound.connection["model"] == self.model_id
            and bound.connection["protocol"] == cfg.api_protocol
            and bound.connection["base_url"] == cfg.api_base_url
            and Decimal(input_price) == cfg.model_input_cny_per_million
            and Decimal(output_price) == cfg.model_output_cny_per_million,
            "bounded_request_proof_invalid",
        )
        require(self._price_status() == "ready", "price_not_verified")
        held = _cost(input_tokens, output_tokens, Decimal(input_price), Decimal(output_price))
        with self._transaction() as db:
            state = self._summary(db)
            require(not state["frozen"], "campaign_frozen")
            require(not state["inflight_count"], "request_inflight")
            self.require_task_not_failed(db)
            db.execute(
                "CREATE TABLE IF NOT EXISTS stage40_request_attempt (reservation_id TEXT PRIMARY KEY REFERENCES reservation(reservation_id),public_input_sha256 TEXT NOT NULL,task_id TEXT NOT NULL,started_at TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS provider_attempt (reservation_id TEXT PRIMARY KEY REFERENCES reservation(reservation_id),task_id TEXT NOT NULL,connection_json TEXT NOT NULL,wire_sha256 TEXT NOT NULL)"
            )
            for table in ("stage40_request_attempt", "provider_attempt"):
                for action in ("UPDATE", "DELETE"):
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()}_immutable BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'attempt_append_only'); END"
                    )
            ident = uuid4().hex
            db.execute(
                "INSERT INTO reservation(reservation_id,campaign_id,input_tokens,output_tokens,input_price,output_price,reserved_cny,state) VALUES(?,?,?,?,?,?,?,'reserved')",
                (
                    ident,
                    CAMPAIGN_ID,
                    input_tokens,
                    output_tokens,
                    str(input_price),
                    str(output_price),
                    _money(held),
                ),
            )
            db.execute(
                "INSERT INTO stage40_request_attempt VALUES(?,?,?,?)",
                (ident, bound.public_input_sha256, self.task_id, self.clock().isoformat()),
            )
            db.execute(
                "INSERT INTO provider_attempt VALUES(?,?,?,?)",
                (ident, self.task_id, services.canonical_json(bound.connection), bound.wire_sha256),
            )
        return ident
