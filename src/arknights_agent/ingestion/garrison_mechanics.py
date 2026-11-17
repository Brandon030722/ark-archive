"""Retain phase-specific descriptions without promoting incomplete mechanisms.

All pins apply to original bytes. Intermediate conflicts, community execution
notes and a mode-specific funding repair are preserved as review-only evidence.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from datetime import datetime
from pathlib import Path

import httpx

from arknights_agent.errors import InvalidData
from arknights_agent.garrison_expansion_policy import (
    EXPANSION_FIELD_SPECS,
)
from arknights_agent.garrison_expansion_policy import (
    WIKI_SOURCES as BASE_WIKI_PINS,
)
from arknights_agent.garrison_mechanics_policy import (
    ADDITIONAL_WIKI_REVISIONS,
    CANDIDATE_NOTES,
    CONTENT_POINTER,
    DESCRIPTION_SPECS,
    MAPPING_BASIS,
    MECHANICS_REVIEWS,
    OFFICIAL_CONTEXT_PIN,
    PARSER_VERSION,
    PHASE_BOND_PROOFS,
    REVIEW_STATUS,
    VERIFICATION_STATUS,
    WIKI_PINS,
)
from arknights_agent.garrison_policy import FIELD_SPECS, HISTORICAL_SOURCES
from arknights_agent.services import content_digest, json_pointer, same_json_value

from .contracts import NormalizedRecord, SampleBatch, SnapshotInput
from .garrison import ANNOUNCEMENTS, HTML_DOCUMENT_PARSER, html_document
from .garrison_expansion import build_garrison_expansion_batch
from .garrison_history import _checked_original, _source_identity, build_garrison_history_batch
from .sample import ACTIVITY_ID, SEASON_POINTER, IngestionError

OWN_KINDS = {
    "mechanics_wiki_content",
    "mechanics_official_context",
    "mechanics_client_buffs",
    "documented_mechanics_field",
    "mechanics_review",
    "mechanics_candidate_note",
}
OFFICIAL_SOURCE_ID = "9695"


def _pinned_bytes(raw: bytes, pin: dict) -> None:
    if (
        not isinstance(raw, bytes)
        or len(raw) != pin["byte_size"]
        or hashlib.sha256(raw).hexdigest() != pin["snapshot_hash"]
    ):
        raise IngestionError("mechanics source original bytes differ from fixed pin")


def _wiki_document(raw: bytes, revision: int) -> tuple[dict, str]:
    pin = WIKI_PINS[revision]
    _pinned_bytes(raw, pin)
    try:
        document = json.loads(raw)
        pages = document["query"]["pages"]
        if len(pages) != 1 or len(pages[0]["revisions"]) != 1:
            raise IngestionError("mechanics wiki has ambiguous page/revision identity")
        page = pages[0]
        row = page["revisions"][0]
        slot = row["slots"]["main"]
        text = slot["content"]
        if (
            page["pageid"] != pin["page_id"]
            or page["ns"] != 0
            or page["title"] != pin["title"]
            or row["revid"] != revision
            or row["timestamp"] != pin["timestamp"]
            or slot["contentmodel"] != "wikitext"
            or slot["contentformat"] != "text/x-wiki"
            or not isinstance(text, str)
            or content_digest(text) != pin["content_hash"]
            or json_pointer(document, CONTENT_POINTER) != text
        ):
            raise IngestionError("mechanics wiki revision identity/content differs")
        return document, text
    except IngestionError:
        raise
    except (KeyError, TypeError, IndexError, ValueError, UnicodeError, InvalidData) as exc:
        raise IngestionError("invalid mechanics wiki original") from exc


def _official_document(raw: bytes) -> dict:
    _pinned_bytes(raw, OFFICIAL_CONTEXT_PIN)
    document = html_document(raw)
    try:
        for selector, expected in OFFICIAL_CONTEXT_PIN["checks"]:
            if content_digest(json_pointer(document, selector)) != content_digest(expected):
                raise IngestionError("mechanics official context projection differs")
    except InvalidData as exc:
        raise IngestionError("mechanics official context projection is invalid") from exc
    return document


def fetch_mechanics_sources(
    cache_dir: str | Path = ".local/garrison-mechanics",
) -> dict[int | str, bytes]:
    """Fetch the five fixed originals; no mutable pages or template expansion."""
    root = Path(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    requests = [
        (revision, WIKI_PINS[revision], f"{revision}.json")
        for revision in ADDITIONAL_WIKI_REVISIONS
    ]
    requests.append((OFFICIAL_SOURCE_ID, OFFICIAL_CONTEXT_PIN, "9695.html"))
    sources = {}
    with httpx.Client(timeout=60, follow_redirects=False) as client:
        for source_id, pin, filename in requests:
            target = root / filename
            if target.exists():
                raw = target.read_bytes()
            else:
                try:
                    response = client.get(pin["url"])
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    raise IngestionError(f"mechanics source fetch failed: {source_id}") from exc
                raw = response.content
            if isinstance(source_id, int):
                _wiki_document(raw, source_id)
            else:
                _official_document(raw)
            if not target.exists():
                with tempfile.NamedTemporaryFile(dir=root, delete=False) as temporary:
                    temporary.write(raw)
                    temporary_path = Path(temporary.name)
                try:
                    temporary_path.replace(target)
                finally:
                    temporary_path.unlink(missing_ok=True)
            sources[source_id] = raw
    return sources


def _wiki_witness(proof: dict, label: str, texts: dict[int, str]) -> None:
    """Keep exact text inside the named bond's description, before its roster."""
    try:
        text = texts[proof["revision"]]
        quote = proof["quote"]
        context = proof["context_quote"]
        if (
            proof["revision"] not in WIKI_PINS
            or proof["content_pointer"] != CONTENT_POINTER
            or proof["context_kind"] != "named_bond_description"
            or proof["template_expansion"] != "not_used"
            or proof["end_anchor"] != "|干员="
            or context != f"{{{{/盟约|盟约={label}|人数="
            or content_digest(quote) != proof["quote_hash"]
            or text.count(quote) != 1
            or text.count(context) != 1
        ):
            raise IngestionError("mechanics wiki quote/context differs")
        start = text.index(context)
        quote_start = text.index(quote)
        end = text.find(proof["end_anchor"], start)
        line_end = text.find("\n", start)
        if (
            end < 0
            or quote_start < start
            or quote_start + len(quote) > end
            or (line_end >= 0 and end > line_end)
        ):
            raise IngestionError("mechanics wiki quote is outside its named bond description")
    except IngestionError:
        raise
    except (KeyError, TypeError, IndexError, ValueError, InvalidData) as exc:
        raise IngestionError("invalid mechanics wiki witness") from exc


