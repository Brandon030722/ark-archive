"""Append the reviewed CN Regulation Sequence I snapshot to the garrison dataset."""

from collections import Counter
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from arknights_agent.services import json_pointer, snapshot_document

from .contracts import NormalizedRecord, SampleBatch, SnapshotInput
from .garrison import HTML_DOCUMENT_PARSER, TIMEZONE
from .sample import REPO, SOURCE_PATH, IngestionError, pointer

PARSER_VERSION = "garrison-regulation-sequence1-v2"
ACTIVITY = "act3autochess"
PHASE = "V3_1"
CLIENT_REVISION = "6f63c14e27b847ef74b53b00fe51fc322b3cd7ff"
CLIENT_KEY = f"garrison-regulation:{CLIENT_REVISION}:activity_table"
CLIENT_URL = f"https://raw.githubusercontent.com/{REPO}/{CLIENT_REVISION}/{SOURCE_PATH}/activity_table.json"
CLIENT_HASH = "8a4327bb9651f875c67990e3a2faab2ba8fe81559d322cdbd27bf34bf0f7169d"
ANNOUNCEMENTS = {
    "0787": ("2026-10-24", "d33ffa75aae84deb0a8067627ede52244b8df67a94279cfc04e872083c37bf12"),
    "4915": ("2026-11-08", "fd7ddd165286a6141c96770163250d30606033c941cac0a2e2589ca258ad03f7"),
    "5197": ("2026-11-08", "1337f7892b2b996d52965a951328a89d600bb92b200ae34b806efcc8b9b414a1"),
}

# Locations in the exact official HTML snapshots above.
OFFICIAL_RECORDS = {
    "launch": ("4915", 11, "序列 I 开启公告"),
    "maintenance": ("5197", 4, "维护结束公告"),
    "scheduled_time": ("0787", 420, "公告计划开启时间"),
    "unlock": ("0787", 422, "解锁条件"),
    "currency": ("0787", 424, "卫戍认证与功勋兑换处"),
    "season_structure": ("0787", 425, "赛季常驻与三个时期"),
    "open_period": ("0787", 426, "开放期"),
    "revision_period": ("0787", 427, "修订期"),
    "end_period": ("0787", 428, "终止期"),
    "content_update": ("0787", 446, "开放期内容更新安排"),
}

SEASON_TABLES = {
    "mode": "modeDataDict",
    "mode_group": "modeGroupDataDict",
    "stage": "stageDatasDict",
    "reward": "periodicalRewards",
    "band": "bandDataListDict",
    "bond": "bondInfoDict",
    "unit": "charChessDataDict",
    "unit_shop": "charShopChessDatas",
    "equipment": "trapChessDataDict",
    "equipment_shop": "trapShopChessDatas",
    "effect": "effectInfoDataDict",
    "skill": "skillInfoDataDict",
    "effect_parameters": "effectBuffInfoDataDict",
    "bond_parameters": "effectDescParamDataDict",
    "unit_effect": "garrisonDataDict",
    "choice": "effectChoiceInfoDict",
    "shop_level": "shopLevelDataDict",
    "rounds": "battleDataDict",
    "boss": "bossInfoDict",
    "rule": "constData",
}


def _snapshot(root, filename, key, url, digest, *, media_type, source_type, revision, **metadata):
    raw = (root / filename).read_bytes()
    if sha256(raw).hexdigest() != digest:
        raise IngestionError(f"reviewed garrison source changed: {filename}")
    return SnapshotInput(key, source_type, url, revision, raw, media_type, metadata)


