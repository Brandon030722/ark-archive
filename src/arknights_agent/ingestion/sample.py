"""Small real-data slice of the community parsed CN client tables.

This parser stores raw descriptions and blackboards without claiming to implement
the game engine. A commit is a source revision, not a verified game effective date.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from .contracts import NormalizedRecord, SampleBatch, SnapshotInput

DEFAULT_COMMIT = "17759cc8cdf37388414145d16262a3a06f3ab25e"
PREVIOUS_COMMIT = "a550f5e048bb94e7cdefc6eb97a4091f0c4c7add"
DEFAULT_OPERATORS = ("char_010_chen", "char_202_demkni")
RECRUIT_SAMPLE_NAMES = (
    "Lancet-2",
    "Castle-3",
    "克洛丝",
    "桃金娘",
    "因陀罗",
    "火神",
    "陈",
    "塞雷娅",
)
REPO = "Kengxxiao/ArknightsGameData"
SOURCE_PATH = "zh_CN/gamedata/excel"
PARSER_VERSION = "cn-sample-v1"
FILES = (
    "character_table.json",
    "skill_table.json",
    "uniequip_table.json",
    "battle_equip_table.json",
    "gacha_table.json",
    "activity_table.json",
)
ACTIVITY_ID = "act2autochess"
SEASON_POINTER = f"/activity/AUTOCHESS_SEASON/{ACTIVITY_ID}"


class IngestionError(ValueError):
    """Unsupported/missing source shape: abort the batch, never silently omit it."""


def source_key(filename: str) -> str:
    return f"arknights-game-data:{SOURCE_PATH}/{filename}"


def pointer(*parts: Any) -> str:
    """RFC 6901 JSON Pointer, including keys containing '~' or '/'."""
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _commit(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise IngestionError("commit must be a full lowercase 40-character Git SHA")


def _field(obj: Any, key: str, expected: type, location: str) -> Any:
    if (
        not isinstance(obj, dict)
        or key not in obj
        or not isinstance(obj[key], expected)
        or (expected is int and isinstance(obj[key], bool))
    ):
        raise IngestionError(f"{location}/{key}: expected {expected.__name__}")
    return obj[key]


def _number(obj: dict, key: str, location: str) -> None:
    value = obj.get(key)
    if not isinstance(value, (float, int)) or isinstance(value, bool):
        raise IngestionError(f"{location}/{key}: expected number")


def _blackboard(values: list, location: str) -> None:
    for i, item in enumerate(values):
        loc = f"{location}/{i}"
        _field(item, "key", str, loc)
        _number(item, "value", loc)


def _reject_constant(value: str) -> None:
    raise IngestionError(f"nonstandard JSON numeric constant: {value}")


def _nonempty(value: list | dict, location: str) -> list | dict:
    if not value:
        raise IngestionError(f"{location}: unexpectedly empty")
    return value


def _record(
    module: str,
    kind: str,
    ident: str,
    name: str,
    raw: dict,
    filename: str,
    path: str,
    parent: str | None = None,
) -> NormalizedRecord:
    return NormalizedRecord(module, kind, ident, name, raw, source_key(filename), path, parent)


def _recruit_members(detail: str, characters: dict) -> dict[str, tuple[str, int]]:
    """Decode the explicit recruitDetail membership text; don't use the gacha pool.

    Known client grammar uses a literal '\\n' after each star header and actual
    CRLF between sections. HTML-like game rich-text tags are removed for exact
    name matching. Any unrecognized name/section is a hard schema error.
    """
    name_to_ids: dict[str, list[str]] = {}
    for ident, char in characters.items():
        # The table also contains traps/tokens named after operators. Only native
        # char_ entries can be public-recruitment operators; never link a trap.
        if (
            ident.startswith("char_")
            and isinstance(char, dict)
            and isinstance(char.get("name"), str)
        ):
            name_to_ids.setdefault(char["name"], []).append(ident)
    plain = re.sub(r"<[^>]*>", "", detail).replace("\\n", "\n")
    sections = re.findall(r"(?m)^(★{1,6})\n([^★]+?)(?=\n-+|\Z)", plain)
    if [len(stars) for stars, _ in sections] != [1, 2, 3, 4, 5, 6]:
        raise IngestionError("gacha_table.json/recruitDetail: unsupported six-star-section grammar")
    members: dict[str, tuple[str, int]] = {}
    for stars, body in sections:
        for name in body.strip().split(" / "):
            ids = name_to_ids.get(name, [])
            if len(ids) != 1:
                raise IngestionError(
                    f"recruitDetail name {name!r}: expected one exact character match"
                )
            ident = ids[0]
            if ident in members:
                raise IngestionError(f"recruitDetail: duplicate member {ident}")
            rarity = _field(characters[ident], "rarity", str, pointer(ident))
            if rarity != f"TIER_{len(stars)}":
                raise IngestionError(f"recruitDetail: rarity differs for {name}")
            members[ident] = (name, len(stars))
    return members


def parse_sample(
    commit: str,
    raw_files: dict[str, bytes],
    operator_ids: list[str] | None = None,
    *,
    all_operators: bool = False,
) -> SampleBatch:
    """Parse a deliberately partial slice, retaining complete original snapshots."""
    _commit(commit)
    selected = list(DEFAULT_OPERATORS if operator_ids is None else operator_ids)
    if not selected or len(selected) != len(set(selected)):
        raise IngestionError("operator_ids must be nonempty and unique")
    tables = {}
    snapshots = []
    for filename in FILES:
        if filename not in raw_files or not isinstance(raw_files[filename], bytes):
            raise IngestionError(f"missing original bytes: {filename}")
        content = raw_files[filename]
        try:
            obj = json.loads(content, parse_constant=_reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IngestionError(f"invalid JSON: {filename}") from exc
        if not isinstance(obj, dict):
            raise IngestionError(f"{filename}: expected JSON object")
        tables[filename] = obj
        snapshots.append(
            SnapshotInput(
                source_key(filename),
                "community_client_data",
                f"https://raw.githubusercontent.com/{REPO}/{commit}/{SOURCE_PATH}/{filename}",
                commit,
                content,
                "application/json",
                {
                    "repository": REPO,
                    "path": f"{SOURCE_PATH}/{filename}",
                    "region": "CN",
                    "parser_version": PARSER_VERSION,
                    "authority": "community_parsed_client",
                    "effective_date_verified": False,
                },
            )
        )

    chars = tables["character_table.json"]
    if all_operators:
        if operator_ids is not None:
            raise IngestionError("choose all_operators or operator_ids")
        selected = sorted(
            ident
            for ident, char in chars.items()
            if ident.startswith("char_") and char.get("isNotObtainable") is False
        )
        _nonempty(selected, "obtainable operators")
    skills = tables["skill_table.json"]
    equips = tables["uniequip_table.json"]
    battle_equips = tables["battle_equip_table.json"]
    char_equip = _field(equips, "charEquip", dict, "uniequip_table.json")
    equip_dict = _field(equips, "equipDict", dict, "uniequip_table.json")
    records = []
    for ident in selected:
        char = _field(chars, ident, dict, "character_table.json")
        name = _field(char, "name", str, pointer(ident))
        for key, typ in {
            "rarity": str,
            "profession": str,
            "position": str,
            "phases": list,
            "skills": list,
            "talents": list,
            "tagList": list,
        }.items():
            _field(char, key, typ, pointer(ident))
        records.append(
            _record(
                "operator", "operator", ident, name, char, "character_table.json", pointer(ident)
            )
        )
        for i, ref in enumerate(char["skills"]):
            skill_id = _field(ref, "skillId", str, pointer(ident, "skills", i))
            skill = _field(skills, skill_id, dict, "skill_table.json")
            if _field(skill, "skillId", str, pointer(skill_id)) != skill_id:
                raise IngestionError(f"skill_table.json/{skill_id}: mismatching skillId")
            levels = _nonempty(
                _field(skill, "levels", list, pointer(skill_id)), pointer(skill_id, "levels")
            )
            for rank, level in enumerate(levels):
                loc = pointer(skill_id, "levels", rank)
                for key, typ in {
                    "name": str,
                    "description": str,
                    "blackboard": list,
                    "spData": dict,
                    "skillType": str,
                }.items():
                    _field(level, key, typ, loc)
                _number(level, "duration", loc)
                _blackboard(level["blackboard"], f"{loc}/blackboard")
                sp = level["spData"]
                if (
                    "spType" not in sp
                    or isinstance(sp["spType"], bool)
                    or not isinstance(sp["spType"], (str, int, type(None)))
                ):
                    raise IngestionError(
                        f"{loc}/spData/spType: expected enum string, integer or null"
                    )
                for key in ("spCost", "initSp", "maxChargeTime", "increment"):
                    _number(sp, key, f"{loc}/spData")
            records.append(
                _record(
                    "operator",
                    "skill",
                    skill_id,
                    levels[0]["name"],
                    skill,
                    "skill_table.json",
                    pointer(skill_id),
                    ident,
                )
            )
        for i, talent in enumerate(char["talents"]):
            if talent.get("candidates") is None:
                # A client placeholder, retained in the complete operator payload.
                continue
            candidates = _field(talent, "candidates", list, pointer(ident, "talents", i))
            for j, candidate in enumerate(candidates):
                if all_operators and candidate.get("isHideTalent") is True:
                    continue
                loc = pointer(ident, "talents", i, "candidates", j)
                for key, typ in {
                    "unlockCondition": dict,
                    "requiredPotentialRank": int,
                    "blackboard": list,
                }.items():
                    _field(candidate, key, typ, loc)
                for key in ("name", "description"):
                    if key not in candidate or (
                        candidate[key] is not None and not isinstance(candidate[key], str)
                    ):
                        raise IngestionError(f"{loc}/{key}: expected string or null")
                _blackboard(candidate["blackboard"], f"{loc}/blackboard")
                records.append(
                    _record(
                        "operator",
                        "talent",
                        f"{ident}:talent:{i}:candidate:{j}",
                        candidate["name"] or f"{name}天赋{i + 1}候选{j + 1}（来源未命名）",
                        candidate,
                        "character_table.json",
                        loc,
                        ident,
                    )
                )
        # Operators without released modules have no charEquip entry.
        bindings = char_equip.get(ident, [])
        if not isinstance(bindings, list):
            raise IngestionError(f"/charEquip/{ident}: expected list")
        for equip_id in bindings:
            equip = _field(equip_dict, equip_id, dict, "/equipDict")
            equip_type = _field(equip, "type", str, pointer("equipDict", equip_id))
            if equip_type == "INITIAL":
                continue  # Class badges are not battle modules.
            equip_name = _field(equip, "uniEquipName", str, pointer("equipDict", equip_id))
            if _field(equip, "charId", str, pointer("equipDict", equip_id)) != ident:
                raise IngestionError(f"{equip_id}: charEquip / charId mismatch")
            # Store unlock/type metadata; omit the narrative story from this mechanism slice.
            payload = {k: v for k, v in equip.items() if k != "uniEquipDesc"}
            records.append(
                _record(
                    "operator",
                    "module",
                    equip_id,
                    equip_name,
                    payload,
                    "uniequip_table.json",
                    pointer("equipDict", equip_id),
                    ident,
                )
            )
            bundle = _field(battle_equips, equip_id, dict, "battle_equip_table.json")
            phases = _nonempty(
                _field(bundle, "phases", list, pointer(equip_id)), pointer(equip_id, "phases")
            )
            seen_levels = set()
            for i, phase in enumerate(phases):
                loc = pointer(equip_id, "phases", i)
                level = _field(phase, "equipLevel", int, loc)
                for key in ("parts", "attributeBlackboard"):
                    _field(phase, key, list, loc)
                _blackboard(phase["attributeBlackboard"], f"{loc}/attributeBlackboard")
                if level in seen_levels:
                    raise IngestionError(f"{equip_id}: duplicate equipLevel {level}")
                seen_levels.add(level)
                records.append(
                    _record(
                        "operator",
                        "module_phase",
                        f"{equip_id}:level:{level}",
                        f"{equip_name} Lv.{level}",
                        phase,
                        "battle_equip_table.json",
                        loc,
                        equip_id,
                    )
                )

    gacha = tables["gacha_table.json"]
    detail = _field(gacha, "recruitDetail", str, "gacha_table.json")
    all_members = _recruit_members(detail, chars)
    recruit_sample = []
    for wanted in RECRUIT_SAMPLE_NAMES:
        found = [(ident, stars) for ident, (name, stars) in all_members.items() if name == wanted]
        if len(found) != 1:
            raise IngestionError(f"required recruitment sample is absent/ambiguous: {wanted}")
        ident, stars = found[0]
        recruit_sample.append(ident)
        records.append(
            _record(
                "recruitment",
                "recruitment_member",
                ident,
                wanted,
                {
                    "name": wanted,
                    "rarity_stars": stars,
                    "membership_basis": "recruitDetail",
                    "coverage": "partial",
                    "character_ref": {
                        "source_key": source_key("character_table.json"),
                        "pointer": pointer(ident),
                    },
                },
                "gacha_table.json",
                "/recruitDetail",
            )
        )
    tags = _nonempty(_field(gacha, "gachaTags", list, "gacha_table.json"), "/gachaTags")
    for i, tag in enumerate(tags):
        tag_id = _field(tag, "tagId", int, pointer("gachaTags", i))
        name = _field(tag, "tagName", str, pointer("gachaTags", i))
        records.append(
            _record(
                "recruitment",
                "recruitment_tag",
                str(tag_id),
                name,
                tag,
                "gacha_table.json",
                pointer("gachaTags", i),
            )
        )
    for key in ("recruitPool", "recruitRarityTable", "specialTagRarityTable"):
        rules = _nonempty(_field(gacha, key, dict, "gacha_table.json"), pointer(key))
        records.append(
            _record(
                "recruitment",
                "recruitment_rules",
                key,
                key,
                rules,
                "gacha_table.json",
                pointer(key),
            )
        )
    records.append(
        _record(
            "recruitment",
            "recruitment_rules",
            "recruitDetail",
            "公开招募原始说明",
            {"description_raw": detail},
            "gacha_table.json",
            "/recruitDetail",
        )
    )

    activity = tables["activity_table.json"]
    basic = _field(
        _field(activity, "basicInfo", dict, "activity_table.json"), ACTIVITY_ID, dict, "/basicInfo"
    )
    act = _field(activity, "activity", dict, "activity_table.json")
    seasons = _field(act, "AUTOCHESS_SEASON", dict, "/activity")
    season = _field(seasons, ACTIVITY_ID, dict, "/activity/AUTOCHESS_SEASON")
    name = _field(basic, "name", str, pointer("basicInfo", ACTIVITY_ID))
    records.append(
        _record(
            "garrison",
            "season",
            ACTIVITY_ID,
            name,
            basic,
            "activity_table.json",
            pointer("basicInfo", ACTIVITY_ID),
        )
    )
    all_versions = _field(
        _field(activity, "autoChessData", dict, "activity_table.json"),
        "versionInfoDict",
        dict,
        "/autoChessData",
    )
    versions = {k: v for k, v in all_versions.items() if v.get("activityId") == ACTIVITY_ID}
    _nonempty(versions, "/autoChessData/versionInfoDict for act2autochess")
    for key, data in versions.items():
        records.append(
            _record(
                "garrison",
                "version_mapping",
                key,
                key,
                data,
                "activity_table.json",
                pointer("autoChessData", "versionInfoDict", key),
                ACTIVITY_ID,
            )
        )
    modes = _nonempty(
        _field(season, "modeDataDict", dict, SEASON_POINTER), f"{SEASON_POINTER}/modeDataDict"
    )
    for key, data in modes.items():
        mode_name = _field(data, "name", str, f"{SEASON_POINTER}/modeDataDict/{key}")
        records.append(
            _record(
                "garrison",
                "mode",
                key,
                mode_name,
                data,
                "activity_table.json",
                f"{SEASON_POINTER}{pointer('modeDataDict', key)}",
                ACTIVITY_ID,
            )
        )
    shops = _field(season, "charShopChessDatas", dict, SEASON_POINTER)
    chess = _field(season, "charChessDataDict", dict, SEASON_POINTER)
    # Two named preset pieces and the actual Saria shop piece; not a made-up Chen fallback.
    chess_ids = ["chess_char_1_01_a", "chess_char_1_02_a"]
    chess_ids += sorted(k for k, v in shops.items() if v.get("charId") == "char_202_demkni")
    if len(chess_ids) != 3:
        raise IngestionError("expected exactly one real Saria shop chess record")
    for key in chess_ids:
        shop = _field(shops, key, dict, f"{SEASON_POINTER}/charShopChessDatas")
        char_id = _field(shop, "charId", str, f"{SEASON_POINTER}/charShopChessDatas/{key}")
        char = _field(chars, char_id, dict, "character_table.json")
        piece_name = _field(char, "name", str, pointer(char_id))
        records.append(
            _record(
                "garrison",
                "shop_chess",
                key,
                piece_name,
                shop,
                "activity_table.json",
                f"{SEASON_POINTER}{pointer('charShopChessDatas', key)}",
                ACTIVITY_ID,
            )
        )
        ids = [
            key,
            _field(shop, "goldenChessId", str, f"{SEASON_POINTER}/charShopChessDatas/{key}"),
        ]
        for chess_id in ids:
            data = _field(chess, chess_id, dict, f"{SEASON_POINTER}/charChessDataDict")
            for field, typ in {"status": dict, "bondIds": list, "isGolden": bool}.items():
                _field(data, field, typ, f"{SEASON_POINTER}/charChessDataDict/{chess_id}")
            records.append(
                _record(
                    "garrison",
                    "chess",
                    chess_id,
                    piece_name,
                    data,
                    "activity_table.json",
                    f"{SEASON_POINTER}{pointer('charChessDataDict', chess_id)}",
                    ACTIVITY_ID,
                )
            )
    bonds = _field(season, "bondInfoDict", dict, SEASON_POINTER)
    for key in ("yanShip", "lateranoShip", "victoriaShip"):
        data = _field(bonds, key, dict, f"{SEASON_POINTER}/bondInfoDict")
        bond_name = _field(data, "name", str, f"{SEASON_POINTER}/bondInfoDict/{key}")
        _field(data, "desc", str, f"{SEASON_POINTER}/bondInfoDict/{key}")
        records.append(
            _record(
                "garrison",
                "bond",
                key,
                bond_name,
                data,
                "activity_table.json",
                f"{SEASON_POINTER}{pointer('bondInfoDict', key)}",
                ACTIVITY_ID,
            )
        )
    trap_shop = _field(season, "trapShopChessDatas", dict, SEASON_POINTER)
    trap_chess = _field(season, "trapChessDataDict", dict, SEASON_POINTER)
    effects = _field(season, "effectInfoDataDict", dict, SEASON_POINTER)
    effect_buffs = _field(season, "effectBuffInfoDataDict", dict, SEASON_POINTER)
    for key in ("chess_item_1_01_e_a", "chess_item_1_02_e_a"):
        shop = _field(trap_shop, key, dict, f"{SEASON_POINTER}/trapShopChessDatas")
        for item_id in (
            key,
            _field(shop, "goldenItemId", str, f"{SEASON_POINTER}/trapShopChessDatas/{key}"),
        ):
            data = _field(trap_chess, item_id, dict, f"{SEASON_POINTER}/trapChessDataDict")
            effect_id = _field(
                data, "effectId", str, f"{SEASON_POINTER}/trapChessDataDict/{item_id}"
            )
            effect = _field(effects, effect_id, dict, f"{SEASON_POINTER}/effectInfoDataDict")
            effect_name = _field(
                effect, "effectName", str, f"{SEASON_POINTER}/effectInfoDataDict/{effect_id}"
            )
            records.append(
                _record(
                    "garrison",
                    "equipment",
                    item_id,
                    effect_name,
                    data,
                    "activity_table.json",
                    f"{SEASON_POINTER}{pointer('trapChessDataDict', item_id)}",
                    ACTIVITY_ID,
                )
            )
            records.append(
                _record(
                    "garrison",
                    "effect",
                    effect_id,
                    effect_name,
                    effect,
                    "activity_table.json",
                    f"{SEASON_POINTER}{pointer('effectInfoDataDict', effect_id)}",
                    ACTIVITY_ID,
                )
            )
            buffs = _field(
                effect_buffs, effect_id, list, f"{SEASON_POINTER}/effectBuffInfoDataDict"
            )
            records.append(
                _record(
                    "garrison",
                    "effect_parameters",
                    effect_id,
                    effect_name,
                    {"effects": buffs},
                    "activity_table.json",
                    f"{SEASON_POINTER}{pointer('effectBuffInfoDataDict', effect_id)}",
                    ACTIVITY_ID,
                )
            )
    for key in ("constData", "shopLevelDisplayDataDict"):
        data = _nonempty(_field(season, key, dict, SEASON_POINTER), f"{SEASON_POINTER}/{key}")
        records.append(
            _record(
                "garrison",
                "rules",
                key,
                key,
                data,
                "activity_table.json",
                f"{SEASON_POINTER}{pointer(key)}",
                ACTIVITY_ID,
            )
        )

    # Generic skills are shared. The operator's own skillId proves slot ownership.
    unique_records = {}
    for row in records:
        key = (row.module, row.kind, row.native_id)
        previous = unique_records.get(key)
        if previous is not None and row.kind == "skill" and row.payload == previous.payload:
            unique_records[key] = replace(previous, parent_native_id=None)
        elif previous is not None:
            raise IngestionError("duplicate normalized record key")
        else:
            unique_records[key] = row
    records = list(unique_records.values())
    keys = [(r.module, r.kind, r.native_id) for r in records]
    if len(keys) != len(set(keys)):
        raise IngestionError("duplicate normalized record key")
    shared_scope = {
        "game": "arknights",
        "region": "CN",
        "source_revision": commit,
        "effective_at": None,
        "mapping_status": "unverified",
    }
    scopes = {
        "operator": {**shared_scope, "mode": "standard_combat"},
        "recruitment": {**shared_scope, "mode": "public_recruitment"},
        "garrison": {
            **shared_scope,
            "mode": "autochess",
            "activity_id": ACTIVITY_ID,
            "mode_ids": sorted(modes),
            "version_keys": sorted(versions),
        },
    }
    coverage = {
        "operator": {
            "status": "partial",
            "operator_ids": selected,
            "mechanics_verified": False,
            "description_templates_rendered": False,
            **(
                {
                    "status": "source_complete",
                    "operator_count": len(selected),
                    "selection_basis": "char_ and isNotObtainable=false",
                }
                if all_operators
                else {}
            ),
        },
        "recruitment": {
            "status": "partial",
            "member_ids": recruit_sample,
            "sample_members": len(recruit_sample),
            "source_listed_members": len(all_members),
            "tag_catalog_scope": "raw_gachaTags_not_verified_selectable_recruitment_tags",
            "tag_retention_verified": False,
            "calculator_enabled": False,
        },
        "garrison": {
            "status": "partial",
            "activity_id": ACTIVITY_ID,
            "shop_chess_ids": chess_ids,
            "bond_ids": ["yanShip", "lateranoShip", "victoriaShip"],
            "equipment_ids": ["chess_item_1_01_e_a", "chess_item_1_02_e_a"],
            "rules_engine_enabled": False,
            "mapping_status": "unverified",
        },
    }
    return SampleBatch(commit, snapshots, records, scopes, coverage)


def fetch_sample(
    commit: str = DEFAULT_COMMIT,
    operator_ids: list[str] | None = None,
    *,
    cache_dir: str | Path | None = ".local/ingestion-cache",
    all_operators: bool = False,
) -> SampleBatch:
    """Fetch only fixed repository paths at an immutable commit, never arbitrary URLs.

    Optional raw-file cache is outside version-controlled project sources. Cache
    and HTTP results pass the same strict parser; HTTP redirects are disallowed.
    """
    _commit(commit)
    cache = Path(cache_dir) / commit if cache_dir is not None else None
    if cache is not None:
        cache.mkdir(parents=True, exist_ok=True)

    def fetch(filename: str) -> tuple[str, bytes]:
        path = cache / filename if cache is not None else None
        if path is not None and path.exists():
            return filename, path.read_bytes()
        url = f"https://raw.githubusercontent.com/{REPO}/{commit}/{SOURCE_PATH}/{filename}"
        try:
            response = httpx.get(url, timeout=60, follow_redirects=False)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise IngestionError(f"fetch failed for {filename} at {commit}: {exc}") from exc
        content = response.content
        if path is not None:
            temporary = path.with_suffix(".download")
            temporary.write_bytes(content)
            temporary.replace(path)
        return filename, content

    with ThreadPoolExecutor(max_workers=4) as workers:
        raw_files = dict(workers.map(fetch, FILES))
    return parse_sample(commit, raw_files, operator_ids, all_operators=all_operators)
