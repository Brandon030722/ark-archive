"""Synthetic boundary/counterexample tests, plus an explicitly marked real parse.

The synthetic pool is deliberately small and never represents the live roster.
Its exact answers make tag intersections and time boundaries falsifiable.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from arknights_agent.errors import Conflict, InvalidData
from arknights_agent.ingestion.contracts import SampleBatch, SnapshotInput
from arknights_agent.ingestion.recruitment import (
    CONTENT_POINTER,
    PROFESSION_TAGS,
    WIKI_REVISIONS,
    build_recruitment_batch,
    wiki_source_key,
)
from arknights_agent.ingestion.sample import (
    DEFAULT_COMMIT,
    IngestionError,
    parse_sample,
    source_key,
)
from arknights_agent.models import (
    Base,
    EvidenceSpan,
    KnowledgeRecord,
    RecordEvidence,
    SourceSnapshot,
)
from arknights_agent.recruitment import calculate
from arknights_agent.services import content_digest, ingest_batch, publish_release
from arknights_agent.storage import SnapshotStore


def _bytes(value):
    return json.dumps(value, ensure_ascii=False).encode()


def _wiki(title, text):
    revision = WIKI_REVISIONS[title]
    body = {
        "query": {
            "pages": [
                {
                    "title": title,
                    "revisions": [
                        {
                            "revid": revision,
                            "timestamp": "2026-01-01T00:00:00Z",
                            "slots": {"main": {"content": text}},
                        }
                    ],
                }
            ]
        }
    }
    return SnapshotInput(
        wiki_source_key(title),
        "synthetic_wiki_fixture",
        "https://example.org/test-rule.json",
        str(revision),
        _bytes(body),
        "application/json",
        {"authority": "synthetic_fixture"},
    )


@pytest.fixture
def synthetic_sources():
    tag_names = [
        *PROFESSION_TAGS.values(),
        "近战位",
        "远程位",
        "高级资深干员",
        "控场",
        "爆发",
        "资深干员",
        "治疗",
        "支援",
        "新手",
        "费用回复",
        "输出",
        "生存",
        "群攻",
        "防护",
        "减速",
        "削弱",
        "快速复活",
        "位移",
        "召唤",
        "支援机械",
        "元素",
    ]
    tags = [
        {"tagId": index, "tagName": name, "tagGroup": index}
        for index, name in enumerate(tag_names, 1)
    ]
    tags += [
        {"tagId": 1012, "tagName": "男性干员", "tagGroup": 1012},
        {"tagId": 1013, "tagName": "女性干员", "tagGroup": 1013},
    ]
    selections = [
        (1, "MEDIC", "RANGED", ["支援机械", "治疗"]),
        (2, "PIONEER", "MELEE", ["新手"]),
        (3, "MEDIC", "RANGED", ["治疗"]),
        (4, "SPECIAL", "MELEE", ["快速复活", "防护"]),
        (5, "MEDIC", "RANGED", ["治疗", "支援"]),
        (6, "TANK", "MELEE", ["防护", "治疗", "支援"]),
    ]
    chars = {
        f"char_synthetic_{stars}": {
            "name": f"合成样本{stars}",
            "rarity": f"TIER_{stars}",
            "profession": profession,
            "position": position,
            "tagList": traits,
        }
        for stars, profession, position, traits in selections
    }
    detail = "\r\n--------------------\r\n".join(
        f"{'★' * stars}\\n合成样本{stars}" for stars in range(1, 7)
    )
    ranges = {
        str(minutes): {
            "rarityStart": 0 if minutes <= 230 else 1 if minutes <= 450 else 2,
            "rarityEnd": 3 if minutes <= 230 else 4,
        }
        for minutes in range(10, 541, 10)
    }
    gacha = {
        "gachaTags": tags,
        "recruitDetail": detail,
        "recruitPool": {
            "recruitConstants": {"maxRecruitTime": 540},
            "recruitTimeTable": [{"timeLength": minutes} for minutes in range(10, 541, 10)],
        },
        "recruitRarityTable": ranges,
        "specialTagRarityTable": {"11": [5], "14": [4]},
    }
    snapshots = [
        SnapshotInput(
            source_key(filename),
            "community_client_data",
            f"https://example.org/{filename}",
            "a" * 40,
            _bytes(value),
            "application/json",
            {"authority": "synthetic_fixture"},
        )
        for filename, value in [("character_table.json", chars), ("gacha_table.json", gacha)]
    ]
    buttons = []
    reverse_professions = {name: enum for enum, name in PROFESSION_TAGS.items()}
    for name in tag_names:
        enum = reverse_professions.get(name)
        icon = f'<i style="mask-image:url(icon_profession_{enum.lower()}.png)"></i>' if enum else ""
        buttons.append(f"<button>{icon}{name.removesuffix('干员') if enum else name}</button>")
    widget = '<section class="hr-pick">' + "".join(buttons) + "</section>"
    rules = " 从5个给定标签中选择至多3个 最低1小时，最高9小时，以10分钟为最小单位调节 同时选择时优先最高稀有度 这两个标签将'''不会被划去''' 1~4★ 2~5★ 3~5★ 4:00:00 7:40:00 标签会被划掉 "
    wiki = [_wiki("公开招募", rules), _wiki("微件:HrCalculator", widget)]
    base = SampleBatch(
        "a" * 40,
        snapshots,
        [],
        {
            "recruitment": {
                "region": "CN",
                "mode": "public_recruitment",
                "source_revision": "a" * 40,
            }
        },
        {"recruitment": {"status": "partial"}},
    )
    return base, wiki


@pytest.fixture
def published(synthetic_sources, tmp_path):
    base, wiki = synthetic_sources
    batch = build_recruitment_batch(base, wiki)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    store = SnapshotStore(tmp_path / "snapshots")
    with Session(engine, expire_on_commit=False) as session:
        session.info["snapshot_store"] = store
        datasets = ingest_batch(
            session, batch, store, parser_version="synthetic-recruitment-fixture"
        )
        release = publish_release(
            session,
            {key: dataset.id for key, dataset in datasets.items()},
            label="synthetic-only",
            store=store,
        )
        session.commit()
        yield session, release, batch
    engine.dispose()


def _single(result, *tags):
    return next(row for row in result["combinations"] if set(row["selected_tags"]) == set(tags))


@pytest.mark.parametrize(
    "minutes,expected",
    [(60, [1, 4]), (230, [1, 4]), (240, [2, 5]), (450, [2, 5]), (460, [3, 5]), (540, [3, 5])],
)
def test_duration_boundaries_match_source_table(published, minutes, expected):
    session, release, _ = published
    result = calculate(session, release, [], minutes)
    assert result["normal_rarity_range_stars"] == expected
    assert result["combination_count"] == 1
    assert {row["rarity_stars"] for row in result["combinations"][0]["candidates"]} == set(
        range(expected[0], expected[1] + 1)
    )


@pytest.mark.parametrize("minutes,count", [(230, 1), (240, 0), (540, 0)])
def test_robot_boundary_is_inclusive_at_230(published, minutes, count):
    session, release, _ = published
    combo = calculate(session, release, ["支援机械"], minutes)["combinations"][0]
    assert combo["candidate_count"] == count
    assert combo["minimum_rarity_if_tags_retained"] == (1 if count else None)


def test_five_tags_enumerate_25_unique_selections(published):
    session, release, _ = published
    result = calculate(session, release, ["医疗", "远程位", "治疗", "支援", "高级资深"])
    assert result["combination_count"] == 25
    assert len({tuple(sorted(row["selected_tag_ids"])) for row in result["combinations"]}) == 25
    assert all(1 <= len(row["selected_tags"]) <= 3 for row in result["combinations"])
    assert result["input"]["normalized_tags"] == [
        "医疗干员",
        "远程位",
        "治疗",
        "支援",
        "高级资深干员",
    ]


def test_no_six_star_without_top_operator_and_special_priority(published):
    session, release, _ = published
    ordinary = calculate(session, release, ["治疗", "支援"])
    combo = _single(ordinary, "治疗", "支援")
    assert [row["rarity_stars"] for row in combo["candidates"]] == [5]
    special = calculate(session, release, ["高资", "资深", "重装"], 540)
    combo = _single(special, "高级资深干员", "资深干员", "重装干员")
    assert [row["rarity_stars"] for row in combo["candidates"]] == [6]
    assert combo["special_tags_protected_at_this_duration"] is True


def test_special_tag_short_duration_is_conditional_not_baseline_range(published):
    session, release, _ = published
    result = calculate(session, release, ["资深"], 60)
    combo = result["combinations"][0]
    assert result["normal_rarity_range_stars"] == [1, 4]
    assert combo["minimum_rarity_if_tags_retained"] == 5
    assert combo["special_tags_protected_at_this_duration"] is False
    assert any("未满9小时" in warning for warning in result["warnings"])


def test_conflicting_tags_do_not_invent_candidate_or_probability(published):
    session, release, _ = published
    result = calculate(session, release, ["医疗", "近战位"])
    conflict = _single(result, "医疗干员", "近战位")
    assert conflict["status"] == "no_candidate_if_tags_retained"
    assert conflict["minimum_rarity_if_tags_retained"] is None
    assert result["probabilities"] is None
    assert all(row["ordinary_tags_may_drop"] for row in result["combinations"])
    assert any("可能被划去" in warning for warning in result["warnings"])


@pytest.mark.parametrize(
    "tags,minutes",
    [
        (["男性干员"], 540),
        (["女性干员"], 540),
        (["不存在"], 540),
        (["医疗", "医疗干员"], 540),
        (["高资"] * 6, 540),
        (["治疗"], 10),
        (["治疗"], 59),
        (["治疗"], 61),
        (["治疗"], 550),
        (["治疗"], True),
    ],
)
def test_invalid_or_unselectable_input_fails_closed(published, tags, minutes):
    session, release, _ = published
    with pytest.raises(InvalidData):
        calculate(session, release, tags, minutes)


def test_partial_coverage_and_absent_input_evidence_fail_closed(published):
    session, release, _ = published
    release.manifest["datasets"]["recruitment"]["coverage"]["complete_pool"] = False
    with pytest.raises(Conflict, match="complete reviewed"):
        calculate(session, release, ["治疗"])
    release.manifest["datasets"]["recruitment"]["coverage"]["complete_pool"] = True
    link = session.query(RecordEvidence).first()
    session.delete(link)
    session.flush()
    with pytest.raises(Conflict, match="no published evidence"):
        calculate(session, release, ["治疗"])


def test_response_traces_every_input_and_keeps_release_pin(published):
    session, release, batch = published
    result = calculate(session, release, ["治疗", "支援"])
    assert result["release_id"] == release.id
    assert result["version"]["source_revision"] == batch.revision
    assert {source["revision"] for source in result["sources"]} == {
        batch.revision,
        "430506",
        "440254",
    }
    assert len(result["sources"]) == 4
    assert len(result["calculation_inputs"]["member_record_ids"]) == 6
    expected_records = {record.id for record in session.query(KnowledgeRecord).all()}
    assert {row["record_id"] for row in result["citations"]} == expected_records
    assert all(len(source["content_hash"]) == 64 for source in result["sources"])
    assert all(
        row["pointer"].startswith("/") and len(row["excerpt_hash"]) == 64
        for row in result["citations"]
    )


def _member_evidence(session):
    member = session.query(KnowledgeRecord).filter_by(kind="recruitment_member").first()
    link = session.query(RecordEvidence).filter_by(record_id=member.id).one()
    return member, session.get(EvidenceSpan, link.evidence_id)


def test_bad_excerpt_hash_is_rejected_even_without_store(published):
    session, release, _ = published
    _, evidence = _member_evidence(session)
    evidence.excerpt_hash = "0" * 64
    session.flush()
    with pytest.raises(Conflict, match="excerpt failed integrity"):
        calculate(session, release, ["治疗"])


def test_forged_excerpt_with_recomputed_hash_does_not_match_snapshot(published):
    session, release, _ = published
    _, evidence = _member_evidence(session)
    raw = json.loads(json.dumps(evidence.excerpt))
    raw["name"] = "伪造来源干员"
    evidence.excerpt = raw
    evidence.excerpt_hash = content_digest(raw)
    session.flush()
    with pytest.raises(Conflict, match="differs from original source"):
        calculate(session, release, ["治疗"], store=session.info["snapshot_store"])


def test_normalized_selectors_are_rebuilt_from_evidence(published):
    session, release, _ = published
    member, _ = _member_evidence(session)
    payload = json.loads(json.dumps(member.payload))
    payload["tag_ids"].append(12)
    payload["tag_names"].append("控场")
    member.payload = payload
    session.flush()
    with pytest.raises(Conflict, match="normalized inputs differ"):
        calculate(session, release, ["控场"])


@pytest.mark.parametrize("damage", ["modified", "missing"])
def test_original_source_object_damage_refuses_success(published, damage):
    session, release, _ = published
    source = (
        session.query(SourceSnapshot).filter_by(source_key=source_key("character_table.json")).one()
    )
    store = session.info["snapshot_store"]
    path = store.path_for(source.content_hash)
    if damage == "modified":
        path.write_bytes(b"corrupt source bytes")
    else:
        path.unlink()
    with pytest.raises(Conflict):
        calculate(session, release, ["治疗"], store=store)


def test_record_identity_cannot_reference_another_character_pointer(published):
    session, release, _ = published
    _, evidence = _member_evidence(session)
    evidence.pointer = "/char_unsupported_identity"
    session.flush()
    with pytest.raises(Conflict, match="normalized inputs differ"):
        calculate(session, release, ["治疗"])


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_extra_tag",
        "missing_ui_rule",
        "missing_widget_profession",
        "wrong_wiki_revision",
        "changed_duration_boundary",
        "changed_special_identity",
    ],
)
def test_parser_rejects_unreviewed_source_shapes(synthetic_sources, mutation):
    base, wiki = synthetic_sources
    if mutation in ("unknown_extra_tag", "changed_duration_boundary", "changed_special_identity"):
        gacha = json.loads(base.snapshots[1].content)
        if mutation == "unknown_extra_tag":
            gacha["gachaTags"].append({"tagId": 1001, "tagName": "未知新增", "tagGroup": 1001})
        elif mutation == "changed_duration_boundary":
            gacha["recruitRarityTable"]["230"]["rarityStart"] = 1
        else:
            gacha["gachaTags"][10]["tagId"] = 999
        base = replace(
            base, snapshots=[base.snapshots[0], replace(base.snapshots[1], content=_bytes(gacha))]
        )
    else:
        index = 1 if mutation == "missing_widget_profession" else 0
        obj = json.loads(wiki[index].content)
        content = obj["query"]["pages"][0]["revisions"][0]["slots"]["main"]["content"]
        if mutation == "missing_ui_rule":
            content = content.replace("最低1小时", "最低未知")
        elif mutation == "missing_widget_profession":
            content = content.replace("icon_profession_warrior.png", "unrecognized.png")
        else:
            obj["query"]["pages"][0]["revisions"][0]["revid"] = 999
        obj["query"]["pages"][0]["revisions"][0]["slots"]["main"]["content"] = content
        wiki[index] = replace(wiki[index], content=_bytes(obj))
    with pytest.raises(IngestionError):
        build_recruitment_batch(base, wiki)


@pytest.mark.live
def test_cached_real_sources_parse_complete_pool_and_trace_character_tags():
    """Uses previously fetched immutable bytes; no network dependency in unit tests."""
    client_root = Path(".local/ingestion-cache") / DEFAULT_COMMIT
    wiki_root = Path(".local/ingestion-cache/prts")
    if not client_root.exists() or not all(
        (wiki_root / f"{revision}.json").exists() for revision in WIKI_REVISIONS.values()
    ):
        pytest.skip("fetch_sample and fetch_rule_snapshots once to enable cached-real parse")
    raw_files = {path.name: path.read_bytes() for path in client_root.glob("*.json")}
    base = parse_sample(DEFAULT_COMMIT, raw_files)
    wiki = [
        SnapshotInput(
            wiki_source_key(title),
            "community_wiki_revision",
            f"https://prts.wiki/index.php?oldid={revision}",
            str(revision),
            (wiki_root / f"{revision}.json").read_bytes(),
            "application/json",
        )
        for title, revision in WIKI_REVISIONS.items()
    ]
    batch = build_recruitment_batch(base, wiki)
    pool = [record for record in batch.records if record.kind == "recruitment_member"]
    assert len(pool) == 160
    assert batch.coverage["recruitment"]["selectable_tag_count"] == 29
    saria = next(record for record in pool if record.native_id == "char_202_demkni")
    assert set(saria.payload["tag_names"]) == {
        "重装干员",
        "近战位",
        "防护",
        "治疗",
        "支援",
        "高级资深干员",
    }
    assert saria.source_key == source_key("character_table.json")
    assert saria.pointer == "/char_202_demkni"
    assert any(record.pointer == CONTENT_POINTER for record in batch.records)
