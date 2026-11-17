"""Append pinned historic configurations and a three-field evidence whitelist.

The entire rule namespace has no direct phase marker. Only individual fields
corroborated by the corresponding original official change statement are mapped.
The two complete client files are retained even though only small raw rows become
query records. A missing first-phase buff is never normalized into a zero value.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime
from pathlib import Path

import httpx

from arknights_agent.errors import InvalidData
from arknights_agent.garrison_policy import FIELD_SPECS, HISTORICAL_SOURCES, OFFICIAL_SOURCE_HASHES
from arknights_agent.services import content_digest, json_pointer, same_json_value

from .contracts import NormalizedRecord, SampleBatch, SnapshotInput
from .garrison import ANNOUNCEMENTS, _check_namespace, html_document
from .sample import ACTIVITY_ID, REPO, SEASON_POINTER, SOURCE_PATH, IngestionError, pointer

PARSER_VERSION = "garrison-history-fields-v1"


def _checked_original(revision: str, raw: bytes) -> dict:
    expected = HISTORICAL_SOURCES[revision]
    if (
        not isinstance(raw, bytes)
        or len(raw) != expected["byte_size"]
        or hashlib.sha256(raw).hexdigest() != expected["snapshot_hash"]
    ):
        raise IngestionError(f"historical original bytes differ from fixed pin: {revision}")
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise IngestionError(f"historical source is not JSON: {revision}") from exc
    if not isinstance(document, dict):
        raise IngestionError(f"historical source is not a JSON object: {revision}")
    return document


def fetch_historical_sources(
    cache_dir: str | Path = ".local/garrison-history",
) -> dict[str, bytes]:
    """Fetch fixed revision URLs; reject changed cached bytes rather than repair them."""
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    sources = {}
    for revision in HISTORICAL_SOURCES:
        path = cache / f"{revision}.activity_table.json"
        if path.exists():
            raw = path.read_bytes()
            _checked_original(revision, raw)
        else:
            url = (
                f"https://raw.githubusercontent.com/{REPO}/{revision}/"
                f"{SOURCE_PATH}/activity_table.json"
            )
            try:
                response = httpx.get(url, timeout=60, follow_redirects=False)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise IngestionError(f"historical source fetch failed: {revision}") from exc
            raw = response.content
            _checked_original(revision, raw)
            with tempfile.NamedTemporaryFile(dir=cache, delete=False) as temporary:
                temporary.write(raw)
                temporary_path = Path(temporary.name)
            try:
                temporary_path.replace(path)
            finally:
                temporary_path.unlink(missing_ok=True)
        sources[revision] = raw
    return sources


def _source_identity(document: dict, revision: str) -> dict:
    try:
        basic = json_pointer(document, pointer("basicInfo", ACTIVITY_ID))
        season = json_pointer(document, SEASON_POINTER)
        versions = json_pointer(document, "/autoChessData/versionInfoDict")
        if basic["id"] != ACTIVITY_ID or basic["type"] != "AUTOCHESS_SEASON":
            raise IngestionError("historical activity identity/type mismatch")
        if not basic["startTime"] < basic["endTime"] <= basic["rewardEndTime"]:
            raise IngestionError("historical activity timeline is not ordered")
        for expected in ANNOUNCEMENTS.values():
            phase = versions[expected["phase_id"]]
            start = int(datetime.fromisoformat(expected["maintenance_start"]).timestamp())
            if (
                phase["activityId"] != ACTIVITY_ID
                or phase["seasonName"] != "盟约"
                or not same_json_value(phase["startTime"], start)
            ):
                raise IngestionError("historical phase identity/schedule differs")
        return _check_namespace(season)
    except (KeyError, TypeError, ValueError, InvalidData) as exc:
        raise IngestionError(f"invalid historical activity graph: {revision}") from exc


def _official_documents(base_batch: SampleBatch) -> dict[str, dict]:
    documents = {}
    for article_id in ANNOUNCEMENTS:
        matches = [
            source
            for source in base_batch.snapshots
            if source.source_key == f"arknights-official:news/{article_id}"
        ]
        if len(matches) != 1 or matches[0].source_type != "official_announcement":
            raise IngestionError(f"one original official announcement is required: {article_id}")
        source = matches[0]
        digest = hashlib.sha256(source.content).hexdigest()
        if (
            source.url != f"https://ak.hypergryph.com/news/{article_id}"
            or source.revision != f"sha256:{digest}"
            or digest != OFFICIAL_SOURCE_HASHES[article_id]
            or source.media_type != "text/html"
        ):
            raise IngestionError(f"official snapshot identity differs: {article_id}")
        doc = html_document(source.content)
        nodes = doc["html_text_nodes"]
        expected = ANNOUNCEMENTS[article_id]
        if expected["publication_date"] not in nodes or nodes.count(expected["lead"]) != 1:
            raise IngestionError(f"official date/phase statement differs: {article_id}")
        announcements = [
            record
            for record in base_batch.records
            if record.module == "garrison"
            and record.kind == "official_phase_announcement"
            and record.native_id == article_id
        ]
        phases = [
            record
            for record in base_batch.records
            if record.module == "garrison"
            and record.kind == "verified_phase"
            and record.native_id == expected["phase_id"]
        ]
        if (
            len(announcements) != 1
            or announcements[0].source_key != source.source_key
            or announcements[0].pointer != ""
            or announcements[0].payload.get("activity_id") != ACTIVITY_ID
            or announcements[0].payload.get("phase_id") != expected["phase_id"]
            or len(phases) != 1
            or phases[0].payload.get("mapping_status") != "scheduled_phase_metadata_verified"
        ):
            raise IngestionError(f"base reviewed announcement/phase record differs: {article_id}")
        documents[article_id] = doc
    return documents


def _verified_source_field(document: dict, spec: dict, official: dict, source: SnapshotInput):
    """Validate native identity and official wording, not merely equal scalar numbers."""
    revision = spec["revision"]
    if (
        source.revision != revision
        or source.source_key != spec["source_key"]
        or source.url != spec["source_url"]
        or hashlib.sha256(source.content).hexdigest() != spec["snapshot_hash"]
    ):
        raise IngestionError("field source differs from pinned policy")
    try:
        season = json_pointer(document, SEASON_POINTER)
        shop = season["trapShopChessDatas"][spec["item_key"]]
        equipment = season["trapChessDataDict"][spec["item_key"]]
        effect = season["effectInfoDataDict"][spec["effect_id"]]
        if (
            shop["itemId"] != spec["item_key"]
            or shop["goldenItemId"] != spec["golden_item_key"]
            or shop["trapId"] != spec["char_id"]
            or equipment["chessId"] != spec["item_key"]
            or equipment["charId"] != spec["char_id"]
            or equipment["isGolden"] is not False
            or equipment["upgradeChessId"] != spec["golden_item_key"]
            or equipment["effectId"] != spec["effect_id"]
            or effect["effectId"] != spec["effect_id"]
            or effect["effectName"] != spec["label"]
        ):
            raise IngestionError("historical shop/equipment/effect native identity mismatch")
        if not same_json_value(json_pointer(document, spec["field_pointer"]), spec["value"]):
            raise IngestionError("historical field value differs from policy")
        buffs = season["effectBuffInfoDataDict"][spec["effect_id"]]
        if spec["field_key"] == "commercial_sell_count":
            if (
                buffs[0]["key"] != "sell_char_count_gain_equip_owner_bond"
                or buffs[0]["blackboard"][0]["key"] != "count"
            ):
                raise IngestionError("commercial field semantics differ from policy")
        elif spec["field_key"] == "arcane_silence_seconds":
            if (
                buffs[1]["key"] != "env_gbuff_new_with_verify"
                or buffs[1]["blackboard"][0]["key"] != "key"
                or buffs[1]["blackboard"][0]["valueStr"] != "silence_attachment"
                or buffs[1]["blackboard"][1]["key"] != "silence"
            ):
                raise IngestionError("arcane silence field semantics differ from policy")
        else:
            raise IngestionError("unknown verified historical field")
        if json_pointer(official, spec["official_selector"]) != spec["official_quote"]:
            raise IngestionError("official field statement differs from policy")
    except (KeyError, TypeError, ValueError, IndexError, InvalidData) as exc:
        raise IngestionError("historical field identity/evidence is invalid") from exc


def _relation_record(revision: str, kind: str, key: str, raw: dict, path: str, source_key: str):
    return NormalizedRecord(
        "garrison",
        f"historical_{kind}",
        f"{revision}:{kind}:{key}",
        f"{key} 历史配置 {revision[:8]}",
        raw,
        source_key,
        path,
        ACTIVITY_ID,
    )


def build_garrison_history_batch(
    base_batch: SampleBatch,
    *,
    cache_dir: str | Path = ".local/garrison-history",
    historical_sources: dict[str, bytes] | None = None,
) -> SampleBatch:
    """Return only the garrison dataset, retaining base sources and raw evidence.

    Publication must merge this dataset with existing operator/recruitment dataset
    IDs. The base metadata source revision is unchanged; each historic source and
    mapped field explicitly carries its separate pinned revision.
    """
    if (
        "garrison" not in base_batch.scopes
        or not base_batch.coverage.get("garrison", {}).get("metadata_qa_enabled")
        or base_batch.scopes["garrison"].get("rule_phase_mapping") != "unverified"
    ):
        raise IngestionError("history ingestion requires the phase-reviewed base dataset")
    base_keys = [source.source_key for source in base_batch.snapshots]
    if len(base_keys) != len(set(base_keys)):
        raise IngestionError("base snapshots contain duplicate source keys")
    official_documents = _official_documents(base_batch)
    supplied = (
        fetch_historical_sources(cache_dir) if historical_sources is None else historical_sources
    )
    if not isinstance(supplied, dict) or set(supplied) != set(HISTORICAL_SOURCES):
        raise IngestionError("exactly both pinned historical originals are required")
    records = [
        record
        for record in base_batch.records
        if record.module == "garrison"
        and record.kind
        not in {
            "verified_rule_field",
            "historical_phase",
            "historical_activity",
            "historical_shop",
            "historical_equipment",
            "historical_effect",
        }
    ]
    historical_keys = {spec["source_key"] for spec in FIELD_SPECS.values()}
    snapshots = [s for s in base_batch.snapshots if s.source_key not in historical_keys]
    documents, sources = {}, {}
    for revision, pin in HISTORICAL_SOURCES.items():
        raw = supplied[revision]
        document = _checked_original(revision, raw)
        counts = _source_identity(document, revision)
        matching_specs = [spec for spec in FIELD_SPECS.values() if spec["revision"] == revision]
        if not matching_specs:
            raise IngestionError("historical source has no field policy")
        spec = matching_specs[0]
        source = SnapshotInput(
            source_key=spec["source_key"],
            source_type="historical_client_data",
            url=spec["source_url"],
            revision=revision,
            content=raw,
            media_type="application/json",
            metadata={
                "authority": "community_client_data_mirror",
                "region": "CN",
                "repository": REPO,
                "path": f"{SOURCE_PATH}/activity_table.json",
                "pinned_sha256": pin["snapshot_hash"],
                "pinned_byte_size": pin["byte_size"],
                "git_commit_timestamp": pin["commit_timestamp"],
                "timestamp_semantics": "mirror_commit_not_game_effective_time",
                "parser_version": PARSER_VERSION,
                "namespace_reference_counts": counts,
                "namespace_hash": content_digest(json_pointer(document, SEASON_POINTER)),
                "namespace_phase_mapping": "unverified",
                "actual_game_effect_verified": False,
            },
        )
        snapshots.append(source)
        documents[revision], sources[revision] = document, source
        activity_path = pointer("basicInfo", ACTIVITY_ID)
        records.append(
            _relation_record(
                revision,
                "activity",
                ACTIVITY_ID,
                json_pointer(document, activity_path),
                activity_path,
                source.source_key,
            )
        )
        # Both phase keys are deliberately retained in both originals. This is
        # evidence of preinstalled content, not permission for global assignment.
        for phase_id in ("V2_1", "V2_2"):
            phase_path = pointer("autoChessData", "versionInfoDict", phase_id)
            records.append(
                _relation_record(
                    revision,
                    "phase",
                    phase_id,
                    json_pointer(document, phase_path),
                    phase_path,
                    source.source_key,
                )
            )

    relations_added = set()
    for field_id, spec in FIELD_SPECS.items():
        revision = spec["revision"]
        source = sources[revision]
        article_id = spec["official_article_id"]
        official_source = next(
            s for s in snapshots if s.source_key == f"arknights-official:news/{article_id}"
        )
        if hashlib.sha256(official_source.content).hexdigest() != spec["official_hash"]:
            raise IngestionError("official original bytes differ from fixed field policy")
        _verified_source_field(documents[revision], spec, official_documents[article_id], source)
        relation_ids = {"phase": f"{revision}:phase:{spec['phase_id']}"}
        for kind, table, key in (
            ("shop", "trapShopChessDatas", spec["item_key"]),
            ("equipment", "trapChessDataDict", spec["item_key"]),
            ("effect", "effectInfoDataDict", spec["effect_id"]),
        ):
            native_id = f"{revision}:{kind}:{key}"
            relation_ids[kind] = native_id
            if native_id not in relations_added:
                path = f"{SEASON_POINTER}{pointer(table, key)}"
                records.append(
                    _relation_record(
                        revision,
                        kind,
                        key,
                        json_pointer(documents[revision], path),
                        path,
                        source.source_key,
                    )
                )
                relations_added.add(native_id)
        records.append(
            NormalizedRecord(
                "garrison",
                "verified_rule_field",
                field_id,
                f"盟约 {spec['phase_id']} {spec['label']}",
                {
                    "field_spec": dict(spec),
                    "field_value_hash": content_digest(spec["value"]),
                    "relation_record_ids": relation_ids,
                    "official_announcement_record": article_id,
                    "mapping_basis": "historical_revision_and_official_change_cross_checked",
                    "verification_status": "official_cross_checked_field",
                    "gameplay_application_verified": False,
                    "namespace_phase_mapping": "unverified",
                },
                source.source_key,
                f"{SEASON_POINTER}{pointer('effectBuffInfoDataDict', spec['effect_id'])}",
                ACTIVITY_ID,
            )
        )
    scope = {
        **base_batch.scopes["garrison"],
        "base_metadata_source_revision": base_batch.revision,
        "historical_source_revisions": list(HISTORICAL_SOURCES),
        "rule_phase_mapping": "unverified",
        "field_mapping_basis": "historical_revision_and_official_change_cross_checked",
    }
    coverage = {
        **base_batch.coverage["garrison"],
        "historical_source_revisions": list(HISTORICAL_SOURCES),
        "verified_rule_fields": list(FIELD_SPECS),
        "verified_field_qa_enabled": True,
        "rule_phase_mapping": "unverified",
        "rules_qa_enabled": False,
        "rules_engine_enabled": False,
        "actual_opening_time_verified": False,
        "gameplay_application_verified": False,
        "rule_field_review": "official_cross_checked_field",
        "independent_human_review_count": 0,
    }
    return SampleBatch(
        base_batch.revision, snapshots, records, {"garrison": scope}, {"garrison": coverage}
    )
