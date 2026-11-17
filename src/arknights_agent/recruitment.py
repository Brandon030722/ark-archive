"""Deterministic recruitment combinations over one immutable published dataset.

No sampling or probability estimates. Each candidate set is conditional on the
selected ordinary tags surviving; an empty intersection is reported explicitly.
"""

from __future__ import annotations

import json
from itertools import combinations
from unicodedata import normalize

from sqlalchemy import select

from arknights_agent.errors import Conflict, InvalidData, NotFound
from arknights_agent.ingestion.recruitment import (
    POSITION_TAGS,
    PROFESSION_TAGS,
    _catalog,
    _ui_contract,
)
from arknights_agent.ingestion.sample import IngestionError, _recruit_members, pointer
from arknights_agent.models import EvidenceSpan, KnowledgeRecord, RecordEvidence, SourceSnapshot
from arknights_agent.services import content_digest, json_pointer, same_json_value

ALIASES = {
    "高资": "高级资深干员",
    "高级资深": "高级资深干员",
    "资深": "资深干员",
    **{
        name: f"{name}干员"
        for name in ("近卫", "狙击", "重装", "医疗", "辅助", "术师", "特种", "先锋")
    },
}
SPECIAL_TAG_IDS = (11, 14)


def _citations(session, release, records, store=None):
    rows = list(
        session.execute(
            select(RecordEvidence, EvidenceSpan, SourceSnapshot)
            .join(EvidenceSpan, EvidenceSpan.id == RecordEvidence.evidence_id)
            .join(SourceSnapshot, SourceSnapshot.id == EvidenceSpan.snapshot_id)
            .where(RecordEvidence.record_id.in_([record.id for record in records]))
            .order_by(RecordEvidence.record_id, EvidenceSpan.id)
        )
    )
    seen, citations, sources, documents, excerpts = set(), [], {}, {}, {}
    allowed = set(release.manifest["datasets"]["recruitment"]["source_snapshot_ids"])
    for link, evidence, snapshot in rows:
        if snapshot.id not in allowed or evidence.dataset_id != link.dataset_id:
            raise Conflict("recruitment evidence refers outside the pinned release")
        if content_digest(evidence.excerpt) != evidence.excerpt_hash:
            raise Conflict("recruitment evidence excerpt failed integrity verification")
        if store is not None:
            if snapshot.id not in documents:
                raw = store.read_verified(snapshot.content_hash)
                if len(raw) != snapshot.byte_size:
                    raise Conflict("recruitment source snapshot size mismatch")
                try:
                    documents[snapshot.id] = json.loads(raw)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise Conflict("recruitment source snapshot is not valid JSON") from exc
            try:
                actual = json_pointer(documents[snapshot.id], evidence.pointer)
            except InvalidData as exc:
                raise Conflict("recruitment evidence pointer is absent in source") from exc
            if not same_json_value(actual, evidence.excerpt):
                raise Conflict("recruitment evidence differs from original source snapshot")
        if link.record_id in excerpts and not same_json_value(excerpts[link.record_id], evidence.excerpt):
            raise Conflict("recruitment record has inconsistent extraction evidence")
        excerpts[link.record_id] = evidence.excerpt
        seen.add(link.record_id)
        citations.append(
            {
                "record_id": link.record_id,
                "evidence_id": evidence.id,
                "snapshot_id": snapshot.id,
                "pointer": evidence.pointer,
                "excerpt_hash": evidence.excerpt_hash,
                "source_url": snapshot.url,
                "source_revision": snapshot.revision,
                "content_hash": snapshot.content_hash,
                "authority": snapshot.source_metadata.get("authority", "unspecified"),
            }
        )
        sources[snapshot.id] = {
            "snapshot_id": snapshot.id,
            "source_key": snapshot.source_key,
            "url": snapshot.url,
            "revision": snapshot.revision,
            "content_hash": snapshot.content_hash,
            "authority": snapshot.source_metadata.get("authority", "unspecified"),
            "page_url": snapshot.source_metadata.get("page_url"),
        }
    if seen != {record.id for record in records}:
        raise Conflict("recruitment calculation input has no published evidence")
    evidence_by_record = {}
    for row in citations:
        evidence_by_record.setdefault(row["record_id"], []).append(row["evidence_id"])
    return citations, list(sources.values()), evidence_by_record, excerpts


