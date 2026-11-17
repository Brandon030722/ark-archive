"""Future-only 512K token contract; historical receipts and holds are immutable.

Count all four official chat renderings with the pinned official BPE tokenizer.
A separate structural reserve covers API framing uncertainty. Neither UTF-8
bytes nor a character estimate is represented as a token count.
"""
from __future__ import annotations

import copy
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from tokenizers import Tokenizer

from arknights_agent.runtime import stage26_current_model as current
from arknights_agent.runtime.bounded_profile import STAGE16_PROFILE_ID, validate_receipt
from arknights_agent.runtime.budget import BudgetError
from arknights_agent.runtime.context_profile import CurrentContextBoundedRequest, _require, _sha
from arknights_agent.runtime.providers import DeepSeekProvider

PROFILE_ID = "current-official-tokenized-512k-model-first-v1"
CONTEXT_TOKENS = 512 * 1024
OUTPUT_TOKENS = 8192
INPUT_TOKENS = CONTEXT_TOKENS - OUTPUT_TOKENS
STRUCTURE_TOKENS = current.API_STRUCTURE_TOKEN_ALLOWANCE
MAX_WIRE_BYTES = 8 * 1024 * 1024
_PROOF = object()


@lru_cache(maxsize=4)
def _tokenizer(root, expected_sha):
    source = (Path(root) / "tokenizer.json").read_bytes()
    _require(hashlib.sha256(source).hexdigest() == expected_sha)
    return Tokenizer.from_str(source.decode())


def measure(payload, source_root):
    root = current.current_sources(source_root)
    rendered = current.render_current_payload(payload, root, max_output_tokens=OUTPUT_TOKENS)
    tokenizer = _tokenizer(str(root.resolve()), current.SOURCE_HASHES["tokenizer.json"])
    counts = [len(tokenizer.encode(value.decode(), add_special_tokens=False).ids) for value in rendered]
    return {
        "current_official_rendered_bytes": [len(v) for v in rendered],
        "current_rendered_sha256": [hashlib.sha256(v).hexdigest() for v in rendered],
        "current_official_rendered_tokens": counts,
        "context_token_limit": CONTEXT_TOKENS,
        "api_structure_token_allowance": STRUCTURE_TOKENS,
        "input_token_upper_bound": INPUT_TOKENS,
        "output_token_upper_bound": OUTPUT_TOKENS,
        "tokenizer_sha256": current.SOURCE_HASHES["tokenizer.json"],
        "current_official_source_hashes": copy.deepcopy(current.SOURCE_HASHES),
        "bound_basis": "official_BPE_tokens_plus_structure_reserve_plus_output_within_512K",
    }


class LongContextBoundedRequest(CurrentContextBoundedRequest):
    def validate_current(self):
        parent = validate_receipt(self.parent_receipt)
        proof = self.current_model_proof
        _require(type(self) is LongContextBoundedRequest
                 and parent["profile_id"] == STAGE16_PROFILE_ID
                 and self.continuation_id == parent["continuation_id"]
                 and self.profile_id == PROFILE_ID
                 and self.max_wire_bytes == MAX_WIRE_BYTES
                 and type(self.wire_bytes) is int and 0 < self.wire_bytes <= MAX_WIRE_BYTES
                 and self.input_tokens == INPUT_TOKENS and self.output_tokens == OUTPUT_TOKENS
                 and type(self.tool_call_limit) is int and 12 <= self.tool_call_limit <= 192
                 and self.message_limit == 512
                 and self._proof == (_PROOF, self._fingerprint())
                 and proof.get("wire_sha256") == self.wire_sha256
                 and proof.get("wire_bytes") == self.wire_bytes
                 and proof.get("context_token_limit") == CONTEXT_TOKENS
                 and proof.get("input_token_upper_bound") == INPUT_TOKENS
                 and proof.get("output_token_upper_bound") == OUTPUT_TOKENS
                 and proof.get("api_structure_token_allowance") == STRUCTURE_TOKENS
                 and proof.get("current_official_source_hashes") == current.SOURCE_HASHES)
        counts = proof.get("current_official_rendered_tokens")
        _require(isinstance(counts, list) and len(counts) == 4
                 and all(type(n) is int and 0 < n <= INPUT_TOKENS - STRUCTURE_TOKENS for n in counts))
        current.current_sources(self.current_source_root)
        return self


