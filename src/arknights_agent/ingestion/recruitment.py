"""A complete, source-referenced recruitment pool and reviewed calculator contract.

Client data determine membership, tags and duration ranges. Two pinned PRTS
revisions supply the actual selectable UI and its minimum duration. They retain
their own source revisions; the batch revision remains the CN client commit.
"""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

import httpx

from .contracts import NormalizedRecord, SampleBatch, SnapshotInput
from .sample import IngestionError, _recruit_members, pointer, source_key

PARSER_VERSION = "cn-recruitment-v2"
WIKI_API = "https://prts.wiki/api.php"
WIKI_REVISIONS = {"公开招募": 430506, "微件:HrCalculator": 440254}
PROFESSION_TAGS = {
    "WARRIOR": "近卫干员",
    "SNIPER": "狙击干员",
    "TANK": "重装干员",
    "MEDIC": "医疗干员",
    "SUPPORT": "辅助干员",
    "CASTER": "术师干员",
    "SPECIAL": "特种干员",
    "PIONEER": "先锋干员",
}
POSITION_TAGS = {"MELEE": "近战位", "RANGED": "远程位"}
CONTENT_POINTER = "/query/pages/0/revisions/0/slots/main/content"


def wiki_source_key(title: str) -> str:
    return f"prts:recruitment:{title}"


def fetch_rule_snapshots(cache_dir: Path | str = ".local/ingestion-cache/prts"):
    """Fetch immutable MediaWiki revision JSON; never execute the widget code."""
    root = Path(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    snapshots = []
    with httpx.Client(timeout=45, follow_redirects=False) as client:
        for title, revision in WIKI_REVISIONS.items():
            target = root / f"{revision}.json"
            url = str(
                httpx.URL(
                    WIKI_API,
                    params={
                        "action": "query",
                        "prop": "revisions",
                        "revids": str(revision),
                        "rvprop": "ids|timestamp|content",
                        "rvslots": "main",
                        "format": "json",
                        "formatversion": "2",
                    },
                )
            )
            if target.exists():
                content = target.read_bytes()
            else:
                response = client.get(url)
                response.raise_for_status()
                content = response.content
                descriptor, temporary = tempfile.mkstemp(dir=root, prefix=f".{revision}-")
                try:
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, target)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            page, row, _ = _wiki_content(content, title, revision)
            snapshots.append(
                SnapshotInput(
                    source_key=wiki_source_key(title),
                    source_type="community_wiki_revision",
                    url=url,
                    revision=str(revision),
                    content=content,
                    media_type="application/json",
                    metadata={
                        "authority": "community_wiki",
                        "title": page["title"],
                        "page_url": f"https://prts.wiki/index.php?oldid={revision}",
                        "wiki_revision": revision,
                        "review_status": "source_reviewed",
                        "parser_version": PARSER_VERSION,
                    },
                    published_at=datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")),
                )
            )
    return snapshots


def _wiki_content(content: bytes, title: str, revision: int):
    try:
        doc = json.loads(content)
        pages = doc["query"]["pages"]
        if len(pages) != 1:
            raise ValueError("ambiguous wiki revision")
        page = pages[0]
        row = page["revisions"][0]
        text = row["slots"]["main"]["content"]
        if page["title"] != title or row["revid"] != revision or not isinstance(text, str):
            raise ValueError("wrong pinned wiki revision")
        return page, row, text
    except (KeyError, TypeError, IndexError, ValueError) as exc:
        raise IngestionError(f"invalid fixed PRTS revision: {title} / {revision}") from exc


def _catalog(text: str) -> tuple[list[str], dict[str, str]]:
    # Only the current rendered selection section; ignore legacy scripts/shells.
    section = re.search(r'<section\b[^>]*class="hr-pick"[^>]*>(.*?)</section>', text, re.S)
    if section is None:
        raise IngestionError("PRTS selectable catalog section is absent")
    names, professions = [], {}
    for body in re.findall(r"<button\b[^>]*>(.*?)</button>", section.group(1), re.S):
        name = html.unescape(re.sub(r"<[^>]*>", "", body)).strip()
        icon = re.search(r"icon_profession_([a-z]+)\.png", body)
        if icon:
            professions[icon.group(1).upper()] = f"{name}干员"
            name = f"{name}干员"
        if not name or name in names:
            raise IngestionError("empty or duplicated PRTS selectable tag")
        names.append(name)
    if professions != PROFESSION_TAGS or len(names) != 29:
        raise IngestionError("unsupported PRTS profession mapping or selectable catalog")
    return names, professions


