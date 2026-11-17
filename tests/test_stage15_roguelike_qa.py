"""Release-pinned synthetic document answers; no model, network or live ledger."""

import copy
import json
from types import SimpleNamespace

import pytest

from arknights_agent import models, roguelike, services
from arknights_agent.db import make_engine, session_factory
from arknights_agent.errors import Conflict
from arknights_agent.ingestion.contracts import NormalizedRecord, SampleBatch, SnapshotInput
from arknights_agent.roguelike_sources import (
    ACTIVITY_FILE,
    LEGACY_FILE,
    LEGACY_THEME,
    THEME_NAMES,
    TOPIC_FILE,
)
from arknights_agent.storage import SnapshotStore

REVISION = "e" * 40


@pytest.fixture
def make_knowledge(tmp_path):
    engines, sessions = [], []
    def create(*, extra_relics=0, ambiguous=False, missing_buff_item=False):
        engine = make_engine(f"sqlite:///{tmp_path / f'qa-{len(engines)}.sqlite3'}")
        models.Base.metadata.create_all(engine)
        engines.append(engine)
        session = session_factory(engine)()
        sessions.append(session)
        store = SnapshotStore(tmp_path / f"objects-{len(engines)}")
        documents = {TOPIC_FILE: {"topics": {}, "details": {}, "modules": {}, "customizeData": {}},
                     LEGACY_FILE: {"itemTable": {"items": {}, "relics": {}}},
                     ACTIVITY_FILE: {"basicInfo": {}}}
        records = []
        def row(theme, kind, filename, path, raw, name=None):
            source = documents[filename]
            tokens = path[1:].split("/")
            for token in tokens[:-1]:
                source = source.setdefault(token, {})
            source[tokens[-1]] = raw
            records.append(NormalizedRecord(
                "roguelike", "roguelike_" + kind,
                theme if kind == "theme" else theme + ":" + path,
                name or raw.get("name") or raw.get("title") or path.rsplit("/", 1)[-1],
                raw, "fixture:/" + filename, path, None if kind == "theme" else theme,
            ))
        for index, (theme, name) in enumerate(THEME_NAMES.items()):
            legacy = theme == LEGACY_THEME
            filename = LEGACY_FILE if legacy else TOPIC_FILE
            base = "" if legacy else f"/details/{theme}"
            row(theme, "theme", ACTIVITY_FILE if legacy else TOPIC_FILE,
                f"/basicInfo/{theme}" if legacy else f"/topics/{theme}",
                {"id": theme, "name": name, "startTime": index, "lineText": "来源原文"})
            item_base = "/itemTable" if legacy else base
            for native, display, kind in [
                ("relic_a", "共享藏品", "relic"), ("band_a", "指挥分队", "squad"),
                ("relic_boundary", "叠加萨卡兹概率", "relic"),
            ]:
                if not (missing_buff_item and native == "relic_a" and theme == "rogue_1"):
                    row(theme, kind, filename, item_base + "/items/" + native, {
                        "id": native, "name": display,
                        "type": "BAND" if kind == "squad" and not legacy else "RELIC",
                        "rarity": "BORN" if kind == "squad" else "NORMAL",
                        "usage": f"<{index}>完整效果 <@rogue.attention>+{index}%</>",
                        "description": None, "unlockCondDesc": "文档条件", "empty": [],
                    })
                row(theme, "buff", filename, item_base + "/relics/" + native,
                    {"id": native, "buffs": [{"key": "source_only", "blackboard": [
                        {"key": "count", "value": index, "valueStr": None}
                    ]}]}, name=display)
            row(theme, "ending", filename, base + "/endings/end_1",
                {"id": "end_1", "name": "结局一", "desc": "结局原文", "changeEndingDesc": None})
            row(theme, "node", filename, base + "/nodeTypeData/SHOP",
                {"name": "诡意行商", "description": "节点原文"})
            for section, kind, display in [
                ("stages", "stage", "测试关卡"), ("zones", "zone", "测试区域"),
                ("choiceScenes", "event", "测试事件"), ("choices", "choice", "测试选项"),
            ]:
                row(theme, kind, filename, base + f"/{section}/test_1",
                    {"id": "test_1", "name": display, "description": "完整原文", "extra": None})
            for grade in (0, 1):
                row(theme, "difficulty", filename, base + f"/difficulties/g{grade}",
                    {"grade": grade, "name": "同名难度", "ruleDesc": f"条件{grade}"})
            row(theme, "initial", filename, base + "/init/normal",
                {"modeId": "NORMAL", "name": "NORMAL", "initialGold": index, "initialHp": 4})
            row(theme, "rule", filename, base + "/rules/rule_1",
                {"name": "原文机制", "functionDesc": "此处只是原文", "level": None})
            row(theme, "rule", filename, base + "/rules/rule_numeric",
                {"name": "1", "functionDesc": "原始短名机制", "level": 1})
            for short_name in ("水月", "刻俄柏", "傀影", "银凇", "招募券", "深入探索"):
                row(theme, "item", filename, item_base + "/items/short_" + short_name,
                    {"id": "short_" + short_name, "name": short_name, "type": "CHARACTER"})
            if theme == "rogue_1":
                for number in range(extra_relics):
                    row(theme, "relic", filename, item_base + f"/items/extra_{number}", {
                        "id": f"extra_{number}", "name": f"人工藏品{number}", "type": "RELIC",
                        "usage": f"人工原文{number}", "description": None,
                    })
                if ambiguous:
                    row(theme, "relic", filename, item_base + "/items/relic_duplicate", {
                        "id": "relic_duplicate", "name": "共享藏品", "type": "RELIC",
                        "usage": "另一个同名来源对象",
                    })
        batch = SampleBatch(REVISION, [SnapshotInput(
            "fixture:/" + filename, "synthetic_test", f"https://example.invalid/{filename}",
            REVISION, json.dumps(document, ensure_ascii=False).encode(), "application/json",
        ) for filename, document in documents.items()], records,
            {"roguelike": {"region": "CN", "mode": "fixed_client_config", "theme_ids": list(THEME_NAMES)}},
            {"roguelike": {"status": "synthetic_fixture", "partial": True}})
        datasets = services.ingest_batch(session, batch, store)
        release = services.publish_release(session, {"roguelike": datasets["roguelike"].id},
                                           label="synthetic documents", store=store)
        session.commit()
        return SimpleNamespace(session=session, release=release, store=store)
    yield create
    for session in sessions:
        session.close()
    for engine in engines:
        engine.dispose()


