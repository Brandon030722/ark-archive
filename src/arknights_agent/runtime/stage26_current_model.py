"""Stage26 append-only current model pricing and complete wire-format proof.

This never changes or replaces the historical bounded profile. The caller must
also retain that genuine original proof and reserve the same 131072 inputs.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

from arknights_agent.runtime.budget import PRICE_TTL, BudgetError
from arknights_agent.runtime.contracts import strict_json_loads

MODEL = "DeepSeek-V4.1-Flash-1017"
MODEL_ALIAS = "deepseek-flash"
REPOSITORY = "deepseek-ai/DeepSeek-V4.1-Flash-1017"
REVISION = "1bae0e1088993b9b82de08f59de5be5a34e42170"
PRICE_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
# Keep the historical byte pin for its original receipts and proofs. The
# additional body was separately fetched and reviewed: only the runtime JS
# asset fingerprint changed; every text node and tariff/model cell is equal.
# Exact reviewed bodies only; never HTML/text normalization at runtime.
PRICE_BODY_SHA256 = "7428747bd2fe3a0b1935a3454d1f29640c1d863a5a14be0a5f8bcb873f407b49"
PRICE_BODY_20261019_SHA256 = "556097caba8adfeeed6455714bffd731c7f60db520fcee06a922eba5840663a3"
# Nov 8: tariff/model tables unchanged; Markdown controls added and the expired
# Nov 1 search/fetch free-period notice removed. Raw capture/review: Stage68.
PRICE_BODY_20261108_SHA256 = "6205cf5d53a7903827bf21938eefb6a8ab6041bb5a73fcf43e4bdd6adc8d344b"
# Nov 16: added ancillary service pricing and revised navigation. The
# reviewed Flash model row, token tariff and billing hours are unchanged.
PRICE_BODY_20261116_SHA256 = "c85eeb649ff392593ad0048babcbf63d73b3e6f05cbb2626808927618c01cb7b"
REVIEWED_PRICE_BODY_SHA256S = frozenset({
    PRICE_BODY_SHA256, PRICE_BODY_20261019_SHA256, PRICE_BODY_20261108_SHA256,
    PRICE_BODY_20261116_SHA256,
})
PRICE_CONTRACT = {
    "url": PRICE_URL, "flash_model_version": MODEL, "model_alias": MODEL_ALIAS,
    "cache_miss_peak_input_cny_per_million": "2", "peak_output_cny_per_million": "8",
    "context_tokens": 1048576, "cache_hit_offpeak": "0.02", "cache_hit_peak": "0.04",
    "cache_miss_offpeak": "1", "cache_miss_peak": "2", "output_offpeak": "4", "output_peak": "8",
}
SOURCE_HASHES = {
    "tokenizer.json": "c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b",
    "tokenizer_config.json": "6ac8c8dc065ed118161d02dd532749ae3f52c243deac27872134fae2f50d8547",
    "config.json": "8be45ce0476004a3f529fd896115a4a2e800a129ad2d3ec05b16050f52e21879",
    "encoding/encoding.py": "13231c592e958fb39e2af6001dae69d90e7a36b265702b9b390236fb387599e2",
    "README.md": "b72b796e4f4c739eca5d0ad360a7679b39118ca7eb64891f7151a527b292c42a",
}
MAX_RENDERED_BYTES = 65536
API_STRUCTURE_TOKEN_ALLOWANCE = 65536
INPUT_TOKEN_UPPER_BOUND = 131072
MAX_EXPLICIT_OUTPUT_TOKENS = 8192


def require(value, code):
    if not value:
        raise BudgetError(code)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _fresh(stamp):
    try:
        value = datetime.fromisoformat(stamp)
        return value.tzinfo is not None and timedelta(0) <= datetime.now(UTC) - value < PRICE_TTL
    except (TypeError, ValueError):
        return False


def price_receipt(source_root):
    root = Path(source_root)
    try:
        receipt_path = root / "official-pricing-receipt.json"
        receipt = strict_json_loads(receipt_path.read_bytes())
        fetch = strict_json_loads((root / "official-fetch-receipt.json").read_bytes())
        require(_fresh(receipt["checked_at"]), "stage26_official_price_not_fresh")
        require(
            receipt["sha256"] in REVIEWED_PRICE_BODY_SHA256S
            and all(receipt[k] == v for k, v in PRICE_CONTRACT.items()),
            "stage26_official_price_contract_differs",
        )
        require(
            fetch["url"] == fetch["final_url"] == PRICE_URL
            and fetch["http_status"] == 200
            and fetch["checked_at"] == receipt["checked_at"]
            and fetch["sha256"] == receipt["sha256"]
            and fetch["byte_size"] == receipt["byte_size"],
            "stage26_official_price_fetch_differs",
        )
        body = root / "official-pricing.html"
        require(
            sha(body) == receipt["sha256"] and body.stat().st_size == receipt["byte_size"],
            "stage26_official_price_original_differs",
        )
        return {**receipt, "receipt_sha256": sha(receipt_path)}
    except (OSError, KeyError, TypeError, ValueError):
        raise BudgetError("stage26_official_price_invalid") from None


def current_sources(source_root):
    root = Path(source_root)
    try:
        manifest_path = root / "model-file-fetch-receipts.json"
        manifest = strict_json_loads(manifest_path.read_bytes())
        revalidation_path = root / "model-identity-revalidation.json"
        identity_checked_again = revalidation_path.is_file()
        if identity_checked_again:
            # An official price page confirms the serving model identity. The
            # encoder files are immutable, so recheck their pinned bytes and
            # keep their original acquisition receipts and dates intact.
            revalidation = strict_json_loads(revalidation_path.read_bytes())
            fresh_price = price_receipt(root.parent / "pricing")
            require(
                revalidation == {
                    "model": MODEL, "model_alias": MODEL_ALIAS, "revision": REVISION,
                    "checked_at": fresh_price["checked_at"],
                    "price_receipt_sha256": fresh_price["receipt_sha256"],
                    "original_fetch_manifest_sha256": sha(manifest_path),
                },
                "stage26_current_model_revalidation_differs",
            )
        require(
            manifest["repository"] == REPOSITORY and manifest["revision"] == REVISION,
            "stage26_current_model_revision_differs",
        )
        for name, expected in SOURCE_HASHES.items():
            row = [r for r in manifest["sources"] if r["name"] == name]
            require(len(row) == 1, "stage26_current_model_source_receipt_invalid")
            row = row[0]
            original_checked_at = datetime.fromisoformat(row["checked_at"])
            valid_date = _fresh(row["checked_at"])
            if identity_checked_again:
                valid_date = (original_checked_at.tzinfo is not None
                              and original_checked_at <= datetime.fromisoformat(fresh_price["checked_at"]))
            require(
                row["url"] == f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{name}"
                and row["http_status"] == 200
                and valid_date
                and row["sha256"] == expected
                and sha(root / name) == expected
                and (root / name).stat().st_size == row["byte_size"],
                "stage26_current_model_source_differs",
            )
        # These three exact hashes are the original validated identity ByteLevel
        # tokenizer/config. The new model explicitly ships them unchanged.
        return root
    except (OSError, KeyError, TypeError, ValueError):
        raise BudgetError("stage26_current_model_sources_invalid") from None


def render_current_payload(payload, source_root, *, max_output_tokens=1024):
    root = current_sources(source_root)
    try:
        require(
            payload["model"] == MODEL_ALIAS
            and payload["stream"] is False
            and payload["thinking"] == {"type": "disabled"}
            and type(max_output_tokens) is int and max_output_tokens in {1024, MAX_EXPLICIT_OUTPUT_TOKENS}
            and type(payload["max_tokens"]) is int
            and 1 <= payload["max_tokens"] <= max_output_tokens,
            "stage26_current_model_payload_invalid",
        )
        # Load only the hash-pinned official encoder. It has stdlib imports,
        # declarations and function definitions; its file-opening helper is never
        # called. Each variant gets an isolated module to avoid mutable globals.
        source = (root / "encoding/encoding.py").read_bytes()
        outputs = []
        for ensure_ascii in (False, True):
            module = ModuleType("stage26_verified_official_encoding")
            exec(compile(source, str(root / "encoding/encoding.py"), "exec"), module.__dict__)
            module.to_json = lambda value, ascii=ensure_ascii: json.dumps(
                value, ensure_ascii=ascii, allow_nan=False
            )
            for parse_arguments in (False, True):
                messages = copy.deepcopy(payload["messages"])
                # This is the official load_cases mapping for a Chat Completions
                # request with top-level tools; no tool definitions are omitted.
                messages[0]["tools"] = copy.deepcopy(payload["tools"])
                if parse_arguments:
                    for message in messages:
                        for call in message.get("tool_calls", []):
                            arguments = call["function"]["arguments"]
                            if isinstance(arguments, str):
                                call["function"]["arguments"] = json.loads(arguments)
                rendered, media = module.encode_messages(
                    messages, thinking_mode="chat", return_multi_modal_data=True
                )
                require(media == {"images": []}, "stage26_current_model_nontext_request")
                outputs.append(rendered.encode("utf-8"))
        return tuple(outputs)
    except (
        OSError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        UnicodeError,
        AssertionError,
        NotImplementedError,
    ):
        raise BudgetError("stage26_current_model_render_invalid") from None


def prove_current_payload(payload, source_root, *, max_output_tokens=1024):
    renderings = render_current_payload(payload, source_root, max_output_tokens=max_output_tokens)
    require(
        len(renderings) == 4 and all(1 <= len(value) <= MAX_RENDERED_BYTES for value in renderings),
        "stage26_current_model_render_limit",
    )
    wire = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
        "utf-8"
    )
    proof = {
        "wire_sha256": hashlib.sha256(wire).hexdigest(),
        "wire_bytes": len(wire),
        "current_model_alias": MODEL_ALIAS,
        "current_model_version": MODEL,
        "current_model_repository": REPOSITORY,
        "current_model_revision": REVISION,
        "current_encoder_sha256": SOURCE_HASHES["encoding/encoding.py"],
        "current_official_source_hashes": dict(SOURCE_HASHES),
        "current_official_rendered_bytes": [len(value) for value in renderings],
        "current_rendered_sha256": [hashlib.sha256(value).hexdigest() for value in renderings],
        "api_structure_token_allowance": API_STRUCTURE_TOKEN_ALLOWANCE,
        "input_token_upper_bound": INPUT_TOKEN_UPPER_BOUND,
        "bound_basis": "current_official_encoder_UTF8_and_identical_identity_ByteLevel_tokenizer_plus_original_structure_allowance",
        "old_jinja_claimed_as_current_template": False,
    }
    if max_output_tokens != 1024:
        proof["output_token_upper_bound"] = payload["max_tokens"]
    return proof
