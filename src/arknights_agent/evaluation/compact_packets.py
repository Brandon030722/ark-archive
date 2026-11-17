"""Lossless source identity dictionaries for an independently versioned prompt.

Only repeated metadata is factored out. Every fragment's original content,
pointer, projection and list order survives a round trip, including whole Wiki
texts. The codec does not inspect questions, answer keys or candidate values.
"""

from __future__ import annotations

import math
import re
from copy import deepcopy

from arknights_agent.errors import InvalidData

SCHEMA_VERSION = 1
MAX_FRAGMENTS = 512
IDENTITY_FIELDS = ("source_key", "source_revision", "snapshot_hash", "source_url")
FRAGMENT_FIELDS = (*IDENTITY_FIELDS, "pointer", "content")
PUBLIC_FIELDS = {
    "case_id", "question", "module", "release_id", "source_revision", "request", "fact_keys",
}


def _json_value(value):
    """Reject non-JSON Python objects without changing any original value."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise InvalidData("compact source content must contain only finite JSON values")


def _pointer(value):
    if type(value) is not str or (
        value and (not value.startswith("/") or re.search(r"~(?![01])", value))
    ):
        raise InvalidData("compact source pointer must be an original RFC 6901 pointer")


def _identity(value):
    if type(value) is not dict or set(value) != set(IDENTITY_FIELDS):
        raise InvalidData("compact source identity has unexpected or absent fields")
    if any(type(value[key]) is not str or not value[key] for key in IDENTITY_FIELDS):
        raise InvalidData("compact source identity fields must be nonempty strings")
    if not re.fullmatch(r"[0-9a-f]{64}", value["snapshot_hash"]):
        raise InvalidData("compact source snapshot hash must be a SHA-256 digest")
    return tuple(value[key] for key in IDENTITY_FIELDS)


def _table(value):
    if type(value) is not list or len(value) > MAX_FRAGMENTS:
        raise InvalidData("compact source table must be a bounded list")
    identities = [_identity(row) for row in value]
    if len(identities) != len(set(identities)):
        raise InvalidData("compact source table has duplicate identities")
    return identities


def encode_source_packet(packet):
    """Factor metadata into a table; retain each content value verbatim.

    A fragment row is ``[source_index, pointer, content]``, optionally followed
    by its original projection string. Index zero identifies the first source
    encountered, independently of the question's release revision.
    """
    if type(packet) is not list or len(packet) > MAX_FRAGMENTS:
        raise InvalidData("compact source packet must be at most 512 original fragments")
    sources, fragments, indices = [], [], {}
    for fragment in packet:
        if type(fragment) is not dict or set(fragment) not in (
            set(FRAGMENT_FIELDS), set(FRAGMENT_FIELDS) | {"projection"},
        ):
            raise InvalidData("compact source fragment has unexpected or absent fields")
        source = {key: fragment[key] for key in IDENTITY_FIELDS}
        identity = _identity(source)
        _pointer(fragment["pointer"])
        _json_value(fragment["content"])
        if identity not in indices:
            indices[identity] = len(sources)
            sources.append(source)
        row = [indices[identity], fragment["pointer"], deepcopy(fragment["content"])]
        if "projection" in fragment:
            if fragment["projection"] is not None and type(fragment["projection"]) is not str:
                raise InvalidData("compact source projection must preserve its original string")
            row.append(fragment["projection"])
        fragments.append(row)
    return {"schema_version": SCHEMA_VERSION, "source_table": sources, "fragments": fragments}


def decode_source_packet(encoded):
    """Reconstruct original fragments and reject noncanonical source indices."""
    if type(encoded) is not dict or set(encoded) != {
        "schema_version", "source_table", "fragments",
    }:
        raise InvalidData("compact source envelope has unexpected or absent fields")
    if type(encoded["schema_version"]) is not int or encoded["schema_version"] != SCHEMA_VERSION:
        raise InvalidData("compact source schema version is unsupported")
    sources = encoded["source_table"]
    _table(sources)
    rows = encoded["fragments"]
    if type(rows) is not list or len(rows) > MAX_FRAGMENTS:
        raise InvalidData("compact source fragments must be a bounded list")
    fragments, encountered = [], []
    for row in rows:
        if type(row) is not list or len(row) not in (3, 4):
            raise InvalidData("compact source fragment row must contain 3 or 4 items")
        index, pointer, content = row[:3]
        if type(index) is not int or not 0 <= index < len(sources):
            raise InvalidData("compact source fragment references an absent source")
        if index not in encountered:
            if index != len(encountered):
                raise InvalidData("compact sources must follow their first appearance order")
            encountered.append(index)
        _pointer(pointer)
        _json_value(content)
        fragment = {key: sources[index][key] for key in IDENTITY_FIELDS}
        fragment.update(pointer=pointer, content=deepcopy(content))
        if len(row) == 4:
            if row[3] is not None and type(row[3]) is not str:
                raise InvalidData("compact source projection must preserve its original string")
            fragment["projection"] = row[3]
        fragments.append(fragment)
    if len(encountered) != len(sources):
        raise InvalidData("compact source table contains an unused identity")
    return fragments


def _public(value, *, compact=False):
    allowed = PUBLIC_FIELDS | {"source_packet"}
    if compact:
        allowed |= {"source_table"}
    if type(value) is not dict or not PUBLIC_FIELDS <= set(value) or not set(value) <= allowed:
        raise InvalidData("compact public input must use only the public task whitelist")
    for key in ("case_id", "question", "module", "release_id", "source_revision"):
        if type(value[key]) is not str or not value[key]:
            raise InvalidData("compact public task binding fields must be nonempty strings")
    if type(value["request"]) is not dict or not set(value["request"]) <= {
        "module", "tags", "duration_minutes",
    }:
        raise InvalidData("compact public request must use only the public request whitelist")
    if type(value["fact_keys"]) is not list or any(
        type(key) is not str for key in value["fact_keys"]
    ):
        raise InvalidData("compact public fact keys must be an original string list")
    _json_value(value)
    if compact and ("source_table" in value) != ("source_packet" in value):
        raise InvalidData("compact source fragments require their source identity table")


def compact_public_input(public_input):
    """Encode a previously whitelisted input; closed-book inputs are unchanged."""
    _public(public_input)
    result = {}
    for key, value in public_input.items():
        if key == "source_packet":
            encoded = encode_source_packet(value)
            result["source_table"] = encoded["source_table"]
            result[key] = encoded["fragments"]
        else:
            result[key] = deepcopy(value)
    return result


def expand_public_input(public_input):
    """Replay the original public input from its compact source representation."""
    _public(public_input, compact=True)
    result = {}
    for key, value in public_input.items():
        if key == "source_table":
            continue
        if key == "source_packet":
            result[key] = decode_source_packet({
                "schema_version": SCHEMA_VERSION,
                "source_table": public_input["source_table"],
                "fragments": value,
            })
        else:
            result[key] = deepcopy(value)
    return result


def citation_identity(source_table, source_index, pointer):
    """Bind a citation's short reference solely to the frozen source identity.

    Content values are deliberately unavailable here. An original pointer may
    address a field below a fragment, which the independent citation resolver
    must still verify against the original snapshot.
    """
    _table(source_table)
    if type(source_index) is not int or not 0 <= source_index < len(source_table):
        raise InvalidData("compact citation references an absent source")
    _pointer(pointer)
    result = {key: source_table[source_index][key] for key in IDENTITY_FIELDS}
    result["pointer"] = pointer
    return result