def ask(knowledge, question):
    return roguelike.answer_roguelike(knowledge.session, knowledge.release, question,
                                    store=knowledge.store)


def assert_originals(answer, knowledge):
    assert answer["claims"] and answer["citations"]
    citations = {row["id"]: row for row in answer["citations"]}
    for citation in citations.values():
        snapshot = services.get_snapshot(knowledge.session, knowledge.release, citation["snapshot_id"])
        raw = knowledge.store.read_verified(citation["snapshot_hash"])
        document = services.snapshot_document(raw, snapshot["media_type"])
        actual = services.json_pointer(document, citation["pointer"])
        assert services.same_json_value(actual, citation["field_value"])
        assert services.content_digest(actual) == citation["field_hash"]
        assert citation["source_revision"] == REVISION
    for claim in answer["claims"]:
        assert len(claim["citation_ids"]) == 1
        citation = citations[claim["citation_ids"][0]]
        assert services.same_json_value(claim["value"], citation["field_value"])


@pytest.mark.parametrize("theme,name", THEME_NAMES.items())
def test_each_theme_document_and_legacy_name_are_source_owned(make_knowledge, theme, name):
    knowledge = make_knowledge()
    result = ask(knowledge, f"{name}主题原文")
    assert result["status"] == "answered"
    assert result["theme_id"] == theme
    assert result["module"] == "roguelike"
    assert result["version"]["source_revision"] == REVISION
    assert result["records"][0]["data"]["name"] == name
    assert result["tool_trace"][-1]["tool"] == "roguelike_source_document"
    assert result["tool_trace"][-1]["runtime_calculation"] is False
    assert_originals(result, knowledge)


