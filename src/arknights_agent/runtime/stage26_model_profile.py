"""Require both the genuine historical bound and the current official encoding.

This adapter changes no historic receipt or tokenizer proof. Its request keeps
the original validated object, then binds the new model's complete encoding to
the same wire and the same conservative input reservation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field

from arknights_agent.runtime import stage26_current_model as current
from arknights_agent.runtime.bounded_profile import BoundedRequest, BoundedToolProfile
from arknights_agent.runtime.budget import BudgetError

_CURRENT_PROOF = object()
_BASE_FIELDS = (
    "continuation_id",
    "wire_sha256",
    "wire_bytes",
    "rendered_bytes",
    "input_tokens",
    "profile_id",
    "public_input_sha256",
    "_proof",
)


def _require(value):
    if not value:
        raise BudgetError("stage26_current_bound_invalid")


@dataclass(frozen=True)
class CurrentModelBoundedRequest(BoundedRequest):
    legacy_request: BoundedRequest | None = field(default=None, repr=False, compare=False)
    current_model_proof: dict | None = field(default=None, repr=False, compare=False)
    current_source_root: str = field(default="", repr=False, compare=False)
    _current_proof: object = field(default=None, repr=False, compare=False)

    def _current_fingerprint(self):
        body = [self._fingerprint(), self.current_model_proof, self.current_source_root]
        try:
            wire = json.dumps(
                body, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode()
        except (TypeError, ValueError, RecursionError):
            raise BudgetError("stage26_current_bound_invalid") from None
        return hashlib.sha256(wire).hexdigest()

    def validate_current(self):
        BoundedRequest.validate(self)
        legacy = self.legacy_request
        _require(type(legacy) is BoundedRequest)
        legacy.validate()
        _require(all(getattr(self, name) == getattr(legacy, name) for name in _BASE_FIELDS))
        _require(self._current_proof == (_CURRENT_PROOF, self._current_fingerprint()))
        proof = self.current_model_proof
        _require(isinstance(proof, dict))
        expected = {
            "wire_sha256": self.wire_sha256,
            "wire_bytes": self.wire_bytes,
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
        _require(
            set(proof) == {*expected, "current_official_rendered_bytes", "current_rendered_sha256"}
        )
        _require(all(proof[key] == value for key, value in expected.items()))
        sizes, hashes = proof["current_official_rendered_bytes"], proof["current_rendered_sha256"]
        _require(
            isinstance(sizes, list)
            and len(sizes) == 4
            and all(type(size) is int and 1 <= size <= current.MAX_RENDERED_BYTES for size in sizes)
        )
        _require(
            isinstance(hashes, list)
            and len(hashes) == 4
            and all(
                isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes
            )
        )
        current.current_sources(self.current_source_root)
        return self

    def validate(self):
        return self.validate_current()

    def audit(self):
        return {
            **BoundedRequest.audit(self),
            "legacy_profile_preserved": True,
            "current_model_profile": copy.deepcopy(self.current_model_proof),
        }


class CurrentModelToolProfile:
    """Generate the old authentic request first, then add the current proof."""

    def __init__(self, *, source_root, receipt, current_source_root):
        self._legacy = BoundedToolProfile(source_root=source_root, receipt=receipt)
        self.current_source_root = str(current.current_sources(current_source_root).resolve())

    @property
    def continuation_id(self):
        return self._legacy.continuation_id

    def bind_request(self, provider, messages, tools, *, max_output_tokens):
        legacy = self._legacy.bind_request(
            provider, messages, tools, max_output_tokens=max_output_tokens
        )
        payload = provider._request(messages, tools, max_output_tokens)
        proof = current.prove_current_payload(payload, self.current_source_root)
        bound = CurrentModelBoundedRequest(
            **{name: getattr(legacy, name) for name in _BASE_FIELDS},
            legacy_request=legacy,
            current_model_proof=copy.deepcopy(proof),
            current_source_root=self.current_source_root,
        )
        object.__setattr__(bound, "_current_proof", (_CURRENT_PROOF, bound._current_fingerprint()))
        return bound.validate_current()