def _base_documents(base_batch: SampleBatch) -> tuple[dict, dict, dict]:
    """Require the unchanged three official and eleven community field baseline."""
    coverage = base_batch.coverage.get("garrison", {})
    scope = base_batch.scopes.get("garrison", {})
    if (
        set(coverage.get("verified_rule_fields", [])) != set(FIELD_SPECS)
        or set(coverage.get("cross_checked_rule_fields", [])) != set(EXPANSION_FIELD_SPECS)
        or coverage.get("verified_field_qa_enabled") is not True
        or coverage.get("cross_checked_field_qa_enabled") is not True
        or coverage.get("rules_qa_enabled") is not False
        or coverage.get("rules_engine_enabled") is not False
        or coverage.get("gameplay_application_verified") is not False
        or scope.get("activity_id") != ACTIVITY_ID
        or scope.get("rule_phase_mapping") != "unverified"
        or coverage.get("rule_phase_mapping") != "unverified"
    ):
        raise IngestionError("mechanics ingestion requires the unchanged stage6 field baseline")
    sources = {source.source_key: source for source in base_batch.snapshots}
    records = {(record.kind, record.native_id): record for record in base_batch.records}
    if (
        len(sources) != len(base_batch.snapshots)
        or len(records) != len(base_batch.records)
        or any(record.module != "garrison" for record in base_batch.records)
    ):
        raise IngestionError("mechanics baseline contains duplicate or foreign identities")
    documents = {}
    for revision, pin in HISTORICAL_SOURCES.items():
        proof = next(s for s in PHASE_BOND_PROOFS.values() if s["revision"] == revision)
        source = sources.get(proof["source_key"])
        if (
            source is None
            or source.source_type != "historical_client_data"
            or source.revision != revision
            or source.url != proof["source_url"]
            or source.media_type != "application/json"
            or proof["snapshot_hash"] != pin["snapshot_hash"]
        ):
            raise IngestionError("mechanics baseline historical source identity differs")
        documents[revision] = _checked_original(revision, source.content)
        _source_identity(documents[revision], revision)
    # Re-derive old field policies from their original bytes, then compare rather
    # than silently repairing old records. No fetching or rewriting of the base.
    history = build_garrison_history_batch(
        base_batch,
        historical_sources={
            revision: sources[
                next(
                    s["source_key"] for s in PHASE_BOND_PROOFS.values() if s["revision"] == revision
                )
            ].content
            for revision in HISTORICAL_SOURCES
        },
    )
    try:
        wiki_sources = {}
        for revision, pin in BASE_WIKI_PINS.items():
            source = sources[pin["source_key"]]
            if (
                source.source_type != "community_wiki_revision"
                or source.url != pin["url"]
                or source.revision != str(revision)
                or source.media_type != "application/json"
            ):
                raise IngestionError("mechanics baseline fixed wiki identity differs")
            wiki_sources[revision] = source.content
    except KeyError as exc:
        raise IngestionError("mechanics baseline fixed wiki source is missing") from exc
    expected = build_garrison_expansion_batch(history, wiki_sources=wiki_sources)
    checked_kinds = {
        "verified_rule_field",
        "cross_checked_rule_field",
        "expansion_wiki_content",
        "historical_phase",
        "historical_activity",
        "historical_shop",
        "historical_equipment",
        "historical_effect",
    }
    for row in expected.records:
        actual = records.get((row.kind, row.native_id))
        if row.kind in checked_kinds and (
            actual != row or not same_json_value(actual.payload, row.payload)
        ):
            raise IngestionError(
                "mechanics baseline existing verified record differs from raw proof"
            )
    return documents, sources, records