def test_theme_catalog_has_every_declared_theme_and_complete_payload(make_knowledge):
    knowledge = make_knowledge()
    result = ask(knowledge, "列出肉鸽所有主题目录")
    assert result["status"] == "answered"
    assert result["conditions"]["total_count"] == 7
    assert {row["native_id"] for row in result["records"]} == set(THEME_NAMES)
    assert result["tool_trace"][-1]["catalog_truncated"] is False
    assert_originals(result, knowledge)


@pytest.mark.parametrize("alias,theme", [("傀影", "rogue_1"), ("水月", "rogue_2"),
                                        ("萨米", "rogue_3"), ("萨卡兹", "rogue_4"),
                                        ("界园", "rogue_5"), ("黑流", "rogue_6"),
                                        ("刻俄柏", LEGACY_THEME)])
def test_aliases_require_roguelike_context_and_isolate_same_name(make_knowledge, alias, theme):
    knowledge = make_knowledge()
    assert roguelike.is_roguelike_question(alias + "技能原文") is False
    assert roguelike.is_roguelike_question(alias + "肉鸽") is True
    result = ask(knowledge, f"{alias}肉鸽指挥分队原文")
    assert result["status"] == "answered"
    assert result["conditions"]["theme_ids"] == [theme]
    assert {row["parent_native_id"] for row in result["records"]} == {theme}
    item = next(row for row in result["records"] if row["kind"] == "roguelike_squad")
    buff = next(row for row in result["records"] if row["kind"] == "roguelike_buff")
    assert "usage" in item["data"] and "buffs" not in item["data"]
    assert "buffs" in buff["data"] and "usage" not in buff["data"]
    assert "null（原件未提供）" in result["answer"]
    assert_originals(result, knowledge)


def test_catalog_over_200_retains_every_object_and_all_original_fields(make_knowledge):
    knowledge = make_knowledge(extra_relics=205)
    result = ask(knowledge, "傀影肉鸽全部藏品原文目录")
    assert result["status"] == "answered"
    assert result["conditions"]["total_count"] == 207
    relics = [row for row in result["records"] if row["kind"] == "roguelike_relic"]
    assert len(relics) == 207
    assert {row["data"]["id"] for row in relics} >= {f"extra_{number}" for number in range(205)}
    assert all(any(claim["record_id"] == row["id"] and claim["label"] == "description"
                   and claim["value"] is None for claim in result["claims"]) for row in relics)
    assert_originals(result, knowledge)


@pytest.mark.parametrize("kind,query", [
    ("ending", "结局一原文"), ("node", "节点诡意行商原文"), ("stage", "关卡测试关卡原文"),
    ("zone", "区域测试区域原文"), ("event", "事件测试事件原文"), ("choice", "选项测试选项原文"),
    ("initial", "初始配置NORMAL原文"), ("rule", "机制原文机制原文"),
])
def test_unique_source_names_cover_each_document_type(make_knowledge, kind, query):
    knowledge = make_knowledge()
    result = ask(knowledge, "水月肉鸽" + query)
    assert result["status"] == "answered"
    assert result["conditions"]["record_kind"] == "roguelike_" + kind
    assert_originals(result, knowledge)


def test_source_title_owns_boundary_and_other_theme_alias_words(make_knowledge):
    knowledge = make_knowledge()
    result = ask(knowledge, "水月肉鸽藏品叠加萨卡兹概率的原文")
    assert result["status"] == "answered"
    assert result["theme_id"] == "rogue_2"
    assert result["conditions"]["total_count"] == 1
    assert_originals(result, knowledge)


