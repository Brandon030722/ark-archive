"""Independent recruitment rule packets built from the public source catalog.

The existing selector and historical packets are unchanged. This version only
appends complete rule text from the catalog's ``prts:recruitment:`` namespace
to recruitment source arms. Selection never reads expectations or predictions.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from arknights_agent.errors import InvalidData
from arknights_agent.evaluation.baselines import ARMS, _json, build_public_input
from arknights_agent.evaluation.packets import SourceReader

PACKET_VERSION = "ark-public-recruitment-rule-packets-v1"
RULE_NAMESPACE = "prts:recruitment:"
CONTENT_POINTER = "/query/pages/0/revisions/0/slots/main/content"
MAX_PACKET_BYTES = 300_000
IDENTITY_FIELDS = (
    "source_key", "source_revision", "snapshot_hash", "source_url", "pointer",
)


def _identity(fragment):
    return tuple(fragment[key] for key in IDENTITY_FIELDS)


def _source_fragment(source, reader):
    """Return complete native rule text, with independently checked raw bytes."""
    path = Path(source["local_cache_path"])
    if any(part.startswith(".env") for part in path.parts):
        raise InvalidData("recruitment rule cache cannot be an environment configuration")
    try:
        original = reader.read(source)
    except OSError as exc:
        raise InvalidData("recruitment rule source cache is unavailable") from exc
    try:
        content = original["query"]["pages"][0]["revisions"][0]["slots"]["main"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise InvalidData("recruitment rule source lacks its native complete text") from exc
    if not isinstance(content, str):
        raise InvalidData("recruitment rule source native text must be a string")
    return {
        **{key: source[key] for key in IDENTITY_FIELDS if key != "pointer"},
        "pointer": CONTENT_POINTER,
        "content": content,
    }


def _repair(case, arm, reader, max_packet_bytes):
    if type(max_packet_bytes) is not int or max_packet_bytes <= 0:
        raise InvalidData("repaired source packet byte limit must be a positive integer")
    public = build_public_input(case, arm)
    if arm != "source_packet" or public["module"] != "recruitment":
        return public
    # Validation above must not reorder existing fragment fields or nested
    # original values. Keep the prepared packet itself as the appended prefix.
    packet = deepcopy(case["source_packet"])
    existing = {_identity(fragment): fragment for fragment in packet}
    # Dictionary insertion order is the frozen catalog order. Page titles,
    # hidden expectations and prior response contents do not affect selection.
    for source in reader.sources.values():
        if not source["source_key"].startswith(RULE_NAMESPACE):
            continue
        fragment = _source_fragment(source, reader)
        identity = _identity(fragment)
        if identity in existing:
            if _json(existing[identity]["content"]) != _json(fragment["content"]):
                raise InvalidData("existing recruitment rule text differs from its original source")
            continue
        packet.append(fragment)
        existing[identity] = fragment
    if len(packet) > 512 or len(_json(packet).encode("utf-8")) > max_packet_bytes:
        raise InvalidData("repaired source packet exceeds its byte limit; no silent truncation")
    public["source_packet"] = packet
    # Reuse the old public whitelist, fragment contract and character bound.
    build_public_input({**case, "source_packet": packet}, arm)
    return public


def repair_public_input(case, arm, sources, workspace, *, max_packet_bytes=MAX_PACKET_BYTES):
    """Extend a prepared case and return the original public input structure."""
    return _repair(case, arm, SourceReader(sources, Path(workspace)), max_packet_bytes)


def prepare_public_inputs(public_by_arm, sources, workspace, *, max_packet_bytes=MAX_PACKET_BYTES):
    """Extend the result of ``runner.prepare_baseline_inputs`` without mutation.

    Return the same arm-to-case-list structure so existing collectors can apply
    their public whitelist. Hidden expectations are excluded before copying.
    Closed arms and other modules retain their original public representation.
    """
    if set(public_by_arm) != ARMS:
        raise InvalidData("repaired packets require both original public arms")
    reader = SourceReader(sources, Path(workspace))
    result = {}
    for arm, cases in public_by_arm.items():
        if not isinstance(cases, list):
            raise InvalidData("repaired public arms must contain case lists")
        result[arm] = []
        for case in cases:
            copied = deepcopy({key: value for key, value in case.items() if key != "expected"})
            public = _repair(copied, arm, reader, max_packet_bytes)
            if arm == "source_packet" and public["module"] == "recruitment":
                copied["source_packet"] = public["source_packet"]
            result[arm].append(copied)
    return result