def _client_proof(proof: dict, documents: dict, records: dict) -> None:
    try:
        revision = proof["revision"]
        phase = proof["phase_id"]
        if (
            proof["activity_id"] != ACTIVITY_ID
            or phase != HISTORICAL_SOURCES[revision]["candidate_phase_id"]
            or proof["official_article_id"] not in ANNOUNCEMENTS
            or ANNOUNCEMENTS[proof["official_article_id"]]["phase_id"] != phase
            or proof["verification_status"] != VERIFICATION_STATUS
            or proof["mapping_basis"] != MAPPING_BASIS
            or proof["gameplay_application_verified"] is not False
            or proof["namespace_phase_mapping"] != "unverified"
            or proof["evidence_pointer"] != SEASON_POINTER + f"/bondInfoDict/{proof['bond_id']}"
        ):
            raise IngestionError("mechanics phase/native identity differs")
        phase_record = records.get(("historical_phase", f"{revision}:phase:{phase}"))
        if phase_record is None:
            raise IngestionError("mechanics baseline historical phase record is absent")
        expected_roles = {
            "bond": ("historical_bond", "bondInfoDict", "bond", proof["bond_id"]),
            "effect": ("historical_effect", "effectInfoDataDict", "effect", proof["effect_id"]),
            "buff": (
                "mechanics_client_buffs",
                "effectBuffInfoDataDict",
                "buff",
                proof["effect_id"],
            ),
        }
        if {e["role"] for e in proof["client_evidence"]} != set(expected_roles) or len(
            proof["client_evidence"]
        ) != 3:
            raise IngestionError("mechanics native proof roles differ")
        for evidence in proof["client_evidence"]:
            kind, table, native_type, key = expected_roles[evidence["role"]]
            pointer = SEASON_POINTER + f"/{table}/{key}"
            native_id = f"{revision}:{native_type}:{key}"
            node = json_pointer(documents[revision], pointer)
            if (
                evidence["pointer"] != pointer
                or evidence["record_kind"] != kind
                or evidence["native_id"] != native_id
                or evidence["value_hash"] != content_digest(node)
            ):
                raise IngestionError("mechanics client native node/hash differs")
            for selector, expected in evidence["checks"]:
                if content_digest(json_pointer(node, selector)) != content_digest(expected):
                    raise IngestionError("mechanics client parameter semantics differ")
            if evidence["role"] != "buff":
                row = records.get((kind, native_id))
                if (
                    row is None
                    or row.source_key != proof["source_key"]
                    or row.pointer != pointer
                    or content_digest(row.payload) != evidence["value_hash"]
                ):
                    raise IngestionError("mechanics baseline native relation record differs")
    except IngestionError:
        raise
    except (KeyError, TypeError, IndexError, ValueError, InvalidData) as exc:
        raise IngestionError("invalid mechanics client proof") from exc