def _validate_extracted_inputs(records, excerpts, citations):
    """Rebuild normalized selectors/rules from evidence instead of trusting payloads."""
    by_key = {record.native_id: record for record in records}
    tag_names = {
        record.name: record.payload["tagId"]
        for record in records
        if record.kind == "recruitment_tag"
    }
    characters = {}
    citation_by_record = {}
    for row in citations:
        citation_by_record.setdefault(row["record_id"], []).append(row)
    try:
        for record in records:
            raw, payload = excerpts[record.id], record.payload
            if record.kind == "recruitment_tag":
                if (
                    not same_json_value(payload, raw)
                    or record.native_id != str(raw["tagId"])
                    or record.name != raw["tagName"]
                ):
                    raise ValueError("selectable tag extraction differs")
            elif record.kind == "recruitment_member":
                if any(
                    row["pointer"] != pointer(record.native_id)
                    for row in citation_by_record[record.id]
                ):
                    raise ValueError("character evidence identity differs")
                stars = int(raw["rarity"].removeprefix("TIER_"))
                selectors = [
                    PROFESSION_TAGS[raw["profession"]],
                    POSITION_TAGS[raw["position"]],
                    *raw["tagList"],
                ]
                if stars in (5, 6):
                    selectors.append("资深干员" if stars == 5 else "高级资深干员")
                expected = {
                    "name": raw["name"],
                    "rarity_stars": stars,
                    "profession": raw["profession"],
                    "position": raw["position"],
                    "raw_tag_list": raw["tagList"],
                    "tag_ids": sorted(tag_names[name] for name in selectors),
                    "tag_names": selectors,
                    "membership_basis": "recruitDetail",
                    "membership_record": "recruitDetail",
                    "coverage": "full",
                    "selector_mapping_record": "selectable_catalog",
                }
                if not same_json_value(payload, expected) or record.name != raw["name"]:
                    raise ValueError("character selector extraction differs")
                characters[record.native_id] = raw
            elif record.kind == "recruitment_rules":
                if record.native_id == "recruitDetail":
                    if not same_json_value(payload["description_raw"], raw):
                        raise ValueError("explicit pool extraction differs")
                elif not same_json_value(payload, raw):
                    raise ValueError("client rule extraction differs")
            elif record.native_id == "ui_contract":
                if not same_json_value(payload, _ui_contract(raw)):
                    raise ValueError("reviewed UI contract extraction differs")
            elif record.native_id == "selectable_catalog":
                selectable, professions = _catalog(raw)
                expected = {
                    "tag_names": selectable,
                    "profession_mapping": professions,
                    "review_status": "source_reviewed",
                }
                if not same_json_value(payload, expected):
                    raise ValueError("reviewed selectable catalog extraction differs")
        detail = by_key["recruitDetail"]
        explicit = _recruit_members(excerpts[detail.id], characters)
        if sorted(explicit) != detail.payload["member_ids"] or len(explicit) != len(characters):
            raise ValueError("explicit pool and source characters differ")
    except (KeyError, TypeError, ValueError, AttributeError, IngestionError) as exc:
        raise Conflict("recruitment normalized inputs differ from their source evidence") from exc