@pytest.mark.parametrize("query,theme", [
    ("傀影与猩红孤钻指挥分队原文", "rogue_1"),
    ("刻俄柏的灰蕈迷境指挥分队原文", LEGACY_THEME),
    ("探索者的银凇止境指挥分队原文", "rogue_3"),
    ("水月肉鸽指挥分队原文", "rogue_2"),
    ("肉鸽水月指挥分队原文", "rogue_2"),
])
def test_short_client_item_titles_cannot_consume_theme_subjects(make_knowledge, query, theme):
    knowledge = make_knowledge()
    result = ask(knowledge, query)
    assert result["status"] == "answered"
    assert result["theme_id"] == theme
    assert {row["parent_native_id"] for row in result["records"]} == {theme}


def test_same_name_difficulty_conditions_must_be_explicit(make_knowledge):
    knowledge = make_knowledge()
    ambiguous = ask(knowledge, "水月肉鸽同名难度原文")
    assert ambiguous["status"] == "needs_clarification"
    result = ask(knowledge, "水月肉鸽难度1原文")
    assert result["status"] == "answered"
    assert result["conditions"]["difficulty_grade"] == 1
    assert result["records"][0]["data"]["grade"] == 1
    assert_originals(result, knowledge)


@pytest.mark.parametrize("query", [
    "水月肉鸽共享藏品和指挥分队原文", "水月肉鸽藏品和结局目录",
    "水月肉鸽藏品共享藏品和指挥分队原文",
    "水月肉鸽共享藏品和不存在的对象原文", "水月肉鸽「不在来源的藏品」原文",
    "水月肉鸽主题原文和未支持的问题", "rogue_10主题原文",
    "水月与傀影肉鸽指挥分队原文", "指挥分队原文", "水月技能原文",
])
def test_multi_request_unknown_literal_or_missing_theme_never_loses_a_request(make_knowledge, query):
    knowledge = make_knowledge()
    result = ask(knowledge, query)
    assert result["status"] == "needs_clarification"
    assert result["claims"] == result["citations"] == result["records"] == []


def test_ambiguous_exact_name_requires_unique_native_identity(make_knowledge):
    knowledge = make_knowledge(ambiguous=True)
    result = ask(knowledge, "傀影肉鸽共享藏品原文")
    assert result["status"] == "needs_clarification"
    ident = "rogue_1:/details/rogue_1/items/relic_a"
    result = ask(knowledge, f"傀影肉鸽藏品{ident}原文")
    assert result["status"] == "answered"
    assert result["conditions"]["total_count"] == 1
    assert_originals(result, knowledge)


def test_original_client_id_is_matched_inside_only_the_selected_theme(make_knowledge):
    knowledge = make_knowledge()
    result = ask(knowledge, "水月肉鸽分队band_a原文")
    assert result["status"] == "answered"
    assert result["conditions"]["total_count"] == 1
    assert {row["parent_native_id"] for row in result["records"]} == {"rogue_2"}
    assert_originals(result, knowledge)


@pytest.mark.parametrize("query", ["rogue_1主题原文", "act12d6主题原文", "rogue_6_band_1原文"])
def test_native_theme_and_mode_ids_are_explicit_public_routing_context(query):
    assert roguelike.is_roguelike_question(query) is True


def test_unique_full_native_id_resolves_its_verified_theme_without_a_nickname(make_knowledge):
    knowledge = make_knowledge()
    result = ask(knowledge, "rogue_1:/details/rogue_1/items/relic_a 原文")
    assert result["status"] == "answered"
    assert result["theme_id"] == "rogue_1"
    assert_originals(result, knowledge)


@pytest.mark.parametrize("query", [
    "水月肉鸽共享藏品的实际概率是多少", "萨米肉鸽原文机制如何叠加",
    "傀影肉鸽结局一怎么解锁", "黑流肉鸽推荐配队", "最新肉鸽是什么",
    "水月肉鸽共享藏品现在还能获得吗", "界园肉鸽原文机制实战是否生效",
    "萨卡兹肉鸽保证获得共享藏品的方法", "水月肉鸽结局一攻略路线",
])
def test_current_game_state_runtime_and_routes_are_not_inferred(make_knowledge, query):
    knowledge = make_knowledge()
    result = ask(knowledge, query)
    assert result["status"] == "unsupported"
    assert result["claims"] == result["citations"] == []