def _ui_contract(text: str) -> dict:
    # Explicit assertions protect the manually reviewed interpretation against
    # a changed source or missing data. Boundaries themselves come from client data.
    required = (
        "从5个给定标签中选择至多3个",
        "最低1小时，最高9小时，以10分钟为最小单位调节",
        "同时选择时优先最高稀有度",
        "这两个标签将'''不会被划去'''",
        "1~4★",
        "2~5★",
        "3~5★",
        "4:00:00",
        "7:40:00",
        "标签会被划掉",
    )
    if any(fragment not in text for fragment in required):
        raise IngestionError("PRTS recruitment rule review no longer matches the pinned source")
    return {
        "minimum_duration_minutes": 60,
        "maximum_duration_minutes": 540,
        "duration_step_minutes": 10,
        "maximum_presented_tags": 5,
        "maximum_selected_tags": 3,
        "special_tag_priority": [11, 14],
        "special_tag_protected_duration_minutes": 540,
        "normal_tags_may_drop": True,
        "results_are_conditional_on_tags_retained": True,
        "probabilities_supported": False,
        "review_status": "source_reviewed",
        "interpretation_version": PARSER_VERSION,
    }


def build_recruitment_batch(
    base_batch: SampleBatch, rule_snapshots: list[SnapshotInput] | None = None
) -> SampleBatch:
    """Replace partial recruitment records; preserve the other module records.

    Call before services.ingest_batch(..., parser_version=PARSER_VERSION).
    The resulting member records reference character JSON; recruitDetail is a
    separate mandatory evidence record for the entire explicit membership list.
    """
    client = {source.source_key: source for source in base_batch.snapshots}
    try:
        characters = json.loads(client[source_key("character_table.json")].content)
        gacha = json.loads(client[source_key("gacha_table.json")].content)
    except (KeyError, ValueError) as exc:
        raise IngestionError(
            "complete original character and gacha snapshots are required"
        ) from exc
    wiki = list(fetch_rule_snapshots() if rule_snapshots is None else rule_snapshots)
    by_key = {source.source_key: source for source in wiki}
    if len(by_key) != 2 or set(by_key) != {wiki_source_key(t) for t in WIKI_REVISIONS}:
        raise IngestionError("both reviewed PRTS source snapshots are required")
    texts = {
        title: _wiki_content(by_key[wiki_source_key(title)].content, title, revision)[2]
        for title, revision in WIKI_REVISIONS.items()
    }
    for title, revision in WIKI_REVISIONS.items():
        if by_key[wiki_source_key(title)].revision != str(revision):
            raise IngestionError("snapshot revision metadata differs from original wiki bytes")
    selectable, professions = _catalog(texts["微件:HrCalculator"])
    contract = _ui_contract(texts["公开招募"])
    members = _recruit_members(gacha["recruitDetail"], characters)
    tags = gacha["gachaTags"]
    names = {tag["tagName"]: tag["tagId"] for tag in tags}
    if len(names) != len(tags) or len(set(names.values())) != len(tags):
        raise IngestionError("duplicate client tag identity")
    if names.get("高级资深干员") != 11 or names.get("资深干员") != 14:
        raise IngestionError("unreviewed special-tag identity mapping")
    if not set(selectable).issubset(names):
        raise IngestionError("PRTS selectable catalog differs from client tags")
    # Known non-selectable gender entries are explicitly excluded by the wiki UI.
    if set(names) - set(selectable) != {"男性干员", "女性干员"}:
        raise IngestionError("unreviewed non-selectable client tags; cannot infer tag behavior")
    # The fixed wiki review and client boundaries must agree. A legitimate future
    # rules change needs a new review, rather than silently reusing this contract.
    for minutes in range(60, 541, 10):
        expected = {
            "rarityStart": 0 if minutes <= 230 else 1 if minutes <= 450 else 2,
            "rarityEnd": 3 if minutes <= 230 else 4,
        }
        if gacha.get("recruitRarityTable", {}).get(str(minutes)) != expected:
            raise IngestionError("client duration range differs from reviewed recruitment rules")
    if gacha.get("specialTagRarityTable") != {"11": [5], "14": [4]}:
        raise IngestionError("client special-tag rarity differs from reviewed rules")
    if gacha.get("recruitPool", {}).get("recruitConstants", {}).get("maxRecruitTime") != 540:
        raise IngestionError("client maximum duration differs from reviewed UI")
    tag_lookup = {tag["tagName"]: (i, tag) for i, tag in enumerate(tags)}
    records = [record for record in base_batch.records if record.module != "recruitment"]

    def add(kind, native_id, name, payload, key, path):
        records.append(NormalizedRecord("recruitment", kind, native_id, name, payload, key, path))

    for name in selectable:
        index, tag = tag_lookup[name]
        add(
            "recruitment_tag",
            str(tag["tagId"]),
            name,
            tag,
            source_key("gacha_table.json"),
            pointer("gachaTags", index),
        )
    for ident, (name, stars) in members.items():
        char = characters[ident]
        profession = professions.get(char.get("profession"))
        position = POSITION_TAGS.get(char.get("position"))
        traits = char.get("tagList")
        if profession is None or position is None or not isinstance(traits, list):
            raise IngestionError(f"unmapped recruitment character selectors: {ident}")
        normalized_tags = [profession, position, *traits]
        if stars in (5, 6):
            normalized_tags.append("资深干员" if stars == 5 else "高级资深干员")
        if len(normalized_tags) != len(set(normalized_tags)) or any(
            tag not in selectable for tag in normalized_tags
        ):
            raise IngestionError(f"unknown/duplicate character recruitment tag: {ident}")
        payload = {
            "name": name,
            "rarity_stars": stars,
            "profession": char["profession"],
            "position": char["position"],
            "raw_tag_list": traits,
            "tag_ids": sorted(names[tag] for tag in normalized_tags),
            "tag_names": normalized_tags,
            "membership_basis": "recruitDetail",
            "membership_record": "recruitDetail",
            "coverage": "full",
            "selector_mapping_record": "selectable_catalog",
        }
        add(
            "recruitment_member",
            ident,
            name,
            payload,
            source_key("character_table.json"),
            pointer(ident),
        )
    for key in ("recruitPool", "recruitRarityTable", "specialTagRarityTable"):
        data = gacha[key]
        if not isinstance(data, dict) or not data:
            raise IngestionError(f"missing recruitment rule: {key}")
        add("recruitment_rules", key, key, data, source_key("gacha_table.json"), pointer(key))
    add(
        "recruitment_rules",
        "recruitDetail",
        "公招完整成员与稀有职业说明",
        {"description_raw": gacha["recruitDetail"], "member_ids": sorted(members)},
        source_key("gacha_table.json"),
        "/recruitDetail",
    )
    add(
        "recruitment_calculator_rules",
        "ui_contract",
        "公招界面与标签规则核验",
        contract,
        wiki_source_key("公开招募"),
        CONTENT_POINTER,
    )
    add(
        "recruitment_calculator_rules",
        "selectable_catalog",
        "公招可选标签目录核验",
        {
            "tag_names": selectable,
            "profession_mapping": professions,
            "review_status": "source_reviewed",
        },
        wiki_source_key("微件:HrCalculator"),
        CONTENT_POINTER,
    )
    scopes = {key: dict(value) for key, value in base_batch.scopes.items()}
    scopes["recruitment"].update(
        {
            "mapping_status": "source_reviewed",
            "rule_source_revisions": {
                title: str(revision) for title, revision in WIKI_REVISIONS.items()
            },
            "source_effective_date_verified": False,
        }
    )
    coverage = {key: dict(value) for key, value in base_batch.coverage.items()}
    coverage["recruitment"] = {
        "status": "full",
        "complete_pool": True,
        "member_count": len(members),
        "member_ids": sorted(members),
        "selectable_tag_count": len(selectable),
        "tag_mapping_verified": True,
        "ui_contract_verified": True,
        "calculator_enabled": True,
        "probabilities_supported": False,
        "coverage_basis": "all members explicitly listed in pinned recruitDetail",
        "source_revision_is_live_game_version": False,
    }
    return SampleBatch(
        base_batch.revision, [*base_batch.snapshots, *wiki], records, scopes, coverage
    )