def _season_records(document):
    base = ("activity", "AUTOCHESS", ACTIVITY, PHASE)
    season = json_pointer(document, pointer(*base))
    global_data = document["autoChessData"]
    version = global_data["versionInfoDict"][PHASE]
    activity = document["basicInfo"][ACTIVITY]
    if (version["versionId"], version["activityId"], version["seasonName"], version["sortId"],
        activity["id"], activity["type"], activity["startTime"], version["startTime"]) != (
        PHASE, ACTIVITY, "规程", 1, ACTIVITY, "AUTOCHESS", 1794124800, 1794124800
    ):
        raise IngestionError("reviewed regulation season identity differs")

    def record(kind, ident, name, raw, *path):
        return NormalizedRecord("garrison", f"regulation_{kind}", ident, name,
                                {"source_value": raw}, CLIENT_KEY, pointer(*path), PHASE)

    yield record("season", PHASE, "卫戍协议·规程·序列 I", version, "autoChessData", "versionInfoDict", PHASE)
    yield record("activity", ACTIVITY, "卫戍协议·规程", activity, "basicInfo", ACTIVITY)
    shop = season["seasonShop"]
    yield record("shop", shop["shopId"], "功勋兑换处", shop, *base, "seasonShop")
    for group_id, group in shop["shopGroup"].items():
        for ident, good in group["shopGood"].items():
            yield record("shop_good", ident, good.get("displayName") or good["item"]["id"], good,
                         *base, "seasonShop", "shopGroup", group_id, "shopGood", ident)
    groups = [(index, row) for index, row in enumerate(document["missionGroup"]) if row["id"] == ACTIVITY]
    if len(groups) != 1:
        raise IngestionError("regulation mission group is missing or ambiguous")
    index, group = groups[0]
    yield record("mission_group", ACTIVITY, "规程·序列 I 关键目标", group, "missionGroup", index)
    missions = [(i, row) for i, row in enumerate(document["missionData"]) if row["missionGroup"] == ACTIVITY]
    if sorted(row["id"] for _, row in missions) != sorted(group["missionIds"]):
        raise IngestionError("regulation mission membership differs from the activity group")
    for index, row in missions:
        yield record("mission", row["id"], row["description"], row, "missionData", index)
    for kind, table in SEASON_TABLES.items():
        for ident, row in season[table].items():
            name = ident
            if isinstance(row, dict):
                name = row.get("name") or row.get("effectName") or ident
            if kind == "band":
                name = global_data["bandDataDict"][ident]["bandName"]
            elif kind in {"equipment", "equipment_shop"}:
                unit = season["trapChessDataDict"][ident]
                effect = season["effectInfoDataDict"][unit["effectId"]]
                name = f"{effect['effectName'] or ident}（{'金色' if unit['isGolden'] else '普通'}）"
            elif kind in {"effect_parameters", "bond_parameters", "skill"}:
                name = season["effectInfoDataDict"].get(ident, {}).get("effectName") or ident
            elif kind in {"shop_level", "rounds"}:
                name = season["modeDataDict"].get(ident, {}).get("name") or ident
            yield record(kind, ident, name, row, *base, table, ident)


def build_garrison_regulation_batch(base, source_dir):
    root = Path(source_dir)
    client = _snapshot(
        root, "activity_table.json", CLIENT_KEY, CLIENT_URL, CLIENT_HASH,
        media_type="application/json", source_type="season_client_data", revision=CLIENT_REVISION,
        authority="community_client_data_mirror", activity_id=ACTIVITY, phase_id=PHASE,
        selector_semantics="RFC6901_on_original_JSON",
    )
    records = list(_season_records(snapshot_document(client.content, client.media_type)))
    snapshots = [client]
    for article, (date, digest) in ANNOUNCEMENTS.items():
        source = _snapshot(
            root, f"{article}.html", f"arknights-official:news/{article}",
            f"https://ak.hypergryph.com/news/{article}", digest, media_type="text/html",
            source_type="official_announcement", revision=f"sha256:{digest}",
            authority="official", region="CN", article_id=article, published_date=date,
            document_parser=HTML_DOCUMENT_PARSER,
            selector_semantics="RFC6901_on_parsed_visible_text_projection",
        )
        nodes = snapshot_document(source.content, source.media_type)["html_text_nodes"]
        year, month, day = date.split("-")
        if f"{year} // {month} / {day}" not in nodes:
            raise IngestionError(f"official announcement date differs: {article}")
        snapshots.append(source)
        for ident, (record_article, index, label) in OFFICIAL_RECORDS.items():
            if record_article == article:
                records.append(NormalizedRecord(
                    "garrison", "regulation_notice", ident, f"规程·序列 I {label}",
                    {"source_value": nodes[index]}, source.source_key, pointer("html_text_nodes", index), PHASE,
                ))
    scope = {
        **base.scopes["garrison"],
        "latest_documented_season": {
            "activity_id": ACTIVITY, "phase_id": PHASE, "title": "卫戍协议·规程·序列 I",
            "launch_date": datetime.fromtimestamp(1794124800, TIMEZONE).date().isoformat(),
            "client_revision": CLIENT_REVISION,
        },
    }
    coverage = {
        **base.coverage["garrison"],
        "regulation_sequence_1": {
            "phase_id": PHASE,
            "record_counts": dict(sorted(Counter(record.kind for record in records).items())),
            "source_scope": "official_launch_and_client_configuration",
            "runtime_mechanics_verified": False,
        },
    }
    # A retry on an active release that already includes this source must not
    # accumulate duplicate season rows or change the historical extraction.
    existing = {source.source_key: source for source in base.snapshots}
    added = {source.source_key: source for source in snapshots}
    if existing.keys() & added.keys():
        if (all(key in existing and existing[key].content == source.content for key, source in added.items())
                and base.scopes["garrison"] == scope and base.coverage["garrison"] == coverage
                and sorted((row for row in base.records if row.source_key in added),
                           key=lambda row: (row.kind, row.native_id))
                == sorted(records, key=lambda row: (row.kind, row.native_id))):
            return base
        raise IngestionError("existing regulation source or extraction differs")
    return SampleBatch(
        base.revision, [*base.snapshots, *snapshots], [*base.records, *records],
        {**base.scopes, "garrison": scope}, {**base.coverage, "garrison": coverage},
    )
