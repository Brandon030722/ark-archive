"""Bounded literal neighborhoods for review, without any new citation authority.

This module does no source IO, search, semantic interpretation or model call.
It only locates an existing observation inside its already registered quote.
The ordinary source replay and independent semantic review remain required.
"""

from __future__ import annotations

import re

from arknights_agent import services
from arknights_agent.errors import InvalidData
from arknights_agent.runtime.evidence_analysis import resolve_registered_source

MAX_CONTEXT_BYTES = 8192
MAX_NEIGHBOR_CHARS = 80
MAX_OBSERVATIONS = 12
# A current tool result is already bounded to 32 KiB. This separate primitive
# guard also prevents a malformed legacy object from creating unbounded work.
MAX_REGISTERED_CHARS = 32768
MAX_OFFSET = 2**53 - 1


def _offset(value):
    return type(value) is int and 0 <= value <= MAX_OFFSET


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _row(answer, observation, index):
    if not isinstance(observation, dict):
        return None
    ref, quote = observation.get("evidence_ref"), observation.get("quote")
    complete = observation.get("complete_source_field")
    if (not isinstance(ref, str) or not 1 <= len(ref) <= 80
            or not isinstance(quote, str) or not 1 <= len(quote) <= 2000
            or "complete_source_field" in observation and type(complete) is not bool):
        return None
    try:
        ref.encode("utf-8")
    except UnicodeError:
        return None
    if "registered_sources" in answer or "registered_sources_format" in answer:
        table = answer.get("registered_sources")
        entry = table.get(ref) if isinstance(table, dict) else None
        raw_candidate = entry.get("quote") if isinstance(entry, dict) else None
    else:
        raw_candidate = observation.get("registered_source_quote")
    if not isinstance(raw_candidate, str) or len(raw_candidate) > MAX_REGISTERED_CHARS:
        return None
    try:
        source = resolve_registered_source(answer, observation)
    except (InvalidData, KeyError, TypeError, ValueError):
        return None
    raw, citation = source["quote"], source["citation"]
    start, end = citation.get("start"), citation.get("end")
    if (len(raw) > MAX_REGISTERED_CHARS or not _offset(start) or not _offset(end)
            or end - start != len(raw) or end <= start
            or not _sha(citation.get("snapshot_hash"))
            or not _sha(citation.get("field_hash"))):
        return None
    count, first, cursor = 0, None, 0
    while True:
        position = raw.find(quote, cursor)
        if position < 0:
            break
        count += 1
        if first is None:
            first = position
        # Count overlapping literal occurrences too; no arbitrary first owner.
        cursor = position + 1
    if not count:
        return None
    row = {"observation_index": index, "evidence_ref": ref,
           "snapshot_hash": citation["snapshot_hash"],
           "citation_field_hash": citation["field_hash"],
           "registered_quote_sha256": services.content_digest(raw),
           "registered_source_start": start, "registered_source_end": end,
           "registered_quote_chars": len(raw), "complete_source_field": complete,
           "occurrence_count": count, "ambiguous": count != 1}
    if count != 1:
        return row
    selected_end = first + len(quote)
    before_start = max(0, first - MAX_NEIGHBOR_CHARS)
    after_end = min(len(raw), selected_end + MAX_NEIGHBOR_CHARS)
    row["match"] = {
        "registered_start": first, "registered_end": selected_end,
        "source_start": start + first, "source_end": start + selected_end,
        "before_registered_start": before_start, "after_registered_end": after_end,
        "before": raw[before_start:first], "after": raw[selected_end:after_end],
    }
    return row


def observation_quote_context(answer, *, max_bytes=MAX_CONTEXT_BYTES):
    """Return at most 12 strict literal location aids within an 8 KiB envelope.

    Shared and legacy registered-source shapes use the same resolver. Invalid
    rows fail closed. Neighbor text stays inside that registered source window
    and grants no permission to cite it as an observation. A repeated quote
    reports ambiguity without choosing a location or semantic owner. Whole
    reading-aid rows may be omitted for bytes; the input object is never edited.
    """
    if type(max_bytes) is not int or not 512 <= max_bytes <= MAX_CONTEXT_BYTES:
        raise ValueError("quote-context byte limit must be an integer from 512 to 8192")
    result = {
        "protocol": "analysis-observation-neighborhood-v1", "reading_only": True,
        "additional_citation_authority": False, "raw_markup_preserved": True,
        "offset_units": "Unicode_codepoints", "source_scope": "registered_quote_only",
        "max_neighbor_chars_each_side": MAX_NEIGHBOR_CHARS, "max_bytes": max_bytes,
        "items": [], "invalid_indices": [], "omitted_indices": [], "omitted_count": 0,
    }
    observations = answer.get("source_observations") if isinstance(answer, dict) else None
    if not isinstance(observations, list) or len(observations) > MAX_OBSERVATIONS:
        result["invalid_input"] = True
        result["total_count"] = 0
        result["full_context_sha256"] = services.content_digest({"invalid_input": True})
        return result
    result["observation_count"] = len(observations)
    result["total_count"] = len(observations)
    for index, observation in enumerate(observations):
        row = _row(answer, observation, index)
        if row is None:
            result["invalid_indices"].append(index)
        else:
            result["items"].append(row)
    result["full_context_sha256"] = services.content_digest({
        "items": result["items"], "invalid_indices": result["invalid_indices"],
        "total_count": result["total_count"]})
    while len(services.canonical_json(result).encode("utf-8")) > max_bytes:
        # Keep source text intact: remove a whole aid row, never part of it.
        omitted = result["items"].pop()
        result["omitted_indices"].insert(0, omitted["observation_index"])
        result["omitted_count"] += 1
    return result
