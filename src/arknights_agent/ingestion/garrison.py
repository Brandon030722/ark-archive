"""Verify retained garrison namespaces and the two scheduled phase start mappings.

The official HTML is stored verbatim. Evidence selectors address a deterministic
visible-text projection of those original bytes, never a fabricated official JSON.
Retained client rules cannot establish their historic V2_1/V2_2 applicability.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .contracts import NormalizedRecord, SampleBatch, SnapshotInput
from .sample import ACTIVITY_ID, SEASON_POINTER, IngestionError, pointer, source_key

PARSER_VERSION = "garrison-phase-review-v1"
HTML_DOCUMENT_PARSER = "html-visible-text-v1"
TIMEZONE = ZoneInfo("Asia/Shanghai")
ANNOUNCEMENTS = {
    "5114": {
        "phase_id": "V2_1",
        "date": "2026-03-14",
        "publication_date": "2026 // 03 / 14",
        "phase_label": "盟约下半期活动的初始阶段",
        "official_label": "下半期活动",
        "maintenance_start": "2026-03-14T16:00:00+08:00",
        "maintenance_end": "2026-03-14T17:00:00+08:00",
        "lead": "「卫戍协议：盟约」下半期活动将于03月14日16:00 ~ 17:00的停机维护后开启，此次活动在优化系统的同时更新了活动内容，具体内容如下：",
    },
    "8584": {
        "phase_id": "V2_2",
        "date": "2026-03-27",
        "publication_date": "2026 // 03 / 27",
        "phase_label": "盟约下半期活动的第二阶段",
        "official_label": "第二阶段",
        "maintenance_start": "2026-03-27T16:00:00+08:00",
        "maintenance_end": "2026-03-27T16:10:00+08:00",
        "lead": "「卫戍协议：盟约」将在03月27日16:00 ~ 16:10的闪断更新后开启第二阶段，本次更新在新增策略、模拟协议等内容的同时也将调整部分活动内容，具体内容如下：",
    },
}


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden_depth = 0
        self.nodes: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"script", "style", "noscript"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style", "noscript"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data):
        if not self.hidden_depth:
            text = re.sub(r"\s+", " ", data).strip()
            if text:
                self.nodes.append(text)


def html_document(raw: bytes) -> dict:
    """The stable parsed document used for RFC6901 evidence selectors.

    This projection deliberately retains text nodes rather than executing script,
    fetching embedded resources, or treating website content as instructions.
    """
    if not isinstance(raw, bytes) or len(raw) > 5_000_000:
        raise IngestionError("official HTML must be bytes smaller than 5 MB")
    try:
        parser = _VisibleText()
        parser.feed(raw.decode("utf-8"))
        parser.close()
    except (UnicodeError, ValueError) as exc:
        raise IngestionError("invalid UTF-8 official HTML") from exc
    return {"html_text_nodes": parser.nodes}


def _official_key(article_id: str) -> str:
    return f"arknights-official:news/{article_id}"


def fetch_announcements(cache_dir: str | Path = ".local/garrison-sources") -> dict[str, bytes]:
    """Fetch only the two fixed official URLs, retaining full response bytes."""
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    results = {}
    for article_id in ANNOUNCEMENTS:
        path = cache / f"{article_id}.html"
        if path.exists():
            results[article_id] = path.read_bytes()
            continue
        url = f"https://ak.hypergryph.com/news/{article_id}"
        try:
            response = httpx.get(url, timeout=40, follow_redirects=False)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise IngestionError(f"official announcement fetch failed: {article_id}") from exc
        content = response.content
        html_document(content)
        # Atomic cache replacement; no shared temporary filename across callers.
        import tempfile

        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        try:
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)
        results[article_id] = content
    return results


def _dict(value, location):
    if not isinstance(value, dict):
        raise IngestionError(f"{location}: expected object")
    return value


def _integer(value, location):
    if not isinstance(value, int) or isinstance(value, bool):
        raise IngestionError(f"{location}: expected integer Unix timestamp")
    return value


def _time(timestamp):
    return datetime.fromtimestamp(timestamp, TIMEZONE).isoformat()


def _check_namespace(season: dict) -> dict:
    """Validate identifiers/references inside this retained activity namespace.

    Referential integrity is weaker than evidence that a rule applied in a phase.
    No phase is inferred from shop IDs, the season name, or an ingestion date.
    """
    keys = (
        "modeDataDict",
        "charShopChessDatas",
        "charChessDataDict",
        "bondInfoDict",
        "trapShopChessDatas",
        "trapChessDataDict",
        "effectInfoDataDict",
    )
    tables = {key: _dict(season.get(key), key) for key in keys}
    bonds, chess = tables["bondInfoDict"], tables["charChessDataDict"]
    equipment, effects = tables["trapChessDataDict"], tables["effectInfoDataDict"]
    for mode_id, mode in tables["modeDataDict"].items():
        if mode.get("modeId") != mode_id:
            raise IngestionError(f"garrison modeId/key mismatch: {mode_id}")
        active = mode.get("activeBondIdList")
        if not isinstance(active, list) or any(bond not in bonds for bond in active):
            raise IngestionError(f"garrison mode references an unknown bond: {mode_id}")
    for chess_id, row in chess.items():
        if row.get("chessId") != chess_id or not isinstance(row.get("bondIds"), list):
            raise IngestionError(f"garrison chess identity/bond shape mismatch: {chess_id}")
        if any(bond not in bonds for bond in row["bondIds"]):
            raise IngestionError(f"garrison chess references an unknown bond: {chess_id}")
    for chess_id, row in tables["charShopChessDatas"].items():
        if row.get("chessId") != chess_id or chess_id not in chess:
            raise IngestionError(f"garrison shop/chess mismatch: {chess_id}")
        if row.get("goldenChessId") not in chess:
            raise IngestionError(f"garrison shop references an unknown golden chess: {chess_id}")
    for equipment_id, row in equipment.items():
        if row.get("chessId") != equipment_id or row.get("effectId") not in effects:
            raise IngestionError(f"garrison equipment/effect mismatch: {equipment_id}")
    for equipment_id, row in tables["trapShopChessDatas"].items():
        if row.get("itemId") != equipment_id or equipment_id not in equipment:
            raise IngestionError(f"garrison shop/equipment mismatch: {equipment_id}")
        if row.get("goldenItemId") is not None and row["goldenItemId"] not in equipment:
            raise IngestionError(
                f"garrison shop references unknown golden equipment: {equipment_id}"
            )
    return {key: len(value) for key, value in tables.items()}


def build_garrison_batch(
    base_batch: SampleBatch,
    official_sources: dict[str, bytes] | None = None,
    *,
    cache_dir: str | Path = ".local/garrison-sources",
) -> SampleBatch:
    """Add reviewed metadata while preserving all original operator/recruit records."""
    source = next(
        (s for s in base_batch.snapshots if s.source_key == source_key("activity_table.json")),
        None,
    )
    if source is None:
        raise IngestionError("garrison verification requires the original activity_table")
    activity = json.loads(source.content)
    basic = _dict(activity.get("basicInfo", {}).get(ACTIVITY_ID), "basicInfo/act2autochess")
    if basic.get("id") != ACTIVITY_ID or basic.get("type") != "AUTOCHESS_SEASON":
        raise IngestionError("garrison activity identity/type mismatch")
    starts = _integer(basic.get("startTime"), "activity startTime")
    ends = _integer(basic.get("endTime"), "activity endTime")
    rewards_end = _integer(basic.get("rewardEndTime"), "activity rewardEndTime")
    if not starts < ends <= rewards_end:
        raise IngestionError("garrison activity timeline is not ordered")
    season = _dict(
        activity.get("activity", {}).get("AUTOCHESS_SEASON", {}).get(ACTIVITY_ID),
        SEASON_POINTER,
    )
    namespace_counts = _check_namespace(season)
    versions = _dict(activity.get("autoChessData", {}).get("versionInfoDict"), "versions")
    supplied = fetch_announcements(cache_dir) if official_sources is None else official_sources
    if set(supplied) != set(ANNOUNCEMENTS):
        raise IngestionError("both original official announcements are required")
    snapshots = list(base_batch.snapshots)
    records = [
        row
        for row in base_batch.records
        if not (
            row.module == "garrison"
            and row.kind in {"verified_activity", "verified_phase", "official_phase_announcement"}
        )
    ]
    verified_phases = []
    for article_id, expected in ANNOUNCEMENTS.items():
        raw = supplied[article_id]
        document = html_document(raw)
        nodes = document["html_text_nodes"]
        if expected["publication_date"] not in nodes or nodes.count(expected["lead"]) != 1:
            raise IngestionError(f"official article date/phase wording differs: {article_id}")
        text_index = nodes.index(expected["lead"])
        digest = hashlib.sha256(raw).hexdigest()
        article_key = _official_key(article_id)
        if any(s.source_key == article_key for s in snapshots):
            raise IngestionError(f"duplicate official source snapshot: {article_key}")
        snapshots.append(
            SnapshotInput(
                source_key=article_key,
                source_type="official_announcement",
                url=f"https://ak.hypergryph.com/news/{article_id}",
                revision=f"sha256:{digest}",
                content=raw,
                media_type="text/html",
                metadata={
                    "authority": "official",
                    "region": "CN",
                    "article_id": article_id,
                    "document_parser": HTML_DOCUMENT_PARSER,
                    "selector_semantics": "RFC6901_on_parsed_visible_text_projection",
                    "parser_version": PARSER_VERSION,
                    "published_date": expected["date"],
                    "publication_time_precision": "date_only",
                },
            )
        )
        phase_id = expected["phase_id"]
        version = _dict(versions.get(phase_id), f"versionInfoDict/{phase_id}")
        if version.get("activityId") != ACTIVITY_ID or version.get("seasonName") != "盟约":
            raise IngestionError(f"phase belongs to another garrison activity: {phase_id}")
        start = _integer(version.get("startTime"), f"{phase_id}/startTime")
        scheduled_start = datetime.fromisoformat(expected["maintenance_start"])
        if start != int(scheduled_start.timestamp()) or not starts <= start < ends:
            raise IngestionError(
                f"phase client timestamp differs from official schedule: {phase_id}"
            )
        if phase_id == "V2_1" and start != starts:
            raise IngestionError("V2_1 must match the retained activity start")
        announcement_payload = {
            "article_id": article_id,
            "activity_id": ACTIVITY_ID,
            "phase_id": phase_id,
            "official_label": expected["official_label"],
            "maintenance_start": expected["maintenance_start"],
            "maintenance_end": expected["maintenance_end"],
            "opening_condition": "after_maintenance",
            "actual_opening_time_verified": False,
            "verification_status": "official_schedule_verified",
            "schedule_selector": pointer("html_text_nodes", text_index),
            "publication_date_selector": pointer(
                "html_text_nodes", nodes.index(expected["publication_date"])
            ),
        }
        records.append(
            NormalizedRecord(
                "garrison",
                "official_phase_announcement",
                article_id,
                f"盟约 {phase_id} 官方开启公告",
                announcement_payload,
                article_key,
                "",
                ACTIVITY_ID,
            )
        )
        phase_payload = {
            "activity_id": ACTIVITY_ID,
            "phase_id": phase_id,
            "phase_label": expected["phase_label"],
            "official_label": expected["official_label"],
            "client_start_time": _time(start),
            "client_start_timestamp": start,
            "maintenance_start": expected["maintenance_start"],
            "maintenance_end": expected["maintenance_end"],
            "opening_condition": "after_maintenance",
            "actual_opening_time_verified": False,
            "official_announcement_source_key": article_key,
            "official_announcement_hash": digest,
            "official_announcement_record": article_id,
            "mapping_status": "scheduled_phase_metadata_verified",
            "verified_fields": [
                "activity_id",
                "phase_id",
                "client_start_time",
                "official_schedule",
            ],
            "rule_payload_assignment": "unverified",
            "mode_rule_assignment": "unverified",
            "chess_rule_assignment": "unverified",
            "bond_rule_assignment": "unverified",
            "equipment_rule_assignment": "unverified",
        }
        records.append(
            NormalizedRecord(
                "garrison",
                "verified_phase",
                phase_id,
                expected["phase_label"],
                phase_payload,
                source.source_key,
                pointer("autoChessData", "versionInfoDict", phase_id),
                ACTIVITY_ID,
            )
        )
        verified_phases.append(phase_id)
    records.append(
        NormalizedRecord(
            "garrison",
            "verified_activity",
            ACTIVITY_ID,
            basic["name"],
            {
                "activity_id": ACTIVITY_ID,
                "activity_name": basic["name"],
                "client_start_time": _time(starts),
                "client_end_time": _time(ends),
                "client_reward_end_time": _time(rewards_end),
                "timeline_source": "retained_client_configuration",
                "official_period_label": "盟约下半期活动",
                "phase_ids": verified_phases,
                "mapping_status": "partial_verified",
                "namespace_reference_counts": namespace_counts,
                "rule_payload_assignment": "unverified",
            },
            source.source_key,
            pointer("basicInfo", ACTIVITY_ID),
        )
    )
    scopes = dict(base_batch.scopes)
    coverage = dict(base_batch.coverage)
    scopes["garrison"] = {
        **scopes["garrison"],
        "mapping_status": "partial_verified",
        "verified_phase_ids": verified_phases,
        "period_label": "盟约下半期活动",
        "rule_phase_mapping": "unverified",
    }
    coverage["garrison"] = {
        **coverage["garrison"],
        "mapping_status": "partial_verified",
        "metadata_qa_enabled": True,
        "verified_phase_ids": verified_phases,
        "verified_metadata_fields": [
            "activity_id",
            "phase_id",
            "client_start_time",
            "official_schedule",
        ],
        "rule_namespace_status": "references_checked_in_retained_activity",
        "namespace_reference_counts": namespace_counts,
        "rule_phase_mapping": "unverified",
        "rules_qa_enabled": False,
        "rules_engine_enabled": False,
        "actual_opening_time_verified": False,
    }
    return SampleBatch(base_batch.revision, snapshots, records, scopes, coverage)