def calculate(session, release, tags: list[str], duration_minutes: int = 540, store=None,
              *, single_tag_overview: bool = False) -> dict:
    """Enumerate all 1–3 tag selections from at most five displayed tags.

    The caller resolves the release once, before invoking this function. This
    function never changes or resolves the active pointer and never uses partial
    datasets. Empty input returns the single no-tag baseline selection.
    """
    entry = release.manifest.get("datasets", {}).get("recruitment")
    if entry is None:
        raise NotFound("recruitment is not included in this release")
    coverage = entry.get("coverage", {})
    if (
        coverage.get("status") != "full"
        or coverage.get("complete_pool") is not True
        or coverage.get("tag_mapping_verified") is not True
        or coverage.get("ui_contract_verified") is not True
        or coverage.get("calculator_enabled") is not True
    ):
        raise Conflict("pinned recruitment release lacks a complete reviewed pool and rules")
    records = list(
        session.scalars(
            select(KnowledgeRecord)
            .where(
                KnowledgeRecord.dataset_id == entry["dataset_id"],
                KnowledgeRecord.module == "recruitment",
            )
            .order_by(KnowledgeRecord.kind, KnowledgeRecord.native_id)
        )
    )
    catalog = [record for record in records if record.kind == "recruitment_tag"]
    members = [record for record in records if record.kind == "recruitment_member"]
    rules = {
        record.native_id: record
        for record in records
        if record.kind in ("recruitment_rules", "recruitment_calculator_rules")
    }
    required = {
        "ui_contract",
        "selectable_catalog",
        "recruitDetail",
        "recruitPool",
        "recruitRarityTable",
        "specialTagRarityTable",
    }
    if not required.issubset(rules):
        raise Conflict("pinned recruitment rules are incomplete")
    if len(members) != coverage.get("member_count") or len(catalog) != coverage.get(
        "selectable_tag_count"
    ):
        raise Conflict("published recruitment coverage differs from actual inputs")
    member_ids = sorted(record.native_id for record in members)
    if member_ids != coverage.get("member_ids") or member_ids != rules["recruitDetail"].payload.get(
        "member_ids"
    ):
        raise Conflict("published pool is not the complete explicit membership list")
    contract = rules["ui_contract"].payload
    expected_contract = {
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
    }
    if any(not same_json_value(contract.get(key), value) for key, value in expected_contract.items()):
        raise Conflict("calculator does not support this published recruitment contract")
    if rules["recruitPool"].payload.get("recruitConstants", {}).get("maxRecruitTime") != 540:
        raise Conflict("unreviewed client recruitment time contract")
    if (
        not isinstance(duration_minutes, int)
        or isinstance(duration_minutes, bool)
        or not 60 <= duration_minutes <= 540
        or duration_minutes % 10
    ):
        raise InvalidData("duration must be 60–540 minutes in 10-minute increments")
    if not isinstance(tags, list) or len(tags) > 5 or any(not isinstance(tag, str) for tag in tags):
        raise InvalidData("tags must be a list of at most five selectable tag names")
    by_name = {record.name: record for record in catalog}
    if len(by_name) != len(catalog):
        raise Conflict("duplicate recruitment tag name")
    reviewed_catalog = rules["selectable_catalog"].payload
    if (
        set(by_name) != set(reviewed_catalog.get("tag_names", []))
        or reviewed_catalog.get("review_status") != "source_reviewed"
    ):
        raise Conflict("client selectable catalog differs from reviewed UI catalog")
    normalized = []
    for value in tags:
        name = normalize("NFKC", value).strip()
        name = ALIASES.get(name, name)
        if name not in by_name:
            raise InvalidData(f"unknown or non-selectable public recruitment tag: {value}")
        if name in normalized:
            raise InvalidData("duplicate tags after normalization")
        normalized.append(name)
    try:
        time_rule = rules["recruitRarityTable"].payload[str(duration_minutes)]
        lower, upper = time_rule["rarityStart"] + 1, time_rule["rarityEnd"] + 1
    except (KeyError, TypeError) as exc:
        raise Conflict("client recruitment time range is absent or invalid") from exc
    if not isinstance(lower, int) or not isinstance(upper, int) or not 1 <= lower <= upper <= 5:
        raise Conflict("unreviewed client recruitment rarity range")
    special = rules["specialTagRarityTable"].payload
    if not same_json_value(special, {"11": [5], "14": [4]}):
        raise Conflict("unreviewed special-tag rarity behavior")
    tag_ids = {name: by_name[name].payload["tagId"] for name in by_name}
    if len(set(tag_ids.values())) != len(tag_ids):
        raise Conflict("duplicate selectable recruitment tag identity")
    member_inputs = []
    for record in members:
        payload = record.payload
        stars, ids = payload.get("rarity_stars"), payload.get("tag_ids")
        if (
            not isinstance(stars, int)
            or isinstance(stars, bool)
            or not 1 <= stars <= 6
            or not isinstance(ids, list)
            or not ids
            or len(ids) != len(set(ids))
            or not set(ids).issubset(tag_ids.values())
            or payload.get("coverage") != "full"
        ):
            raise Conflict("recruitment member has unknown selectors or rarity")
        if (11 in ids) != (stars == 6) or (14 in ids) != (stars == 5):
            raise Conflict("recruitment member has inconsistent special-tag selectors")
        member_inputs.append((record, stars, set(ids)))
    cited_records = [*members, *catalog, *(rules[key] for key in sorted(required))]
    citations, sources, evidence_by_record, excerpts = _citations(
        session, release, cited_records, store
    )
    _validate_extracted_inputs(cited_records, excerpts, citations)
    if single_tag_overview:
        if tags:
            raise InvalidData('single tag overview cannot take selected tags')
        normalized = sorted(by_name)
    selections = [(name,) for name in normalized] if single_tag_overview else [
        combo
        for size in range(1, min(3, len(normalized)) + 1)
        for combo in combinations(normalized, size)
    ]
    if not normalized:
        selections = [()]
    result = []
    rule_evidence = sorted({eid for key in required for eid in evidence_by_record[rules[key].id]})
    for selected in selections:
        selected_ids = {tag_ids[name] for name in selected}
        selected_special = next((tag for tag in SPECIAL_TAG_IDS if tag in selected_ids), None)
        allowed_stars = (
            {special[str(selected_special)][0] + 1}
            if selected_special
            else set(range(lower, upper + 1))
        )
        # Rare requirements take priority when both are selected (source explicit).
        ordinary_required = selected_ids - set(SPECIAL_TAG_IDS)
        candidates = []
        for record, stars, selectors in member_inputs:
            if stars not in allowed_stars or not ordinary_required.issubset(selectors):
                continue
            candidates.append(
                {
                    "native_id": record.native_id,
                    "name": record.name,
                    "rarity_stars": stars,
                    "tag_names": record.payload["tag_names"],
                    "record_id": record.id,
                    "evidence_ids": evidence_by_record[record.id],
                }
            )
        candidates.sort(key=lambda candidate: (candidate["rarity_stars"], candidate["native_id"]))
        combination_evidence = sorted(
            set(rule_evidence)
            | {eid for name in selected for eid in evidence_by_record[by_name[name].id]}
            | {eid for candidate in candidates for eid in candidate["evidence_ids"]}
        )
        result.append(
            {
                "selected_tags": list(selected),
                "selected_tag_ids": sorted(selected_ids),
                "candidate_count": len(candidates),
                "candidates": candidates,
                "minimum_rarity_if_tags_retained": min(
                    (candidate["rarity_stars"] for candidate in candidates), default=None
                ),
                "status": "conditional_candidates"
                if candidates
                else "no_candidate_if_tags_retained",
                "condition": "所选标签保留；稀有标签同时选择时按最高稀有度优先",
                "ordinary_tags_may_drop": True,
                "special_tags_protected_at_this_duration": selected_special is not None
                and duration_minutes == 540,
                "evidence_ids": combination_evidence,
            }
        )
    result.sort(
        key=lambda combo: (
            -(combo["minimum_rarity_if_tags_retained"] or 0),
            combo["candidate_count"] if combo["candidate_count"] else len(members) + 1,
            len(combo["selected_tags"]),
            tuple(combo["selected_tag_ids"]),
        )
    )
    warnings = [
        "候选集合与最低星级以所选普通标签保留为条件；普通标签可能被划去。",
        "候选数量不代表概率，不推断等概率，不承诺获得某位干员。",
        "无候选的组合表示标签保留条件下交集为空，不能据此预测实际掉标签结果。",
        "版本指固定来源数据修订；未自动验证当前线上游戏的生效时间。",
    ]
    if "支援机械" in normalized and duration_minutes > 230:
        warnings.append(
            "本时长的普通稀有度范围排除1星，支援机械单标签在保留条件下无候选；230分钟仍含1星。"
        )
    if any(tag_ids[name] in SPECIAL_TAG_IDS for name in normalized) and duration_minutes < 540:
        warnings.append("未满9小时，资深/高级资深标签也可能被划去。")
    return {
        "status": "computed",
        "release_id": release.id,
        "manifest_hash": release.manifest_hash,
        "version": {
            "release_id": release.id,
            "manifest_hash": release.manifest_hash,
            "dataset_id": entry["dataset_id"],
            "ruleset_id": entry["ruleset_id"],
            "source_revision": entry["source_revision"],
            "rule_source_revisions": entry.get("scope", {}).get("rule_source_revisions", {}),
            "region": entry.get("scope", {}).get("region", "CN"),
        },
        "coverage": coverage,
        "input": {
            "tags": tags,
            "normalized_tags": normalized,
            "duration_minutes": duration_minutes,
            **({'scope': 'all_selectable_single_tags'} if single_tag_overview else {}),
        },
        "normal_rarity_range_stars": [lower, upper],
        "combination_count": len(result),
        "combinations": result,
        "warnings": warnings,
        "sources": sources,
        "citations": citations,
        "evidence_ids": sorted({citation["evidence_id"] for citation in citations}),
        "calculation_inputs": {
            "member_record_ids": [record.id for record in members],
            "tag_record_ids": [record.id for record in catalog],
            "rule_record_ids": {key: rules[key].id for key in sorted(required)},
        },
        "trace": [
            {
                "tool": "recruitment.calculate",
                "release_id": release.id,
                "duration_minutes": duration_minutes,
                "combination_count": len(result),
            }
        ],
        "probabilities": None,
    }
