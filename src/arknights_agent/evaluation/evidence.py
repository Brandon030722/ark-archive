"""Independent evidence replay for evaluation, never the production answer builder.

Raw-object integrity and answer correctness are separate checks. A valid source
pointer does not establish that it answers the question at the requested level.
The scorer must additionally compare each fact with its gold source anchors.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from arknights_agent import services
from arknights_agent.errors import Conflict, InvalidData, NotFound


def json_equal(left, right):
    """JSON comparison that cannot confuse booleans with numeric facts."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            json_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return type(left) is type(right) and left == right


@dataclass
class EvidenceAudit:
    errors: list[dict] = field(default_factory=list)
    verified: dict[str, dict] = field(default_factory=dict)
    citation_count: int = 0

    @property
    def passed(self):
        return self.citation_count > 0 and not self.errors

    def summary(self):
        return {
            "passed": self.passed,
            "citation_count": self.citation_count,
            "verified_count": len(self.verified),
            "errors": self.errors,
        }


class EvidenceContext:
    """Replay references inside one release and one module's source inventory.

    For imported baseline references the database evidence/record IDs are optional:
    an exact snapshot hash, original-source pointer, URL and revision are sufficient
    to replay a raw field. If database IDs are supplied, their ownership and links
    are checked as well. This does not grant correctness to an unsupported claim.
    """

    def __init__(self, session, release, store):
        self.session, self.release, self.store = session, release, store
        self.documents = {}
        self.snapshots = {}

    def _sources(self, module):
        if services.content_digest(self.release.manifest) != self.release.manifest_hash:
            raise Conflict("evaluation release manifest failed integrity verification")
        entry = self.release.manifest.get("datasets", {}).get(module)
        if entry is None:
            raise NotFound("evaluation module is absent from this release")
        result = {}
        for ident in entry.get("source_snapshot_ids", []):
            if ident not in self.snapshots:
                self.snapshots[ident] = services.get_snapshot(self.session, self.release, ident)
            result[ident] = self.snapshots[ident]
        return entry, result

    def _document(self, snapshot):
        digest = snapshot["content_hash"]
        if digest not in self.documents:
            raw = self.store.read_verified(digest)
            if len(raw) != snapshot["byte_size"]:
                raise Conflict("evaluation snapshot byte count differs from published source")
            self.documents[digest] = services.snapshot_document(raw, snapshot["media_type"])
        return self.documents[digest]

    def audit(self, citations, module):
        audit = EvidenceAudit()
        if not isinstance(citations, list):
            audit.errors.append({"code": "citations_not_a_list"})
            return audit
        audit.citation_count = len(citations)
        if not citations:
            audit.errors.append({"code": "missing_citations"})
            return audit
        try:
            entry, sources = self._sources(module)
        except (Conflict, InvalidData, NotFound) as exc:
            audit.errors.append({"code": "release_source_inventory_invalid", "detail": str(exc)})
            return audit
        seen = set()
        for index, citation in enumerate(citations):
            ident = (
                citation.get("id", f"citation:{index}")
                if isinstance(citation, dict)
                else str(index)
            )
            if not isinstance(ident, str) or not ident:
                audit.errors.append({"citation_index": index, "code": "invalid_citation_id"})
                continue
            if ident in seen:
                audit.errors.append({"citation_id": ident, "code": "duplicate_citation_id"})
                continue
            seen.add(ident)
            try:
                verified = self._verify(citation, entry, sources)
            except (Conflict, InvalidData, NotFound, KeyError, TypeError, ValueError) as exc:
                audit.errors.append(
                    {"citation_id": ident, "code": "citation_invalid", "detail": str(exc)}
                )
            else:
                audit.verified[ident] = verified
        return audit

    def _verify(self, citation, entry, sources):
        if not isinstance(citation, dict):
            raise InvalidData("citation must be an object")
        digest = citation.get("snapshot_hash", citation.get("content_hash"))
        if (
            "snapshot_hash" in citation
            and "content_hash" in citation
            and citation["snapshot_hash"] != citation["content_hash"]
        ):
            raise InvalidData("citation contains contradictory snapshot hashes")
        ident = citation.get("snapshot_id")
        if ident is not None and not isinstance(ident, str):
            raise InvalidData("citation snapshot ID must be a string")
        if ident is None:
            matches = [
                snapshot
                for snapshot in sources.values()
                if snapshot["content_hash"] == digest
                and snapshot["url"] == citation.get("source_url")
                and snapshot["revision"] == citation.get("source_revision")
            ]
            if len(matches) != 1:
                raise InvalidData("citation cannot identify exactly one pinned source")
            snapshot = matches[0]
            ident = snapshot["id"]
        elif ident not in sources:
            raise InvalidData("citation refers outside the pinned module/release")
        else:
            snapshot = sources[ident]
        if digest != snapshot["content_hash"]:
            raise InvalidData("citation snapshot hash differs from published raw object")
        if citation.get("source_url") != snapshot["url"]:
            raise InvalidData("citation source URL differs from published raw object")
        if citation.get("source_revision") != snapshot["revision"]:
            raise InvalidData("citation source revision differs from published raw object")
        if citation.get("source_key", snapshot["source_key"]) != snapshot["source_key"]:
            raise InvalidData("citation source key differs from published raw object")
        if citation.get("dataset_id", entry["dataset_id"]) != entry["dataset_id"]:
            raise InvalidData("citation dataset differs from pinned module")
        document = self._document(snapshot)
        source_pointer = citation["pointer"]
        if not isinstance(source_pointer, str):
            raise InvalidData("citation pointer must be a string")
        actual = services.json_pointer(document, source_pointer)
        if citation.get("selector_semantics") == "RFC6901_then_Unicode_codepoint_slice":
            start, end = citation.get("start"), citation.get("end")
            if (not isinstance(actual, str) or type(start) is not int or type(end) is not int
                    or not 0 <= start < end <= len(actual)):
                raise InvalidData("citation Unicode range is invalid")
            actual = actual[start:end]
        elif "start" in citation or "end" in citation:
            raise InvalidData("citation range requires explicit Unicode selector semantics")
        if "field_value" in citation and not json_equal(actual, citation["field_value"]):
            raise InvalidData("citation field differs from original snapshot")
        if "field_hash" in citation and citation["field_hash"] != services.content_digest(actual):
            raise InvalidData("citation field hash differs from original snapshot")
        evidence_id = citation.get("evidence_id")
        if evidence_id is not None:
            evidence = services.get_evidence(self.session, self.release, evidence_id)
            if evidence["dataset_id"] != entry["dataset_id"] or evidence["snapshot_id"] != ident:
                raise InvalidData("citation evidence belongs to another dataset or source")
            base = evidence["pointer"]
            if source_pointer != base and not source_pointer.startswith(base + "/"):
                raise InvalidData("citation field is outside its referenced evidence excerpt")
            if citation.get("evidence_pointer", base) != base:
                raise InvalidData("citation evidence pointer differs from stored excerpt")
            raw_excerpt = services.json_pointer(document, base)
            if not json_equal(raw_excerpt, evidence["excerpt"]):
                raise InvalidData("stored evidence excerpt differs from original snapshot")
            if services.content_digest(raw_excerpt) != evidence["excerpt_hash"]:
                raise InvalidData("stored evidence excerpt hash differs from original snapshot")
            if citation.get("excerpt_hash") != evidence["excerpt_hash"]:
                raise InvalidData("citation excerpt hash differs from stored evidence")
        elif "excerpt_hash" in citation and citation["excerpt_hash"] != services.content_digest(
            actual
        ):
            raise InvalidData("citation excerpt hash cannot be replayed at its source pointer")
        if citation.get("record_id") is not None:
            record = services.get_record(self.session, self.release, citation["record_id"])
            if record["dataset_id"] != entry["dataset_id"]:
                raise InvalidData("citation record belongs to another module/dataset")
            if evidence_id is None or evidence_id not in record["evidence_ids"]:
                raise InvalidData("citation record is not linked to its evidence")
        return {
            "source_key": snapshot["source_key"],
            "snapshot_hash": digest,
            "snapshot_id": ident,
            "pointer": source_pointer,
            "source_url": snapshot["url"],
            "source_revision": snapshot["revision"],
            "field_value": actual,
            "field_hash": services.content_digest(actual),
        }
