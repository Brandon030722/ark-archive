"""Opt-in future tool turns with byte-based, source-pinned input reservations.

The public model tokenizer has an identity normalizer and ByteLevel BPE. Each
token consumes at least one UTF-8 byte; merges can only lower the token count.
We render the complete official chat template in four JSON/argument variants,
bound each rendering at 64 KiB, and reserve 131072 input tokens: the byte upper
bound plus a separate 65536-token allowance for API-specific structural text.
This never recalculates a historical reservation or changes the campaign cap.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from arknights_agent.runtime.budget import CAMPAIGN_ID, BudgetError

PROFILE_ID = "stage15-official-byte-bounded-tool-turns-v1"
BASELINE_COUNT = 200
MAX_ADDITIONAL_REQUESTS = 48
STAGE16_PROFILE_ID = "stage16-official-byte-bounded-tool-turns-v1"
STAGE16_BASELINE_COUNT = 229
STAGE16_MAX_ADDITIONAL_REQUESTS = 64
STAGE16_AUTHORIZATION_TEXT = (
    "继续检查七个肉鸽的藏品、事件和结局问答，做真实模型测试，把类似的遗漏和引用错误也修复，"
    "还是按原来的18元总预算，不充值。"
)
MAX_WIRE_BYTES = 16 * 1024
MAX_RENDERED_BYTES = 64 * 1024
API_STRUCTURE_TOKEN_ALLOWANCE = 65_536
BOUNDED_INPUT_TOKENS = MAX_RENDERED_BYTES + API_STRUCTURE_TOKEN_ALLOWANCE
MAX_MESSAGES = 20
MAX_TOOLS = 12
MAX_TOOL_CALLS = 8
AUTHORIZATION_TEXT = (
    "继续检查其他干员和卫戍协议问答，把类似的遗漏和引用错误也一并修复。\n"
    "你还有这么多钱，然后把明日方舟那几个肉鸽做进去"
)
MODEL_REPOSITORY = "deepseek-ai/DeepSeek-V4.1-Flash"
MODEL_REVISION = "5254448103b6254fc646dacbca1cbbf20e9216c1"
SOURCE_HASHES = {
    "chat_template.jinja": "d959d804d5101b79a49b6ff1bf3c54cd5affa9a5a78c503d925f3090040b31c3",
    "tokenizer_config.json": "6ac8c8dc065ed118161d02dd532749ae3f52c243deac27872134fae2f50d8547",
    "tokenizer.json": "c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b",
    "config.json": "8be45ce0476004a3f529fd896115a4a2e800a129ad2d3ec05b16050f52e21879",
}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _sha(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def reservation_rows_hash(rows):
    return _sha(sorted(rows, key=lambda row: row["reservation_id"]))


def ledger_tables_hash(snapshot):
    """Canonical identity of original tables and, when present, registered receipts."""
    base = {"campaign", "reservation", "campaign_review", "reviewed_reservation"}
    if not isinstance(snapshot, dict) or set(snapshot) not in (base, base | {"bounded_continuation"}):
        raise BudgetError("continuation_baseline_invalid")
    return _sha({name: sorted(rows, key=_canonical) for name, rows in snapshot.items()})


def build_continuation_receipt(snapshot, *, authorization_text=AUTHORIZATION_TEXT,
                               max_additional_requests=MAX_ADDITIONAL_REQUESTS):
    """Bind a new human request to the complete existing 200-row accounting."""
    if (authorization_text != AUTHORIZATION_TEXT
        or type(max_additional_requests) is not int
        or not 1 <= max_additional_requests <= MAX_ADDITIONAL_REQUESTS
        or not isinstance(snapshot, dict)
        or set(snapshot) != {"campaign", "reservation", "campaign_review", "reviewed_reservation"}
        or len(snapshot.get("campaign", [])) != 1
        or len(snapshot.get("reservation", [])) != BASELINE_COUNT):
        raise BudgetError("continuation_receipt_invalid")
    campaign = snapshot["campaign"][0]
    if (campaign["campaign_id"] != CAMPAIGN_ID or campaign["cap_cny"] != "14.40000000"
        or campaign["budget_cny"] != "18" or campaign["safety_fraction"] != "0.8"
        or campaign["frozen"] != 0
        or any(row["state"] == "reserved" for row in snapshot["reservation"])):
        raise BudgetError("continuation_baseline_invalid")
    receipt = {
        "profile_id": PROFILE_ID, "campaign_id": CAMPAIGN_ID,
        "authorization_text": authorization_text,
        "authorization_sha256": hashlib.sha256(authorization_text.encode("utf-8")).hexdigest(),
        "authorization_scope": "public_game_questions_same_18cny_campaign_no_topups",
        "baseline_count": BASELINE_COUNT,
        "baseline_reservation_ids": sorted(row["reservation_id"] for row in snapshot["reservation"]),
        "baseline_reservation_sha256": reservation_rows_hash(snapshot["reservation"]),
        "baseline_tables_sha256": ledger_tables_hash(snapshot),
        "max_additional_requests": max_additional_requests,
        "budget_cny": "18", "safety_fraction": "0.8", "cap_cny": "14.40000000",
        "model_id": "deepseek-flash", "model_repository": MODEL_REPOSITORY,
        "model_revision": MODEL_REVISION, "source_hashes": SOURCE_HASHES,
        "max_wire_bytes": MAX_WIRE_BYTES, "max_rendered_bytes": MAX_RENDERED_BYTES,
        "api_structure_token_allowance": API_STRUCTURE_TOKEN_ALLOWANCE,
        "input_token_upper_bound": BOUNDED_INPUT_TOKENS,
        "historical_reservations_reestimated": False,
        "unknown_reservations_released": False,
    }
    receipt["continuation_id"] = _sha(receipt)
    validate_receipt(receipt)
    return receipt


def build_stage16_continuation_receipt(snapshot, *, authorization_text=STAGE16_AUTHORIZATION_TEXT):
    """Append this exact human request to the immutable 229-row, five-table baseline."""
    if (authorization_text != STAGE16_AUTHORIZATION_TEXT or not isinstance(snapshot, dict)
        or set(snapshot) != {"campaign", "reservation", "campaign_review", "reviewed_reservation",
                             "bounded_continuation"}
        or len(snapshot["campaign"]) != 1
        or len(snapshot["reservation"]) != STAGE16_BASELINE_COUNT
        or len(snapshot["bounded_continuation"]) != 1):
        raise BudgetError("continuation_baseline_invalid")
    try:
        registration = snapshot["bounded_continuation"][0]
        parent = validate_receipt(json.loads(registration["receipt_json"]))
        campaign = snapshot["campaign"][0]
        parent_ids = set(parent["baseline_reservation_ids"])
        original = [row for row in snapshot["reservation"] if row["reservation_id"] in parent_ids]
        parent_snapshot = {name: rows for name, rows in snapshot.items()
                           if name != "bounded_continuation"}
        parent_snapshot["reservation"] = original
        valid = (parent["profile_id"] == PROFILE_ID
                 and registration["continuation_id"] == parent["continuation_id"]
                 and registration["campaign_id"] == CAMPAIGN_ID
                 and campaign["campaign_id"] == CAMPAIGN_ID and campaign["frozen"] == 0
                 and ledger_tables_hash(parent_snapshot) == parent["baseline_tables_sha256"]
                 and all(row["state"] == "settled" for row in snapshot["reservation"]
                         if row["reservation_id"] not in parent_ids)
                 and not any(row["state"] == "reserved" for row in original)
                 and STAGE16_BASELINE_COUNT <= parent["baseline_count"] + parent["max_additional_requests"])
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise BudgetError("continuation_baseline_invalid")
    receipt = copy.deepcopy(parent)
    receipt.pop("continuation_id")
    receipt.update(profile_id=STAGE16_PROFILE_ID, authorization_text=authorization_text,
                   authorization_sha256=hashlib.sha256(authorization_text.encode("utf-8")).hexdigest(),
                   baseline_count=STAGE16_BASELINE_COUNT,
                   baseline_reservation_ids=sorted(row["reservation_id"] for row in snapshot["reservation"]),
                   baseline_reservation_sha256=reservation_rows_hash(snapshot["reservation"]),
                   baseline_tables_sha256=ledger_tables_hash(snapshot),
                   max_additional_requests=STAGE16_MAX_ADDITIONAL_REQUESTS,
                   parent_continuation_id=parent["continuation_id"])
    receipt["continuation_id"] = _sha(receipt)
    validate_receipt(receipt)
    return receipt


def validate_receipt(receipt):
    if not isinstance(receipt, dict):
        raise BudgetError("continuation_receipt_invalid")
    required = {
        "profile_id", "campaign_id", "authorization_text", "authorization_sha256",
        "authorization_scope", "baseline_count", "baseline_reservation_ids",
        "baseline_reservation_sha256", "baseline_tables_sha256", "max_additional_requests",
        "budget_cny", "safety_fraction", "cap_cny", "model_id", "model_repository",
        "model_revision", "source_hashes", "max_wire_bytes", "max_rendered_bytes",
        "api_structure_token_allowance", "input_token_upper_bound",
        "historical_reservations_reestimated", "unknown_reservations_released", "continuation_id",
    }
    stage16 = receipt.get("profile_id") == STAGE16_PROFILE_ID
    profile_id = STAGE16_PROFILE_ID if stage16 else PROFILE_ID
    authorization_text = STAGE16_AUTHORIZATION_TEXT if stage16 else AUTHORIZATION_TEXT
    baseline_count = STAGE16_BASELINE_COUNT if stage16 else BASELINE_COUNT
    max_additional = STAGE16_MAX_ADDITIONAL_REQUESTS if stage16 else MAX_ADDITIONAL_REQUESTS
    if stage16:
        required.add("parent_continuation_id")
    static = {
        "profile_id": profile_id, "campaign_id": CAMPAIGN_ID,
        "authorization_text": authorization_text,
        "authorization_sha256": hashlib.sha256(authorization_text.encode("utf-8")).hexdigest(),
        "authorization_scope": "public_game_questions_same_18cny_campaign_no_topups",
        "baseline_count": baseline_count,
        "budget_cny": "18", "safety_fraction": "0.8", "cap_cny": "14.40000000",
        "model_id": "deepseek-flash", "model_repository": MODEL_REPOSITORY,
        "model_revision": MODEL_REVISION, "source_hashes": SOURCE_HASHES,
        "max_wire_bytes": MAX_WIRE_BYTES, "max_rendered_bytes": MAX_RENDERED_BYTES,
        "api_structure_token_allowance": API_STRUCTURE_TOKEN_ALLOWANCE,
        "input_token_upper_bound": BOUNDED_INPUT_TOKENS,
        "historical_reservations_reestimated": False,
        "unknown_reservations_released": False,
    }
    try:
        body = {key: value for key, value in receipt.items() if key != "continuation_id"}
        identifiers = receipt["baseline_reservation_ids"]
        valid = (set(receipt) == required
                 and all(_canonical(receipt[key]) == _canonical(value)
                         for key, value in static.items())
                 and type(receipt["max_additional_requests"]) is int
                 and 1 <= receipt["max_additional_requests"] <= max_additional
                 and (not stage16 or receipt["max_additional_requests"] == max_additional)
                 and (not stage16 or isinstance(receipt["parent_continuation_id"], str)
                      and re.fullmatch(r"[0-9a-f]{64}", receipt["parent_continuation_id"]))
                 and isinstance(identifiers, list) and len(identifiers) == baseline_count
                 and len(set(identifiers)) == baseline_count and identifiers == sorted(identifiers)
                 and all(isinstance(ident, str) and re.fullmatch(r"[0-9a-f]{32}", ident)
                         for ident in identifiers)
                 and all(isinstance(receipt[key], str) and
                         re.fullmatch(r"[0-9a-f]{64}", receipt[key])
                         for key in ("baseline_reservation_sha256", "baseline_tables_sha256",
                                     "continuation_id"))
                 and receipt["continuation_id"] == _sha(body))
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise BudgetError("continuation_receipt_invalid")
    return receipt


def _verified_template(root):
    sources = {}
    for name, expected in SOURCE_HASHES.items():
        try:
            raw = (Path(root) / name).read_bytes()
        except OSError:
            raise BudgetError("bounded_tokenizer_source_unavailable") from None
        if hashlib.sha256(raw).hexdigest() != expected:
            raise BudgetError("bounded_tokenizer_source_mismatch")
        sources[name] = raw
    tokenizer = json.loads(sources["tokenizer.json"])
    config = json.loads(sources["tokenizer_config.json"])
    model = tokenizer["model"]
    pre = tokenizer["pre_tokenizer"]["pretokenizers"]
    if (tokenizer["normalizer"] != {"type": "Sequence", "normalizers": []}
        or model["type"] != "BPE" or model["dropout"] is not None
        or model["unk_token"] is not None or model["continuing_subword_prefix"] is not None
        or model["end_of_word_suffix"] is not None or tokenizer["padding"] is not None
        or tokenizer["truncation"] is not None
        or any(stage["type"] != "Split" for stage in pre[:-1])
        or pre[-1] != {"type": "ByteLevel", "add_prefix_space": False,
                       "trim_offsets": True, "use_regex": False}
        or not all(isinstance(token["content"], str) and token["content"]
                   for token in tokenizer["added_tokens"])
        or config["add_bos_token"] is not False or config["add_eos_token"] is not False
        or config["model_max_length"] != 1_048_576):
        raise BudgetError("bounded_tokenizer_proof_invalid")
    return sources["chat_template.jinja"].decode("utf-8")


def _render(template, payload):
    try:
        from jinja2 import StrictUndefined
        from jinja2.sandbox import ImmutableSandboxedEnvironment
    except ImportError:
        raise BudgetError("bounded_template_dependency_missing") from None
    outputs = []
    for ensure_ascii in (False, True):
        environment = ImmutableSandboxedEnvironment(undefined=StrictUndefined,
                                                     trim_blocks=True, lstrip_blocks=True)
        environment.filters["tojson"] = lambda value, ascii=ensure_ascii: json.dumps(
            value, ensure_ascii=ascii, allow_nan=False)

        def reject(_message):
            raise BudgetError("bounded_template_render_invalid")

        environment.globals["raise_exception"] = reject
        compiled = environment.from_string(template)
        for parse_arguments in (False, True):
            messages = copy.deepcopy(payload["messages"])
            if parse_arguments:
                for message in messages:
                    for call in message.get("tool_calls", []):
                        arguments = call["function"]["arguments"]
                        if isinstance(arguments, str):
                            call["function"]["arguments"] = json.loads(arguments)
            try:
                rendered = compiled.render(messages=messages, tools=payload.get("tools"),
                                           add_generation_prompt=True, thinking_mode="chat",
                                           enable_thinking=False).encode("utf-8")
            except Exception as exc:
                if isinstance(exc, BudgetError):
                    raise
                raise BudgetError("bounded_template_render_invalid") from None
            outputs.append(rendered)
    return tuple(outputs)


@dataclass(frozen=True)
class BoundedRequest:
    """A request-specific validated upper bound, never a usage estimate."""

    continuation_id: str
    wire_sha256: str
    wire_bytes: int
    rendered_bytes: tuple[int, ...]
    input_tokens: int = BOUNDED_INPUT_TOKENS
    profile_id: str = PROFILE_ID
    public_input_sha256: str | None = None
    _proof: object = field(default=None, repr=False, compare=False)

    def _fingerprint(self):
        return _sha([self.profile_id, self.continuation_id, self.wire_sha256,
                     self.wire_bytes, self.rendered_bytes, self.input_tokens, self.public_input_sha256])

    def validate(self):
        if (self.profile_id not in {PROFILE_ID, STAGE16_PROFILE_ID}
            or self.input_tokens != BOUNDED_INPUT_TOKENS
            or type(self.wire_bytes) is not int or not 1 <= self.wire_bytes <= MAX_WIRE_BYTES
            or len(self.rendered_bytes) != 4
            or any(type(value) is not int or not 1 <= value <= MAX_RENDERED_BYTES
                   for value in self.rendered_bytes)
            or not isinstance(self.continuation_id, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.continuation_id)
            or not isinstance(self.wire_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.wire_sha256)
            or self._proof != (_BOUND_PROOF, self._fingerprint())):
            raise BudgetError("bounded_request_proof_invalid")
        return self

    def audit(self):
        self.validate()
        return {"profile_id": self.profile_id, "continuation_id": self.continuation_id,
                "wire_sha256": self.wire_sha256, "wire_bytes": self.wire_bytes,
                "official_rendered_bytes": list(self.rendered_bytes),
                "input_token_upper_bound": self.input_tokens,
                "api_structure_token_allowance": API_STRUCTURE_TOKEN_ALLOWANCE,
                "model_revision": MODEL_REVISION, "official_source_hashes": SOURCE_HASHES,
                "bound_basis": "identity_ByteLevel_BPE_complete_UTF8_template_plus_structure_allowance"}


_BOUND_PROOF = object()


@dataclass(frozen=True, init=False)
class BoundedToolProfile:
    receipt: dict
    continuation_id: str
    template: str = field(repr=False)

    def __init__(self, *, source_root, receipt):
        validate_receipt(receipt)
        object.__setattr__(self, "receipt", copy.deepcopy(receipt))
        object.__setattr__(self, "continuation_id", receipt["continuation_id"])
        object.__setattr__(self, "template", _verified_template(source_root))

    def bind_request(self, provider, messages, tools, *, max_output_tokens):
        from arknights_agent.runtime.contracts import ProviderError
        from arknights_agent.runtime.providers import DeepSeekProvider

        validate_receipt(self.receipt)
        if (not isinstance(provider, DeepSeekProvider)
            or provider.model != "deepseek-flash"
            or getattr(provider._settings, "deepseek_model", None) != "deepseek-flash"
            or type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 1024
            or not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES
            or any(not isinstance(message, dict) for message in messages)
            or not isinstance(tools, list) or not 1 <= len(tools) <= MAX_TOOLS
            or sum(len(message.get("tool_calls", [])) for message in messages) > MAX_TOOL_CALLS):
            raise BudgetError("bounded_request_shape_invalid")
        try:
            payload = provider._request(messages, tools, max_output_tokens)
            wire = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")).encode("utf-8")
        except (ProviderError, UnicodeError, ValueError, TypeError, RecursionError):
            raise BudgetError("bounded_request_shape_invalid") from None
        if len(wire) > MAX_WIRE_BYTES:
            raise BudgetError("bounded_request_wire_limit")
        renderings = _render(self.template, payload)
        if any(len(rendering) > MAX_RENDERED_BYTES for rendering in renderings):
            raise BudgetError("bounded_request_template_limit")
        public_input_sha256 = None
        if len(messages) >= 2 and messages[1].get('role') == 'user':
            try:
                public_input = json.loads(messages[1]['content'])
                if (set(public_input) == {'question', 'module', 'release_id'}
                    and all(isinstance(v, str) for v in public_input.values())):
                    public_input_sha256 = _sha(public_input)
            except (KeyError, TypeError, ValueError):
                pass
        bound = BoundedRequest(self.continuation_id, hashlib.sha256(wire).hexdigest(),
                               len(wire), tuple(map(len, renderings)),
                               profile_id=self.receipt["profile_id"],
                               public_input_sha256=public_input_sha256)
        object.__setattr__(bound, "_proof", (_BOUND_PROOF, bound._fingerprint()))
        return bound.validate()
