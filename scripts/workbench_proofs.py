"""Renew the workbench's public DeepSeek proof bundle without a model request."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from arknights_agent.runtime import stage26_current_model as official
from arknights_agent.runtime.budget import PRICE_TTL

RENEW_BEFORE = timedelta(days=1)
MAX_PRICE_BYTES = 512 * 1024


class PriceSourceChanged(ValueError):
    pass


def fetch_price():
    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        with client.stream("GET", official.PRICE_URL, headers={"Cache-Control": "no-cache"}) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("official price response is incomplete")
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_PRICE_BYTES:
                    raise ValueError("official price response too large")
            return bytes(body)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def refresh(root: Path, original_model: Path):
    body = fetch_price()
    digest = hashlib.sha256(body).hexdigest()
    checked_at = datetime.now(UTC)
    if digest not in official.REVIEWED_PRICE_BODY_SHA256S:
        # Save the candidate for review. It cannot become the active proof.
        root.mkdir(parents=True, exist_ok=True)
        (root / f"unreviewed-price-{digest}.html").write_bytes(body)
        raise PriceSourceChanged()
    generations = root / "generations"
    generations.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".pending-", dir=generations) as staging:
        bundle = Path(staging) / "bundle"
        pricing, model = bundle / "pricing", bundle / "model"
        pricing.mkdir(parents=True)
        model.mkdir()
        stamp = checked_at.isoformat()
        fetch = {
            "url": official.PRICE_URL, "final_url": official.PRICE_URL, "http_status": 200,
            "checked_at": stamp, "sha256": digest, "byte_size": len(body),
        }
        (pricing / "official-pricing.html").write_bytes(body)
        write_json(pricing / "official-fetch-receipt.json", fetch)
        write_json(pricing / "official-pricing-receipt.json", {**fetch, **official.PRICE_CONTRACT})
        manifest = "model-file-fetch-receipts.json"
        for name in (*official.SOURCE_HASHES, manifest):
            target = model / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original_model / name, target)
        price = official.price_receipt(pricing)
        write_json(model / "model-identity-revalidation.json", {
            "model": official.MODEL, "model_alias": official.MODEL_ALIAS, "revision": official.REVISION,
            "checked_at": stamp, "price_receipt_sha256": price["receipt_sha256"],
            "original_fetch_manifest_sha256": official.sha(model / manifest),
        })
        official.current_sources(model)
        generation = generations / checked_at.strftime("%Y%m%dT%H%M%S%fZ")
        bundle.rename(generation)
        link = Path(staging) / "current"
        link.symlink_to(generation.resolve(), target_is_directory=True)
        link.replace(root / "current")
    return generation


def expiry(checked_at):
    return datetime.fromisoformat(checked_at) + PRICE_TTL
