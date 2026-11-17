"""Shared provider capabilities and public model metadata (never credentials).

Catalog prices are reference estimates, not provider invoices. The fixed CNY
factor is a reservation assumption, not a live exchange rate. Unknown models
are not silently assigned a context window, a price, or tool capability.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx

PROVIDERS = {
    "deepseek": {"name": "DeepSeek", "base_url": "https://api.deepseek.com", "wire": "chat", "balance": True},
    "openai": {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "wire": "responses", "balance": False},
    "anthropic": {"name": "Anthropic", "base_url": "https://api.anthropic.com/v1", "wire": "messages", "balance": False},
    "compatible": {"name": "通用兼容", "base_url": "", "wire": "chat", "balance": False},
}
CATALOG_URL = "https://models.dev/api.json"
CATALOG_PATH = Path(__file__).with_name("provider_catalog.json")
MODEL_ID = re.compile(r"^[A-Za-z0-9_.:/-]{1,150}$")
ESTIMATE_FACTOR = Decimal("8")


def extract_catalog(raw, checked_at):
    result = {"source": CATALOG_URL, "checked_at": checked_at, "usd_cny_estimate_factor": str(ESTIMATE_FACTOR), "providers": {}}
    for provider in PROVIDERS:
        rows = {}
        for ident, m in raw.get(provider, {}).get("models", {}).items():
            if not MODEL_ID.fullmatch(ident) or not isinstance(m, dict):
                continue
            limits, cost = m.get("limit", {}), m.get("cost", {})
            # Take the largest standard context tier and cache-write rate. Do
            # not mix optional priority/service-tier rates into normal calls.
            tiers = [cost, *cost.get("tiers", []), cost.get("context_over_200k", {})]
            def rate(keys):
                values = [Decimal(str(t[k])) for t in tiers if isinstance(t, dict) for k in keys if isinstance(t.get(k), (int, float)) and t[k] >= 0]
                return str(max(values) * ESTIMATE_FACTOR) if values else None
            context = limits.get("input", limits.get("context"))
            total = limits.get("context")
            if type(context) is not int or type(total) is not int or total < 4096:
                continue
            # Application envelope, not a claim that every model supports 512k.
            cap = min(total, context + 8192, 524288)
            rows[ident] = {"id": ident, "name": str(m.get("name", ident))[:160],
                "context_tokens": cap, "max_output_tokens": min(limits.get("output", 4096), 8192, cap // 2),
                "input_cny_per_million": rate(["input", "cache_write", "cache_write_1h"]),
                "output_cny_per_million": rate(["output", "reasoning"]),
                "tool_call": m.get("tool_call") is True,
                "text_output": "text" in m.get("modalities", {}).get("output", []),
                "supported_wire": not (provider == "openai" and ("realtime" in ident or ident.startswith("gpt-live"))),
                "status": m.get("status", "active")}
        result["providers"][provider] = rows
    return result


def read_catalog(cache_path=None, *, refresh=False):
    paths = [p for p in (cache_path, CATALOG_PATH) if p]
    catalog = None
    for path in paths:
        try:
            candidate = json.loads(Path(path).read_text())
            age = datetime.now(UTC) - datetime.fromisoformat(candidate["checked_at"])
            if timedelta(0) <= age < timedelta(days=7):
                return candidate
            catalog = candidate
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if refresh:
        try:
            # This public request has no credential header and never follows redirects.
            with httpx.Client(timeout=8, trust_env=False, follow_redirects=False) as client:
                with client.stream("GET", CATALOG_URL) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 20_000_000:
                            raise ValueError()
            catalog = extract_catalog(json.loads(raw), datetime.now(UTC).isoformat())
            if cache_path:
                from .credentials import _atomic_private
                _atomic_private(Path(cache_path), json.dumps(catalog).encode())
            return catalog
        except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError):
            pass
    # Stale metadata may be displayed, but cannot auto-authorize new pricing.
    return {**(catalog or {"providers": {}}), "stale": True}


def model_defaults(protocol, model, catalog):
    if catalog.get("stale"):
        return None
    entry = catalog.get("providers", {}).get(protocol, {}).get(model)
    if not entry or not entry.get("tool_call") or not entry.get("text_output") or entry.get("supported_wire") is False or entry.get("status") == "retired":
        return None
    if not all(entry.get(k) and Decimal(entry[k]) > 0 for k in ("input_cny_per_million", "output_cny_per_million")):
        return None
    return {k: entry[k] for k in ("context_tokens", "max_output_tokens", "input_cny_per_million", "output_cny_per_million")}