class LongContextToolProfile:
    profile_id = PROFILE_ID
    max_wire_bytes = MAX_WIRE_BYTES
    max_output_tokens = OUTPUT_TOKENS
    max_messages = 512

    def __init__(self, *, receipt, current_source_root, max_tool_calls=96):
        self.receipt = copy.deepcopy(validate_receipt(receipt))
        _require(self.receipt["profile_id"] == STAGE16_PROFILE_ID
                 and type(max_tool_calls) is int and 12 <= max_tool_calls <= 192)
        self.current_source_root = str(current.current_sources(current_source_root).resolve())
        self.max_tool_calls = max_tool_calls

    @property
    def continuation_id(self):
        return self.receipt["continuation_id"]

    def _request_data(self, provider, messages, tools, *, max_output_tokens):
        cfg = provider._settings
        _require(isinstance(provider, DeepSeekProvider) and provider.model == current.MODEL_ALIAS
                 and cfg.agent_model_first and cfg.agent_semantic_review and cfg.agent_large_output_enabled
                 and cfg.agent_context_tokens == CONTEXT_TOKENS
                 and cfg.agent_max_output_tokens == max_output_tokens == OUTPUT_TOKENS
                 and cfg.agent_max_prompt_bytes == MAX_WIRE_BYTES
                 and cfg.agent_max_tool_calls == self.max_tool_calls
                 and cfg.agent_max_total_tokens == CONTEXT_TOKENS * cfg.agent_max_model_calls
                 and isinstance(messages, list) and 2 <= len(messages) <= self.max_messages
                 and messages[0].get("role") == "system" and messages[1].get("role") == "user"
                 and sum(len(m.get("tool_calls", [])) for m in messages) <= self.max_tool_calls)
        try:
            public = json.loads(messages[1]["content"])
            _require(set(public) == {"question", "module", "release_id"}
                     and all(isinstance(v, str) and v.strip() for v in public.values()))
            payload = provider._request(messages, tools, max_output_tokens)
            wire = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        except (ValueError, TypeError, KeyError):
            raise BudgetError("current_context_request_invalid") from None
        return public, payload, wire

    def request_geometry(self, provider, messages, tools, *, max_output_tokens):
        _, payload, wire = self._request_data(provider, messages, tools, max_output_tokens=max_output_tokens)
        geometry = measure(payload, self.current_source_root)
        geometry.update(profile_id=PROFILE_ID, wire_bytes=len(wire),
                        wire_sha256=hashlib.sha256(wire).hexdigest(), max_wire_bytes=MAX_WIRE_BYTES,
                        wire_fits=0 < len(wire) <= MAX_WIRE_BYTES,
                        render_fits=max(geometry["current_official_rendered_tokens"]) + STRUCTURE_TOKENS <= INPUT_TOKENS,
                        proof_issued=False)
        geometry["fits"] = geometry["wire_fits"] and geometry["render_fits"]
        return geometry

    def bind_request(self, provider, messages, tools, *, max_output_tokens):
        public, _, _ = self._request_data(provider, messages, tools, max_output_tokens=max_output_tokens)
        proof = self.request_geometry(provider, messages, tools, max_output_tokens=max_output_tokens)
        if not proof["fits"]:
            raise BudgetError("context_512k_token_limit")
        bound = LongContextBoundedRequest(
            continuation_id=self.continuation_id, wire_sha256=proof["wire_sha256"],
            wire_bytes=proof["wire_bytes"], public_input_sha256=_sha(public),
            parent_receipt=copy.deepcopy(self.receipt), current_model_proof=proof,
            current_source_root=self.current_source_root, input_tokens=INPUT_TOKENS,
            output_tokens=OUTPUT_TOKENS, tool_call_limit=self.max_tool_calls,
            message_limit=self.max_messages, profile_id=PROFILE_ID, max_wire_bytes=MAX_WIRE_BYTES)
        object.__setattr__(bound, "_proof", (_PROOF, bound._fingerprint()))
        return bound.validate_current()