def test_old_release_and_absent_store_have_no_cross_release_fallback(make_knowledge):
    knowledge = make_knowledge()
    old = SimpleNamespace(id="old_release", manifest={"datasets": {}}, manifest_hash="old_hash")
    result = roguelike.answer_roguelike(knowledge.session, old, "肉鸽主题目录", store=knowledge.store)
    assert result["status"] == "unsupported"
    result = roguelike.answer_roguelike(knowledge.session, knowledge.release, "肉鸽主题目录")
    assert result["status"] == "needs_verification"
    assert result["claims"] == result["citations"] == []


@pytest.mark.parametrize("corruption", ["partial_record", "partial_evidence", "source_bytes", "source_size", "wrong_revision", "wrong_parent", "wrong_name", "wrong_kind"])
def test_complete_parent_source_and_revision_integrity_are_required(make_knowledge, monkeypatch, corruption):
    knowledge = make_knowledge()
    original_get = services.get_record
    if corruption in {"partial_record", "wrong_parent", "wrong_name", "wrong_kind"}:
        def record(session, release, ident):
            result = copy.deepcopy(original_get(session, release, ident))
            if result["kind"] == "roguelike_squad" and result["parent_native_id"] == "rogue_2":
                if corruption == "partial_record":
                    result["data"] = {"usage": result["data"]["usage"]}
                elif corruption == "wrong_parent":
                    result["parent_native_id"] = "rogue_1"
                elif corruption == "wrong_name":
                    result["name"] = "被污染的别名"
                else:
                    result["kind"] = "roguelike_relic"
            return result
        monkeypatch.setattr(services, "get_record", record)
    elif corruption == "partial_evidence":
        original = services.get_evidence
        def evidence(session, release, ident):
            result = copy.deepcopy(original(session, release, ident))
            if result["pointer"].endswith("/items/band_a"):
                result["excerpt"] = {"usage": result["excerpt"]["usage"]}
                result["excerpt_hash"] = services.content_digest(result["excerpt"])
            return result
        monkeypatch.setattr(services, "get_evidence", evidence)
    elif corruption in {"source_size", "wrong_revision"}:
        original = services.get_snapshot
        def snapshot(session, release, ident):
            result = copy.deepcopy(original(session, release, ident))
            result["byte_size" if corruption == "source_size" else "revision"] = 0 if corruption == "source_size" else "a" * 40
            return result
        monkeypatch.setattr(services, "get_snapshot", snapshot)
    elif corruption == "source_bytes":
        for path in knowledge.store.root.rglob("*"):
            if path.is_file():
                path.write_bytes(b"tampered")
    result = ask(knowledge, "水月肉鸽指挥分队原文")
    assert result["status"] == "needs_verification"
    assert result["claims"] == result["citations"] == result["records"] == []


def test_parent_pointer_cannot_certify_subset_or_python_equal_other_json_type(make_knowledge):
    knowledge = make_knowledge()
    record = next(row for row in services.list_records(knowledge.session, knowledge.release,
                  module="roguelike", kind="roguelike_squad", limit=200)
                  if row["parent_native_id"] == "rogue_2")
    answer = roguelike._Answer(knowledge.session, knowledge.release, "test", knowledge.store)
    with pytest.raises(Conflict):
        answer.cite(record, "", {"usage": record["data"]["usage"]})
    with pytest.raises(Conflict):
        answer.cite(record, "/description", False)
    assert answer.result["citations"] == []


def test_buff_name_is_proved_by_a_separate_matching_item_record(make_knowledge):
    knowledge = make_knowledge()
    result = ask(knowledge, "傀影肉鸽共享藏品buff原文")
    assert result["status"] == "answered"
    assert {row["kind"] for row in result["records"]} == {"roguelike_buff", "roguelike_relic"}
    assert_originals(result, knowledge)
    missing = make_knowledge(missing_buff_item=True)
    result = ask(missing, "傀影肉鸽共享藏品buff原文")
    assert result["status"] == "needs_verification"
    assert result["citations"] == result["claims"] == []
