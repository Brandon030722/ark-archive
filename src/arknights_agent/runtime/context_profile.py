"""Current-model context proof independent of the historical 16 KiB profile.

The old receipts and bounds remain unchanged. Future web requests use their
existing continuation identity and the same 131072-token full reservation, but
prove the complete current official encoding directly. All four renderings
must still fit 64 KiB plus the original 65536-token structural allowance.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field

from arknights_agent.runtime import stage26_current_model as current
from arknights_agent.runtime.bounded_profile import (
    BOUNDED_INPUT_TOKENS,
    MAX_MESSAGES,
    MAX_TOOL_CALLS,
    MAX_TOOLS,
    STAGE16_PROFILE_ID,
    validate_receipt,
)
from arknights_agent.runtime.budget import BudgetError
from arknights_agent.runtime.contracts import ProviderError
from arknights_agent.runtime.providers import DeepSeekProvider

PROFILE_ID = "current-official-complete-context-v1"
MAX_WIRE_BYTES = 48 * 1024
REVIEW_WIRE_PROFILE_ID = "current-official-complete-context-64k-review-v1"
REVIEW_MAX_WIRE_BYTES = 64 * 1024
_PROOF = object()


def _require(value):
    if not value:
        raise BudgetError("current_context_proof_invalid")


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _sha(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _wire_policy(profile_id, max_wire_bytes, output_tokens, tool_call_limit, message_limit):
    if type(max_wire_bytes) is not int:
        return False
    if profile_id == PROFILE_ID:
        return max_wire_bytes == MAX_WIRE_BYTES
    return (profile_id == REVIEW_WIRE_PROFILE_ID and max_wire_bytes == REVIEW_MAX_WIRE_BYTES
            and output_tokens == current.MAX_EXPLICIT_OUTPUT_TOKENS
            and (tool_call_limit, message_limit) == (12, 32))


@dataclass(frozen=True)
class CurrentContextBoundedRequest:
    continuation_id: str
    wire_sha256: str
    wire_bytes: int
    public_input_sha256: str
    parent_receipt: dict = field(repr=False)
    current_model_proof: dict
    current_source_root: str = field(repr=False)
    input_tokens: int = BOUNDED_INPUT_TOKENS
    output_tokens: int = 1024
    tool_call_limit: int = MAX_TOOL_CALLS
    message_limit: int = MAX_MESSAGES
    profile_id: str = PROFILE_ID
    max_wire_bytes: int = MAX_WIRE_BYTES
    _proof: object = field(default=None, repr=False, compare=False)

    def _fingerprint(self):
        try:
            values = [self.continuation_id, self.wire_sha256, self.wire_bytes,
                      self.public_input_sha256, self.parent_receipt,
                      self.current_model_proof, self.current_source_root,
                      self.input_tokens, self.output_tokens, self.tool_call_limit,
                      self.message_limit, self.profile_id]
            # The default profile retains its exact original fingerprint.
            if self.profile_id != PROFILE_ID or self.max_wire_bytes != MAX_WIRE_BYTES:
                values.append(self.max_wire_bytes)
            return _sha(values)
        except (TypeError, ValueError, RecursionError):
            raise BudgetError("current_context_proof_invalid") from None

    def validate_current(self):
        _require(type(self) is CurrentContextBoundedRequest)
        parent = validate_receipt(self.parent_receipt)
        _require(parent["profile_id"] == STAGE16_PROFILE_ID
                 and self.continuation_id == parent["continuation_id"]
                 and _wire_policy(self.profile_id, self.max_wire_bytes, self.output_tokens,
                                  self.tool_call_limit, self.message_limit)
                 and type(self.input_tokens) is int
                 and self.input_tokens == current.INPUT_TOKEN_UPPER_BOUND == BOUNDED_INPUT_TOKENS
                 and type(self.output_tokens) is int
                 and self.output_tokens in {1024, current.MAX_EXPLICIT_OUTPUT_TOKENS}
                 and type(self.tool_call_limit) is int and type(self.message_limit) is int
                 and (self.tool_call_limit, self.message_limit) in {(MAX_TOOL_CALLS, MAX_MESSAGES), (12, 32)}
                 and (self.tool_call_limit == MAX_TOOL_CALLS
                      or self.output_tokens == current.MAX_EXPLICIT_OUTPUT_TOKENS)
                 and type(self.wire_bytes) is int and 1 <= self.wire_bytes <= self.max_wire_bytes
                 and isinstance(self.wire_sha256, str)
                 and re.fullmatch(r"[a-f0-9]{64}", self.wire_sha256)
                 and isinstance(self.public_input_sha256, str)
                 and re.fullmatch(r"[a-f0-9]{64}", self.public_input_sha256)
                 and self._proof == (_PROOF, self._fingerprint()))
        proof = self.current_model_proof
        expected = {
            "wire_sha256": self.wire_sha256, "wire_bytes": self.wire_bytes,
            "current_model_alias": current.MODEL_ALIAS,
            "current_model_version": current.MODEL,
            "current_model_repository": current.REPOSITORY,
            "current_model_revision": current.REVISION,
            "current_encoder_sha256": current.SOURCE_HASHES["encoding/encoding.py"],
            "current_official_source_hashes": current.SOURCE_HASHES,
            "api_structure_token_allowance": current.API_STRUCTURE_TOKEN_ALLOWANCE,
            "input_token_upper_bound": self.input_tokens,
            "bound_basis": "current_official_encoder_UTF8_and_identical_identity_ByteLevel_tokenizer_plus_original_structure_allowance",
            "old_jinja_claimed_as_current_template": False,
        }
        if self.output_tokens != 1024:
            expected["output_token_upper_bound"] = self.output_tokens
        if self.tool_call_limit != MAX_TOOL_CALLS:
            expected.update(tool_call_upper_bound=self.tool_call_limit, message_upper_bound=self.message_limit)
        if self.profile_id == REVIEW_WIRE_PROFILE_ID:
            expected.update(wire_profile_id=self.profile_id, wire_byte_upper_bound=self.max_wire_bytes)
        _require(isinstance(proof, dict)
                 and set(proof) == {*expected, "current_official_rendered_bytes", "current_rendered_sha256"}
                 and all(proof[key] == value for key, value in expected.items()))
        sizes, hashes = proof["current_official_rendered_bytes"], proof["current_rendered_sha256"]
        _require(isinstance(sizes, list) and len(sizes) == 4
                 and all(type(n) is int and 1 <= n <= current.MAX_RENDERED_BYTES for n in sizes)
                 and isinstance(hashes, list) and len(hashes) == 4
                 and all(isinstance(h, str) and re.fullmatch(r"[a-f0-9]{64}", h) for h in hashes))
        current.current_sources(self.current_source_root)
        return self

    def validate(self):
        return self.validate_current()

    def audit(self):
        self.validate_current()
        return {
            "profile_id": self.profile_id, "continuation_id": self.continuation_id,
            "wire_sha256": self.wire_sha256, "wire_bytes": self.wire_bytes,
            "max_wire_bytes": self.max_wire_bytes,
            "input_token_upper_bound": self.input_tokens,
            "output_token_upper_bound": self.output_tokens,
            "tool_call_upper_bound": self.tool_call_limit,
            "message_upper_bound": self.message_limit,
            "parent_profile_id": self.parent_receipt["profile_id"],
            "parent_receipt_sha256": _sha(self.parent_receipt),
            "historical_receipt_preserved": True,
            "historical_16kb_request_proof_claimed": False,
            "current_model_profile": copy.deepcopy(self.current_model_proof),
        }


class CurrentContextToolProfile:
    def __init__(self, *, receipt, current_source_root, max_output_tokens=1024,
                 max_tool_calls=MAX_TOOL_CALLS, max_messages=MAX_MESSAGES,
                 max_wire_bytes=MAX_WIRE_BYTES):
        parent = validate_receipt(receipt)
        profile_id = REVIEW_WIRE_PROFILE_ID if max_wire_bytes == REVIEW_MAX_WIRE_BYTES else PROFILE_ID
        _require(parent["profile_id"] == STAGE16_PROFILE_ID
                 and _wire_policy(profile_id, max_wire_bytes, max_output_tokens,
                                  max_tool_calls, max_messages)
                 and type(max_output_tokens) is int
                 and max_output_tokens in {1024, current.MAX_EXPLICIT_OUTPUT_TOKENS}
                 and type(max_tool_calls) is int and type(max_messages) is int
                 and (max_tool_calls, max_messages) in {(MAX_TOOL_CALLS, MAX_MESSAGES), (12, 32)}
                 and (max_tool_calls == MAX_TOOL_CALLS
                      or max_output_tokens == current.MAX_EXPLICIT_OUTPUT_TOKENS))
        self.receipt = copy.deepcopy(parent)
        self.current_source_root = str(current.current_sources(current_source_root).resolve())
        self.max_output_tokens = max_output_tokens
        self.max_tool_calls, self.max_messages = max_tool_calls, max_messages
        self.max_wire_bytes, self.profile_id = max_wire_bytes, profile_id

    @property
    def continuation_id(self):
        return self.receipt["continuation_id"]

    def _request_data(self, provider, messages, tools, *, max_output_tokens):
        parent = validate_receipt(self.receipt)
        _require(isinstance(provider, DeepSeekProvider)
                 and _wire_policy(self.profile_id, self.max_wire_bytes, self.max_output_tokens,
                                  self.max_tool_calls, self.max_messages)
                 and provider.model == current.MODEL_ALIAS
                 and provider._settings.deepseek_model == current.MODEL_ALIAS
                 and type(max_output_tokens) is int and max_output_tokens == self.max_output_tokens
                 and self.max_output_tokens in {1024, current.MAX_EXPLICIT_OUTPUT_TOKENS}
                 and type(self.max_tool_calls) is int and type(self.max_messages) is int
                 and (self.max_tool_calls, self.max_messages) in {(MAX_TOOL_CALLS, MAX_MESSAGES), (12, 32)}
                 and (self.max_tool_calls == MAX_TOOL_CALLS
                      or self.max_output_tokens == current.MAX_EXPLICIT_OUTPUT_TOKENS)
                 and isinstance(messages, list) and 2 <= len(messages) <= self.max_messages
                 and all(isinstance(m, dict) for m in messages)
                 and messages[0].get("role") == "system" and messages[1].get("role") == "user"
                 and isinstance(tools, list) and 1 <= len(tools) <= MAX_TOOLS)
        if self.profile_id == REVIEW_WIRE_PROFILE_ID:
            cfg = provider._settings
            _require(getattr(cfg, "agent_large_output_enabled", False) is True
                     and getattr(cfg, "agent_semantic_review", False) is True
                     and type(getattr(cfg, "agent_max_model_calls", None)) is int
                     and cfg.agent_max_model_calls == 8
                     and type(getattr(cfg, "agent_max_tool_calls", None)) is int
                     and cfg.agent_max_tool_calls == 12
                     and type(getattr(cfg, "agent_max_total_tokens", None)) is int
                     and cfg.agent_max_total_tokens == (BOUNDED_INPUT_TOKENS + 8192) * 8
                     and type(getattr(cfg, "agent_max_prompt_bytes", None)) is int
                     and cfg.agent_max_prompt_bytes == self.max_wire_bytes
                     and cfg.agent_max_output_tokens == 8192)
        try:
            _require(sum(len(m.get("tool_calls", [])) for m in messages) <= self.max_tool_calls)
            public = json.loads(messages[1]["content"])
            _require(isinstance(public, dict) and set(public) == {"question", "module", "release_id"}
                     and all(isinstance(v, str) and v.strip() for v in public.values()))
            payload = provider._request(messages, tools, max_output_tokens)
            wire = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")).encode()
        except (ProviderError, KeyError, TypeError, ValueError, UnicodeError, RecursionError):
            raise BudgetError("current_context_request_invalid") from None
        return parent, public, payload, wire

    def request_geometry(self, provider, messages, tools, *, max_output_tokens):
        """Measure both future-profile gates without a reservation or proof.

        The same pinned official renderer used by ``prove_current_payload``
        measures all four complete variants. Oversized valid requests return
        false gates, while invalid input/source encoding still fails closed.
        No message, tool, source condition or current setting is changed.
        """
        _require(self.profile_id == REVIEW_WIRE_PROFILE_ID)
        _, _, payload, wire = self._request_data(
            provider, messages, tools, max_output_tokens=max_output_tokens)
        renderings = current.render_current_payload(
            payload, self.current_source_root, max_output_tokens=max_output_tokens)
        _require(isinstance(renderings, tuple) and len(renderings) == 4
                 and all(type(value) is bytes for value in renderings))
        sizes = [len(value) for value in renderings]
        wire_fits = 1 <= len(wire) <= self.max_wire_bytes
        render_fits = all(1 <= size <= current.MAX_RENDERED_BYTES for size in sizes)
        return {
            "protocol": "current-official-request-geometry-v1", "profile_id": self.profile_id,
            "wire_bytes": len(wire), "wire_sha256": hashlib.sha256(wire).hexdigest(),
            "max_wire_bytes": self.max_wire_bytes,
            "current_official_rendered_bytes": sizes,
            "current_rendered_sha256": [hashlib.sha256(value).hexdigest() for value in renderings],
            "max_rendered_bytes": current.MAX_RENDERED_BYTES,
            "wire_fits": wire_fits, "render_fits": render_fits,
            "fits": wire_fits and render_fits,
            "input_token_upper_bound": BOUNDED_INPUT_TOKENS, "proof_issued": False,
        }

    def bind_request(self, provider, messages, tools, *, max_output_tokens):
        parent, public, payload, wire = self._request_data(
            provider, messages, tools, max_output_tokens=max_output_tokens)
        if len(wire) > self.max_wire_bytes:
            raise BudgetError("current_context_wire_limit")
        proof = current.prove_current_payload(payload, self.current_source_root,
                                               max_output_tokens=self.max_output_tokens)
        if self.max_tool_calls != MAX_TOOL_CALLS:
            proof.update(tool_call_upper_bound=self.max_tool_calls, message_upper_bound=self.max_messages)
        if self.profile_id == REVIEW_WIRE_PROFILE_ID:
            proof.update(wire_profile_id=self.profile_id, wire_byte_upper_bound=self.max_wire_bytes)
        bound = CurrentContextBoundedRequest(
            continuation_id=parent["continuation_id"],
            wire_sha256=hashlib.sha256(wire).hexdigest(), wire_bytes=len(wire),
            public_input_sha256=_sha(public), parent_receipt=copy.deepcopy(parent),
            current_model_proof=copy.deepcopy(proof), current_source_root=self.current_source_root,
            output_tokens=max_output_tokens,
            tool_call_limit=self.max_tool_calls, message_limit=self.max_messages,
            profile_id=self.profile_id, max_wire_bytes=self.max_wire_bytes,
        )
        object.__setattr__(bound, "_proof", (_PROOF, bound._fingerprint()))
        return bound.validate_current()