def build_garrison_mechanics_batch(
    base_batch: SampleBatch,
    *,
    cache_dir: str | Path = ".local/garrison-mechanics",
    supplied_sources: dict[int | str, bytes] | None = None,
) -> SampleBatch:
    """Append twelve bounded descriptions and six explicit incomplete reviews."""
    documents, base_sources, base_records = _base_documents(base_batch)
    supplied = fetch_mechanics_sources(cache_dir) if supplied_sources is None else supplied_sources
    if not isinstance(supplied, dict) or set(supplied) != {
        *ADDITIONAL_WIKI_REVISIONS,
        OFFICIAL_SOURCE_ID,
    }:
        raise IngestionError("exactly five fixed mechanics originals are required")
    own_keys = {WIKI_PINS[r]["source_key"] for r in ADDITIONAL_WIKI_REVISIONS} | {
        OFFICIAL_CONTEXT_PIN["source_key"]
    }
    snapshots = [source for source in base_batch.snapshots if source.source_key not in own_keys]
    records = [row for row in base_batch.records if row.kind not in OWN_KINDS]
    texts = {}
    for revision, pin in WIKI_PINS.items():
        if revision in ADDITIONAL_WIKI_REVISIONS:
            raw = supplied[revision]
        else:
            source = base_sources.get(pin["source_key"])
            if (
                source is None
                or source.url != pin["url"]
                or source.revision != str(revision)
                or source.source_type != "community_wiki_revision"
            ):
                raise IngestionError("reused mechanics wiki source identity differs")
            raw = source.content
        _, text = _wiki_document(raw, revision)
        texts[revision] = text
        if revision not in ADDITIONAL_WIKI_REVISIONS:
            row = base_records.get((pin["record_kind"], str(revision)))
            if (
                row is None
                or row.payload != text
                or row.pointer != CONTENT_POINTER
                or row.source_key != pin["source_key"]
            ):
                raise IngestionError("reused mechanics wiki full-content record differs")
            continue
        snapshots.append(
            SnapshotInput(
                pin["source_key"],
                "community_wiki_revision",
                pin["url"],
                str(revision),
                raw,
                "application/json",
                {
                    "authority": "community_wiki",
                    "title": pin["title"],
                    "page_url": pin["page_url"],
                    "wiki_revision": revision,
                    "pinned_sha256": pin["snapshot_hash"],
                    "pinned_byte_size": pin["byte_size"],
                    "content_hash": pin["content_hash"],
                    "parser_version": PARSER_VERSION,
                    "template_expansion": "not_used",
                    "review_only": revision in (386822, 389183, 389184),
                    "rule_namespace_phase_mapping": "unverified",
                    "gameplay_application_verified": False,
                },
                datetime.fromisoformat(pin["timestamp"].replace("Z", "+00:00")),
            )
        )
        records.append(
            NormalizedRecord(
                "garrison",
                pin["record_kind"],
                str(revision),
                pin["title"],
                text,
                pin["source_key"],
                CONTENT_POINTER,
                ACTIVITY_ID,
            )
        )
    official = _official_document(supplied[OFFICIAL_SOURCE_ID])
    pin = OFFICIAL_CONTEXT_PIN
    snapshots.append(
        SnapshotInput(
            pin["source_key"],
            pin["source_type"],
            pin["url"],
            pin["revision"],
            supplied[OFFICIAL_SOURCE_ID],
            pin["media_type"],
            {
                "authority": "official",
                "pinned_sha256": pin["snapshot_hash"],
                "pinned_byte_size": pin["byte_size"],
                "parser_version": HTML_DOCUMENT_PARSER,
                "document_parser": HTML_DOCUMENT_PARSER,
                "selector_semantics": "RFC6901_on_parsed_visible_text_projection",
                "phase_id": pin["phase_id"],
                "claim_role": pin["claim_role"],
                "review_only": True,
                "gameplay_application_verified": False,
            },
        )
    )
    records.append(
        NormalizedRecord(
            "garrison",
            pin["record_kind"],
            pin["native_id"],
            "终极模拟资金获得修复：仅供机制复核",
            official,
            pin["source_key"],
            "",
            ACTIVITY_ID,
        )
    )
    for proof in PHASE_BOND_PROOFS.values():
        _client_proof(proof, documents, base_records)
        for witness in proof["wiki_evidence"]:
            _wiki_witness(witness, proof["label"], texts)
        evidence = next(e for e in proof["client_evidence"] if e["role"] == "buff")
        records.append(
            NormalizedRecord(
                "garrison",
                evidence["record_kind"],
                evidence["native_id"],
                f"{proof['phase_id']} {proof['label']} 原始 buff 配置",
                json_pointer(documents[proof["revision"]], evidence["pointer"]),
                proof["source_key"],
                evidence["pointer"],
                ACTIVITY_ID,
            )
        )
    for field_id, spec in DESCRIPTION_SPECS.items():
        base_proof = PHASE_BOND_PROOFS.get((spec["phase_id"], spec["label"]))
        if (
            field_id != f"{spec['phase_id']}:{spec['field_key']}"
            or spec["value_hash"] != content_digest(spec["value"])
            or base_proof is None
            or any(not same_json_value(spec.get(key), value) for key, value in base_proof.items())
        ):
            raise IngestionError("mechanics documented field identity/value hash differs")
        records.append(
            NormalizedRecord(
                "garrison",
                "documented_mechanics_field",
                field_id,
                spec["title"],
                {"field_spec": copy.deepcopy(spec), "value_hash": spec["value_hash"]},
                spec["source_key"],
                spec["evidence_pointer"],
                ACTIVITY_ID,
            )
        )
    for review_id, review in MECHANICS_REVIEWS.items():
        if (
            review_id != f"{review['phase_id']}:{review['topic']}"
            or review["verification_status"] != REVIEW_STATUS
            or review["query_permission"] != "refuse"
            or review["gameplay_application_verified"] is not False
            or review["namespace_phase_mapping"] != "unverified"
        ):
            raise IngestionError("mechanics incomplete review was promoted")
        records.append(
            NormalizedRecord(
                "garrison",
                "mechanics_review",
                review_id,
                f"{review['phase_id']} {review['topic']} 证据不足",
                copy.deepcopy(review),
                review["source_key"],
                review["evidence_pointer"],
                ACTIVITY_ID,
            )
        )
    for note_id, note in CANDIDATE_NOTES.items():
        if (
            note_id != str(note["wiki_revision"])
            or note["review_only"] is not True
            or note["verification_status"] != "candidate_not_runtime_verified"
            or note["phase_id"] != "V2_2"
        ):
            raise IngestionError("mechanics candidate note was promoted")
        _wiki_witness(note["witness"], "奇迹", texts)
        pin = WIKI_PINS[note["wiki_revision"]]
        records.append(
            NormalizedRecord(
                "garrison",
                "mechanics_candidate_note",
                note_id,
                note["purpose"],
                copy.deepcopy(note),
                pin["source_key"],
                CONTENT_POINTER,
                ACTIVITY_ID,
            )
        )
    coverage = {
        **copy.deepcopy(base_batch.coverage["garrison"]),
        "documented_mechanics_qa_enabled": True,
        "documented_mechanics_fields": list(DESCRIPTION_SPECS),
        "mechanics_reviews": list(MECHANICS_REVIEWS),
        "mechanics_review_available": True,
        "full_mechanics_qa_enabled": False,
        "documented_mechanics_mapping_basis": MAPPING_BASIS,
        "documented_mechanics_verification_status": VERIFICATION_STATUS,
        "mechanics_candidate_notes": list(CANDIDATE_NOTES),
        "rules_qa_enabled": False,
        "rules_engine_enabled": False,
        "gameplay_application_verified": False,
        "rule_phase_mapping": "unverified",
        "independent_human_review_count": 0,
    }
    scope = {
        **copy.deepcopy(base_batch.scopes["garrison"]),
        "documented_mechanics_mapping_basis": MAPPING_BASIS,
        "mechanics_wiki_revisions": [str(r) for r in WIKI_PINS],
        "rule_phase_mapping": "unverified",
        "full_mechanics_qa_enabled": False,
    }
    records.sort(key=lambda row: (row.kind, row.native_id))
    return SampleBatch(
        base_batch.revision, snapshots, records, {"garrison": scope}, {"garrison": coverage}
    )
