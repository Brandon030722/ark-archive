"""Cross-check individual historical fields against fixed, unexpanded wiki text.

The wiki's original revision content supplies explicit old/new change wording;
templates are never fetched or rendered. The complete client namespace remains
unassigned to any phase, and these records do not establish runtime behavior.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import datetime
from pathlib import Path

import httpx

from arknights_agent.errors import InvalidData
from arknights_agent.garrison_expansion_policy import (
    CONTENT_POINTER,
    EXPANSION_FIELD_SPECS,
    MAPPING_BASIS,
    VERIFICATION_STATUS,
    WIKI_SOURCES,
)
from arknights_agent.garrison_policy import FIELD_SPECS, HISTORICAL_SOURCES
from arknights_agent.services import content_digest, json_pointer, same_json_value

from .contracts import NormalizedRecord, SampleBatch, SnapshotInput
from .garrison import ANNOUNCEMENTS
from .garrison_history import _checked_original, _official_documents, _source_identity
from .sample import ACTIVITY_ID, SEASON_POINTER, IngestionError, pointer

PARSER_VERSION = "garrison-expanded-fields-v1"


def _wiki_document(raw: bytes, revision: int) -> tuple[dict, str]:
    pin = WIKI_SOURCES[revision]
    if (
        not isinstance(raw, bytes)
        or len(raw) != pin["byte_size"]
        or hashlib.sha256(raw).hexdigest() != pin["snapshot_hash"]
    ):
        raise IngestionError(f"fixed wiki original bytes differ: {revision}")
    try:
        document = json.loads(raw)
        pages = document["query"]["pages"]
        if len(pages) != 1:
            raise IngestionError("fixed wiki contains ambiguous pages")
        page = pages[0]
        if len(page["revisions"]) != 1:
            raise IngestionError("fixed wiki contains ambiguous revisions")
        row = page["revisions"][0]
        slot = row["slots"]["main"]
        text = slot["content"]
        if (
            page["title"] != pin["title"]
            or row["revid"] != revision
            or row["timestamp"] != pin["timestamp"]
            or slot["contentmodel"] != "wikitext"
            or slot["contentformat"] != "text/x-wiki"
            or not isinstance(text, str)
            or content_digest(text) != pin["content_hash"]
        ):
            raise IngestionError("fixed wiki revision identity/content differs")
        return document, text
    except IngestionError:
        raise
    except (KeyError, TypeError, IndexError, ValueError, UnicodeError) as exc:
        raise IngestionError(f"invalid original fixed wiki revision: {revision}") from exc


def fetch_expansion_sources(
    cache_dir: str | Path = ".local/garrison-expansion",
) -> dict[int, bytes]:
    """Fetch exact API URLs and retain their response bytes without normalization."""
    root = Path(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    sources = {}
    with httpx.Client(timeout=60, follow_redirects=False) as client:
        for revision, pin in WIKI_SOURCES.items():
            target = root / f"{revision}.json"
            if target.exists():
                raw = target.read_bytes()
                _wiki_document(raw, revision)
            else:
                try:
                    response = client.get(pin["url"])
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    raise IngestionError(f"fixed wiki fetch failed: {revision}") from exc
                raw = response.content
                _wiki_document(raw, revision)
                with tempfile.NamedTemporaryFile(dir=root, delete=False) as temporary:
                    temporary.write(raw)
                    temporary_path = Path(temporary.name)
                try:
                    temporary_path.replace(target)
                finally:
                    temporary_path.unlink(missing_ok=True)
            sources[revision] = raw
    return sources


def _wiki_field_evidence(spec: dict, wiki_texts: dict[int, str]) -> None:
    for proof in spec["wiki_evidence"]:
        revision = proof["revision"]
        pin = WIKI_SOURCES[revision]
        text = wiki_texts[revision]
        # Every quoted scalar is kept in its original article context. A matching
        # number elsewhere, a current template or a merely identical label fails.
        if (
            any(
                proof[key] != pin[key]
                for key in ("title", "source_key", "url", "page_url", "snapshot_hash", "byte_size")
            )
            or proof["content_pointer"] != CONTENT_POINTER
            or content_digest(proof["quote"]) != proof["quote_hash"]
            or text.count(proof["quote"]) != 1
            or text.count(proof["context_quote"]) != 1
            or text.index(proof["context_quote"]) >= text.index(proof["quote"])
        ):
            raise IngestionError("fixed wiki field quote/context differs from policy")
        start = text.index(proof["context_quote"])
        quote_start = text.index(proof["quote"])
        quote_end = quote_start + len(proof["quote"])
        if proof["context_kind"] == "march27_change_section":
            next_heading = re.search(
                r"\{\{(?:模板:)?AKCollapse/title\|", text[start + len(proof["context_quote"]) :]
            )
            if (
                revision != 439189
                or "|名称=卫戍协议：盟约 下半\n" not in text
                or "|活动开始时间=2026-03-14 16:00\n" not in text
                or proof["context_quote"] != "{{AKCollapse/title|3月27日更新#2调整}}"
                or next_heading is None
            ):
                raise IngestionError("fixed wiki phase change context differs")
            next_title_start = start + len(proof["context_quote"]) + next_heading.start()
            content_end = text.find("</div>", start)
            if content_end < 0 or quote_end > min(next_title_start, content_end):
                raise IngestionError("fixed wiki field is outside the phase change section")
        elif proof["context_kind"] == "named_item_block":
            block_start = text.rfind("{{:卫戍协议/道具\n", 0, start)
            block_end = text.find("\n}}", start)
            next_name = text.find("\n|名称=", start + len(proof["context_quote"]))
            if (
                revision != 430572
                or proof["context_quote"] != f"|名称={spec['label']}"
                or block_start < 0
                or "\n|类型=装备\n" not in text[block_start:start]
                or block_end < 0
                or quote_end > block_end
                or (next_name >= 0 and quote_end > next_name)
            ):
                raise IngestionError("fixed wiki field is outside its named equipment block")
        else:
            raise IngestionError("unsupported fixed wiki field context")


def _client_field(document: dict, spec: dict) -> None:
    """Check entity links, state, buff parameter keys and exact raw scalar."""
    try:
        season = json_pointer(document, SEASON_POINTER)
        effect = season["effectInfoDataDict"][spec["effect_id"]]
        expected_type = "EQUIP" if spec["entity_type"] == "equipment" else "BOND"
        if (
            effect["effectId"] != spec["effect_id"]
            or effect["effectName"] != spec["label"]
            or effect["effectType"] != expected_type
        ):
            raise IngestionError("expanded field effect identity differs")
        if spec["entity_type"] == "equipment":
            shop = season["trapShopChessDatas"][spec["normal_item_key"]]
            normal = season["trapChessDataDict"][spec["normal_item_key"]]
            golden = season["trapChessDataDict"][spec["golden_item_key"]]
            if (
                spec["state"] != "golden"
                or spec["item_key"] != spec["golden_item_key"]
                or shop["itemId"] != spec["normal_item_key"]
                or shop["goldenItemId"] != spec["golden_item_key"]
                or shop["trapId"] != spec["char_id"]
                or normal["chessId"] != spec["normal_item_key"]
                or normal["charId"] != spec["char_id"]
                or normal["isGolden"] is not False
                or normal["upgradeChessId"] != spec["golden_item_key"]
                or golden["chessId"] != spec["golden_item_key"]
                or golden["charId"] != spec["char_id"]
                or golden["isGolden"] is not True
                or golden["effectId"] != spec["effect_id"]
            ):
                raise IngestionError("expanded golden equipment native chain differs")
        elif spec["entity_type"] == "bond":
            bond = season["bondInfoDict"][spec["bond_id"]]
            if (
                spec["state"] != "bond"
                or bond["bondId"] != spec["bond_id"]
                or bond["name"] != spec["label"]
                or bond["effectId"] != spec["effect_id"]
            ):
                raise IngestionError("expanded bond native chain differs")
        else:
            raise IngestionError("unknown expanded field entity type")
        buffs = json_pointer(document, spec["evidence_pointer"])
        if (
            spec["evidence_pointer"]
            != SEASON_POINTER + pointer("effectBuffInfoDataDict", spec["effect_id"])
            or spec["field_pointer"] != spec["evidence_pointer"] + spec["relative_selector"]
            or not same_json_value(json_pointer(buffs, spec["relative_selector"]), spec["value"])
        ):
            raise IngestionError("expanded field pointer/value differs from policy")
        for selector, expected in spec["buff_checks"]:
            if not same_json_value(json_pointer(buffs, selector), expected):
                raise IngestionError("expanded field buff semantics differ from policy")
    except IngestionError:
        raise
    except (KeyError, TypeError, ValueError, IndexError, InvalidData) as exc:
        raise IngestionError("expanded client field graph is invalid") from exc


def build_garrison_expansion_batch(
    base_batch: SampleBatch,
    *,
    cache_dir: str | Path = ".local/garrison-expansion",
    wiki_sources: dict[int, bytes] | None = None,
) -> SampleBatch:
    """Append eleven separately mapped fields without reopening whole-rule QA."""
    base_coverage = base_batch.coverage.get("garrison", {})
    if (
        not base_coverage.get("verified_field_qa_enabled")
        or set(base_coverage.get("verified_rule_fields", [])) != set(FIELD_SPECS)
        or base_coverage.get("rules_qa_enabled") is not False
        or base_batch.scopes.get("garrison", {}).get("rule_phase_mapping") != "unverified"
    ):
        raise IngestionError("expanded fields require the verified three-field history batch")
    keys = [source.source_key for source in base_batch.snapshots]
    if len(keys) != len(set(keys)):
        raise IngestionError("expanded base sources contain duplicate keys")
    _official_documents(base_batch)
    sources_by_key = {source.source_key: source for source in base_batch.snapshots}
    documents = {}
    for revision in HISTORICAL_SOURCES:
        source_keys = {
            spec["source_key"]
            for spec in EXPANSION_FIELD_SPECS.values()
            if spec["revision"] == revision
        }
        if len(source_keys) != 1:
            raise IngestionError("expanded policy has ambiguous historic source keys")
        key = next(iter(source_keys))
        source = sources_by_key.get(key)
        matching_spec = next(s for s in EXPANSION_FIELD_SPECS.values() if s["revision"] == revision)
        if (
            source is None
            or source.revision != revision
            or source.source_type != "historical_client_data"
            or source.url != matching_spec["source_url"]
        ):
            raise IngestionError("expanded field historical source identity differs")
        document = _checked_original(revision, source.content)
        _source_identity(document, revision)
        documents[revision] = document
    supplied = fetch_expansion_sources(cache_dir) if wiki_sources is None else wiki_sources
    if not isinstance(supplied, dict) or set(supplied) != set(WIKI_SOURCES):
        raise IngestionError("exactly both pinned wiki originals are required")
    records = [
        record
        for record in base_batch.records
        if record.module == "garrison"
        and record.kind not in {"cross_checked_rule_field", "expansion_wiki_content"}
    ]
    wiki_keys = {pin["source_key"] for pin in WIKI_SOURCES.values()}
    snapshots = [source for source in base_batch.snapshots if source.source_key not in wiki_keys]
    wiki_texts = {}
    for revision, pin in WIKI_SOURCES.items():
        _, text = _wiki_document(supplied[revision], revision)
        wiki_texts[revision] = text
        snapshots.append(
            SnapshotInput(
                pin["source_key"],
                "community_wiki_revision",
                pin["url"],
                str(revision),
                supplied[revision],
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
                    "rule_namespace_phase_mapping": "unverified",
                    "gameplay_application_verified": False,
                },
                datetime.fromisoformat(pin["timestamp"].replace("Z", "+00:00")),
            )
        )
        records.append(
            NormalizedRecord(
                "garrison",
                "expansion_wiki_content",
                str(revision),
                pin["title"],
                text,
                pin["source_key"],
                CONTENT_POINTER,
                ACTIVITY_ID,
            )
        )
    relation_keys = {(record.kind, record.native_id) for record in records}
    for field_id, spec in EXPANSION_FIELD_SPECS.items():
        _client_field(documents[spec["revision"]], spec)
        _wiki_field_evidence(spec, wiki_texts)
        if (
            spec["phase_id"] != HISTORICAL_SOURCES[spec["revision"]]["candidate_phase_id"]
            or spec["official_article_id"] not in ANNOUNCEMENTS
            or ANNOUNCEMENTS[spec["official_article_id"]]["phase_id"] != spec["phase_id"]
        ):
            raise IngestionError("expanded field phase identity differs")
        revision = spec["revision"]
        relation_ids = {"phase": f"{revision}:phase:{spec['phase_id']}"}
        native_paths = [("effect", "effectInfoDataDict", spec["effect_id"])]
        if spec["entity_type"] == "equipment":
            native_paths += [
                ("shop", "trapShopChessDatas", spec["normal_item_key"]),
                ("normal_equipment", "trapChessDataDict", spec["normal_item_key"]),
                ("equipment", "trapChessDataDict", spec["golden_item_key"]),
            ]
        else:
            native_paths.append(("bond", "bondInfoDict", spec["bond_id"]))
        for role, table, key in native_paths:
            kind = "equipment" if role == "normal_equipment" else role
            native_id = f"{revision}:{kind}:{key}"
            relation_ids[role] = native_id
            identity = (f"historical_{kind}", native_id)
            if identity not in relation_keys:
                path = SEASON_POINTER + pointer(table, key)
                records.append(
                    NormalizedRecord(
                        "garrison",
                        identity[0],
                        native_id,
                        f"{key} 历史配置 {revision[:8]}",
                        json_pointer(documents[revision], path),
                        spec["source_key"],
                        path,
                        ACTIVITY_ID,
                    )
                )
                relation_keys.add(identity)
        if ("historical_phase", relation_ids["phase"]) not in relation_keys:
            raise IngestionError("expanded field historical phase proof is absent")
        records.append(
            NormalizedRecord(
                "garrison",
                "cross_checked_rule_field",
                field_id,
                f"盟约 {spec['phase_id']} {spec['label']} {spec['field_key']}",
                {
                    "field_spec": dict(spec),
                    "field_value_hash": content_digest(spec["value"]),
                    "relation_record_ids": relation_ids,
                    "wiki_content_record_ids": [
                        str(proof["revision"]) for proof in spec["wiki_evidence"]
                    ],
                    "official_announcement_record": spec["official_article_id"],
                    "mapping_basis": MAPPING_BASIS,
                    "verification_status": VERIFICATION_STATUS,
                    "gameplay_application_verified": False,
                    "namespace_phase_mapping": "unverified",
                },
                spec["source_key"],
                spec["evidence_pointer"],
                ACTIVITY_ID,
            )
        )
    scope = {
        **base_batch.scopes["garrison"],
        "cross_checked_field_mapping_basis": MAPPING_BASIS,
        "fixed_wiki_revisions": [str(revision) for revision in WIKI_SOURCES],
        "rule_phase_mapping": "unverified",
    }
    coverage = {
        **base_coverage,
        "cross_checked_rule_fields": list(EXPANSION_FIELD_SPECS),
        "cross_checked_field_qa_enabled": True,
        "cross_checked_rule_field_review": VERIFICATION_STATUS,
        "fixed_wiki_revisions": [str(revision) for revision in WIKI_SOURCES],
        "rules_qa_enabled": False,
        "rules_engine_enabled": False,
        "gameplay_application_verified": False,
        "rule_phase_mapping": "unverified",
        "independent_human_review_count": 0,
    }
    records.sort(key=lambda record: (record.kind, record.native_id))
    return SampleBatch(
        base_batch.revision, snapshots, records, {"garrison": scope}, {"garrison": coverage}
    )
