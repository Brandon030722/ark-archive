"""Fixed CN Integrated Strategies client snapshots, with exact source selectors.

The source revision identifies retained client configuration. It does not prove
current availability, historical applicability, ending routes or engine behavior.
Every record preserves one complete raw object; linked item/buff objects remain
separate evidence records rather than a synthetic, unquotable joined object.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from arknights_agent.ingestion.contracts import NormalizedRecord, SampleBatch, SnapshotInput
from arknights_agent.ingestion.sample import DEFAULT_COMMIT, REPO, SOURCE_PATH, IngestionError
from arknights_agent.ingestion.sample import pointer as source_pointer
from arknights_agent.storage import SnapshotStore

PARSER_VERSION = "cn-roguelike-fixed-source-v2"
LEGACY_THEME = "act12d6"
TOPIC_FILE = "roguelike_topic_table.json"
LEGACY_FILE = "roguelike_table.json"
ACTIVITY_FILE = "activity_table.json"
THEME_NAMES = {
    LEGACY_THEME: "刻俄柏的灰蕈迷境",
    "rogue_1": "傀影与猩红孤钻",
    "rogue_2": "水月与深蓝之树",
    "rogue_3": "探索者的银凇止境",
    "rogue_4": "萨卡兹的无终奇语",
    "rogue_5": "岁的界园志异",
    "rogue_6": "沉沦者的黑流树海",
}
# The bytes were fetched from these exact commit URLs. Mutable branches and a
# caller supplied hash never authorize a source; future revisions need review.
SOURCE_PINS = {
    DEFAULT_COMMIT: {
        TOPIC_FILE: {
            "sha256": "36d8189e1e466fa07226effbf2cf224a69fb41a942ae83827934052e7a01cc69",
            "byte_size": 17958101,
        },
        LEGACY_FILE: {
            "sha256": "95e15b22c5efb46c5c80e29286072a8e9d93e058ddaeee9819d5b6dc20278533",
            "byte_size": 382157,
        },
        ACTIVITY_FILE: {
            "sha256": "10f9b70a5e69a86dfa845e761ee49232c7d17755f469202b080c59bef5feb304",
            "byte_size": 14126572,
        },
    }
}
DICT_SECTIONS = {
    "endings": "ending",
    "failEndings": "ending",
    "nodeTypeData": "node",
    "stages": "stage",
    "zones": "zone",
    "choiceScenes": "event",
    "choices": "choice",
    "monthSquad": "month_squad",
    "challenges": "challenge",
    "recruitGrps": "recruit_group",
    "recruitTickets": "ticket",
    "upgradeTickets": "ticket",
    "customTickets": "ticket",
    "stashableTickets": "ticket",
    "variationData": "rule",
    "charBuffData": "rule",
    "squadBuffData": "rule",
    "exploreTools": "rule",
}
LIST_SECTIONS = {"difficulties": "difficulty", "init": "initial"}
RULE_SECTIONS = ("gameConst", "detailConst", "archiveUnlockCond", "styleConfig")
NAME_FIELDS = (
    "name", "outerName", "innerName", "title", "teamName", "challengeName", "modeId",
)


def _pins(revision: str) -> dict:
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise IngestionError("roguelike revision must be a full lowercase Git SHA")
    if revision not in SOURCE_PINS:
        raise IngestionError("roguelike revision has no reviewed source pins")
    return SOURCE_PINS[revision]


def _object(value: Any, location: str) -> dict:
    if not isinstance(value, dict):
        raise IngestionError(f"{location}: expected object")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise IngestionError("roguelike source contains duplicate object keys")
        result[key] = value
    return result


def _reject_constant(value):
    raise IngestionError(f"roguelike source contains nonstandard number: {value}")


def _document(filename: str, raw: bytes, pin: dict) -> dict:
    if (
        not isinstance(raw, bytes)
        or len(raw) != pin["byte_size"]
        or hashlib.sha256(raw).hexdigest() != pin["sha256"]
    ):
        raise IngestionError(f"fixed roguelike original bytes differ: {filename}")
    try:
        return _object(
            json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant),
            filename,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise IngestionError(f"invalid fixed roguelike JSON: {filename}") from exc


def _source_key(filename: str) -> str:
    return f"arknights-game-data:{SOURCE_PATH}/{filename}"


def _name(row: dict, fallback: str) -> str:
    for field in NAME_FIELDS:
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return fallback  # Source key, not an invented display name.


def _named_objects(value, path):
    """Keep the complete highest named object, including all its child fields."""
    if isinstance(value, dict):
        if any(isinstance(value.get(field), str) and value[field] for field in NAME_FIELDS):
            yield path, value
        else:
            for key, child in sorted(value.items()):
                yield from _named_objects(child, [*path, key])
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _named_objects(child, [*path, index])


def parse_roguelike(revision: str, raw_files: dict[str, bytes]) -> SampleBatch:
    """Parse all seven retained themes; fail an unsupported or incomplete input."""
    pins = _pins(revision)
    if set(raw_files) != set(pins):
        raise IngestionError("roguelike input must contain exactly the reviewed source files")
    documents = {name: _document(name, raw_files[name], pin) for name, pin in pins.items()}
    topic = documents[TOPIC_FILE]
    topics = _object(topic.get("topics"), "/topics")
    details = _object(topic.get("details"), "/details")
    modules = _object(topic.get("modules"), "/modules")
    custom = _object(topic.get("customizeData"), "/customizeData")
    expected = set(THEME_NAMES) - {LEGACY_THEME}
    if any(set(table) != expected for table in (topics, details, modules, custom)):
        raise IngestionError("fixed roguelike source theme identity/coverage differs")
    legacy = documents[LEGACY_FILE]
    legacy_topic = _object(
        _object(documents[ACTIVITY_FILE].get("basicInfo"), "/basicInfo").get(LEGACY_THEME),
        f"/basicInfo/{LEGACY_THEME}",
    )
    if (
        legacy_topic.get("id") != LEGACY_THEME
        or legacy_topic.get("name") != THEME_NAMES[LEGACY_THEME]
        or legacy_topic.get("type") != "ROGUELIKE"
    ):
        raise IngestionError("retained legacy roguelike activity identity differs")
    snapshots = [
        SnapshotInput(
            _source_key(name), "community_client_data",
            f"https://raw.githubusercontent.com/{REPO}/{revision}/{SOURCE_PATH}/{name}",
            revision, raw_files[name], "application/json",
            {
                "repository": REPO, "path": f"{SOURCE_PATH}/{name}", "region": "CN",
                "parser_version": PARSER_VERSION, "authority": "community_parsed_client",
                "effective_date_verified": False, "runtime_mechanics_verified": False,
                "source_pin": pins[name],
            },
        )
        for name in pins
    ]
    records: list[NormalizedRecord] = []
    counts = {}

    def add(kind, theme, filename, path, raw, *, theme_record=False, name=None):
        raw = _object(raw, source_pointer(*path))
        native = theme if theme_record else theme + ":" + source_pointer(*path)
        records.append(NormalizedRecord(
            "roguelike", f"roguelike_{kind}", native,
            name if name is not None else _name(raw, str(path[-1])), raw,
            _source_key(filename), source_pointer(*path), None if theme_record else theme,
        ))

    def table_rows(theme, filename, base, section, values, kind):
        values = _object(values, source_pointer(*base, section))
        for key, raw in sorted(values.items()):
            raw = _object(raw, source_pointer(*base, section, key))
            if "id" in raw and raw["id"] != key:
                raise IngestionError(f"roguelike key/id mismatch: {theme}/{section}/{key}")
            add(kind, theme, filename, [*base, section, key], raw)

    def items_and_buffs(theme, filename, base, item_values, buff_values, legacy_items=False):
        items = _object(item_values, source_pointer(*base, "items"))
        buffs = _object(buff_values, source_pointer(*base, "relics"))
        if not items or not buffs or not set(buffs).issubset(items):
            raise IngestionError("roguelike relic/item reference coverage differs")
        for key, raw in sorted(items.items()):
            raw = _object(raw, source_pointer(*base, "items", key))
            if raw.get("id") != key or not isinstance(raw.get("name"), str):
                raise IngestionError("roguelike item identity/name differs")
            if not isinstance(raw.get("type"), str):
                raise IngestionError("roguelike item type is absent")
            kind = "item"
            if raw["type"] == "BAND" or (
                legacy_items and raw["type"] == "RELIC" and raw.get("rarity") == "BORN"
            ):
                kind = "squad"
            elif raw["type"] == "RELIC":
                kind = "relic"
            add(kind, theme, filename, [*base, "items", key], raw)
        for key, raw in sorted(buffs.items()):
            raw = _object(raw, source_pointer(*base, "relics", key))
            if raw.get("id") != key or not isinstance(raw.get("buffs"), list):
                raise IngestionError("roguelike buff identity/list differs")
            add("buff", theme, filename, [*base, "relics", key], raw, name=items[key]["name"])

    for theme in sorted(expected):
        before = len(records)
        raw_theme = _object(topics[theme], f"/topics/{theme}")
        if raw_theme.get("id") != theme or raw_theme.get("name") != THEME_NAMES[theme]:
            raise IngestionError("roguelike theme name/id differs")
        add("theme", theme, TOPIC_FILE, ["topics", theme], raw_theme, theme_record=True)
        detail = _object(details[theme], f"/details/{theme}")
        base = ["details", theme]
        items_and_buffs(theme, TOPIC_FILE, base, detail.get("items"), detail.get("relics"))
        for section, kind in DICT_SECTIONS.items():
            table_rows(theme, TOPIC_FILE, base, section, detail.get(section), kind)
        for section, kind in LIST_SECTIONS.items():
            values = detail.get(section)
            if not isinstance(values, list) or not values:
                raise IngestionError(f"roguelike {theme}/{section}: expected nonempty list")
            for index, raw in enumerate(values):
                add(kind, theme, TOPIC_FILE, [*base, section, index], raw)
        for section in RULE_SECTIONS:
            add("rule", theme, TOPIC_FILE, [*base, section], detail.get(section))
        for section in ("modules", "customizeData"):
            source = _object(topic[section][theme], f"/{section}/{theme}")
            for path, raw in _named_objects(source, [section, theme]):
                add("rule", theme, TOPIC_FILE, path, raw)
        counts[theme] = dict(Counter(record.kind for record in records[before:]))

    before = len(records)
    add("theme", LEGACY_THEME, ACTIVITY_FILE, ["basicInfo", LEGACY_THEME], legacy_topic,
        theme_record=True)
    item_table = _object(legacy.get("itemTable"), "/itemTable")
    items_and_buffs(LEGACY_THEME, LEGACY_FILE, ["itemTable"], item_table.get("items"),
                    item_table.get("relics"), legacy_items=True)
    for section, kind in {
        "endings": "ending", "stages": "stage", "zones": "zone", "choices": "choice",
        "choiceScenes": "event", "modes": "difficulty",
    }.items():
        table_rows(LEGACY_THEME, LEGACY_FILE, [], section, legacy.get(section), kind)
    const = _object(legacy.get("constTable"), "/constTable")
    table_rows(LEGACY_THEME, LEGACY_FILE, ["constTable"], "eventTypeTable",
               const.get("eventTypeTable"), "node")
    add("rule", LEGACY_THEME, LEGACY_FILE, ["constTable"], const)
    for path, raw in _named_objects(legacy.get("outBuffs"), ["outBuffs"]):
        add("rule", LEGACY_THEME, LEGACY_FILE, path, raw)
    counts[LEGACY_THEME] = dict(Counter(record.kind for record in records[before:]))
    scope = {
        "region": "CN", "mode": "fixed_client_config", "theme_ids": list(THEME_NAMES),
        "source_revision": revision, "effective_date_verified": False,
        "runtime_mechanics_verified": False, "historical_availability_verified": False,
        "latest_availability_verified": False,
    }
    coverage = {
        "partial": True, "themes": counts, "theme_count": len(THEME_NAMES),
        "normalized_sections": {
            "dict": DICT_SECTIONS, "list": LIST_SECTIONS, "rule": list(RULE_SECTIONS),
            "item_and_buff": ["items", "relics"], "named_rule_objects": ["modules", "customizeData"],
        },
        "original_snapshot_complete": True, "all_client_fields_normalized": False,
        "battle_engine_implemented": False, "ending_unlock_routes_verified": False,
        "source_pins": pins,
    }
    return SampleBatch(revision, snapshots, records, {"roguelike": scope}, {"roguelike": coverage})


def collect_roguelike(
    revision: str = DEFAULT_COMMIT, *,
    cache_dir: str | Path = ".local/stage15-roguelike-source-cache",
    transport: httpx.BaseTransport | None = None, offline: bool = False,
) -> SampleBatch:
    """Fetch only reviewed commit URLs, storing and rechecking original bytes."""
    pins = _pins(revision)
    store = SnapshotStore(cache_dir)
    sources = {}
    with httpx.Client(transport=transport, timeout=60, follow_redirects=False) as client:
        for filename, pin in pins.items():
            if store.path_for(pin["sha256"]).exists():
                raw = store.read_verified(pin["sha256"])
            else:
                if offline:
                    raise IngestionError(f"fixed roguelike cached source is unavailable: {filename}")
                url = f"https://raw.githubusercontent.com/{REPO}/{revision}/{SOURCE_PATH}/{filename}"
                try:
                    response = client.get(url)
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    raise IngestionError(f"fixed roguelike source fetch failed: {filename}") from exc
                raw = response.content
                _document(filename, raw, pin)
                if store.put(raw) != pin["sha256"]:
                    raise IngestionError("roguelike cached source identity differs")
            _document(filename, raw, pin)
            sources[filename] = raw
    return parse_roguelike(revision, sources)
