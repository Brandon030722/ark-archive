"""Ordinary relic-effect requests use raw sources, never the oral parser."""

import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_runtime_runner import call, context, response
from test_stage15_roguelike_qa import assert_originals, make_knowledge

from arknights_agent import agent, models, roguelike, roguelike_dialogue, services
from arknights_agent.config import Settings
from arknights_agent.errors import Conflict, InvalidData
from arknights_agent.evaluation.evidence import EvidenceContext
from arknights_agent.ingestion.releases import export_module_batch
from arknights_agent.roguelike_relic_effects import answer_relic_effects
from arknights_agent.runtime.context_profile import MAX_WIRE_BYTES
from arknights_agent.runtime.dialogue_context import attach_state
from arknights_agent.runtime.runner import run_chat

__all__ = ["make_knowledge"]


def args(**extra):
    return {"intent": "relic_effect", "entity_name": "共享藏品", "reference": "current",
            "theme_scope": "all", "theme_id": None, "conditions": [], "unresolved": [], **extra}


def ask(k, proposal=None, question="共享藏品在各主题的作用", previous=None):
    result = answer_relic_effects(k.session, k.release, question, proposal or args(),
                                  store=k.store, previous_answer=previous)
    attach_state(result, question, previous, previous.get("run_id") if previous else None)
    return result


def clone(k, *, remove=(), usage=None, name=None):
    """Publish an isolated source epoch with raw and normalized changes together."""
    batch = export_module_batch(k.session, k.release, "roguelike", k.store)
    docs = {s.source_key: json.loads(s.content) for s in batch.snapshots}
    records = []
    for r in batch.records:
        if r.payload.get("id") != "relic_a":
            records.append(r)
            continue
        table_path, _, token = r.pointer.rpartition("/")
        table = services.json_pointer(docs[r.source_key], table_path)
        if r.parent_native_id in remove:
            del table[token]
            continue
        if usage is not None and r.kind == "roguelike_relic":
            table[token]["usage"] = usage
            r = replace(r, payload=copy.deepcopy(table[token]))
        if name is not None:
            if r.kind == "roguelike_relic":
                table[token]["name"] = name
                r = replace(r, payload=copy.deepcopy(table[token]))
            r = replace(r, name=name)
        records.append(r)
    snapshots = [replace(s, content=json.dumps(docs[s.source_key], ensure_ascii=False).encode())
                 for s in batch.snapshots]
    ds = services.ingest_batch(k.session, replace(batch, records=records, snapshots=snapshots), k.store)
    k.release = services.publish_release(k.session, {"roguelike": ds["roguelike"].id}, store=k.store)
    k.session.commit()
    return k


def test_all_themes_render_actual_usage_and_do_not_count_buff_as_second_relic(make_knowledge, monkeypatch):
    k = make_knowledge()
    def forbidden(*_a, **_k):
        raise AssertionError("No local language parser")
    monkeypatch.setattr(roguelike, "answer_roguelike", forbidden)
    monkeypatch.setattr(roguelike_dialogue, "answer_roguelike_dialogue", forbidden)
    result = ask(k)
    assert result["status"] == "answered" and len(result["records"]) == 7
    assert len(result["claims"]) == len(result["citations"]) == 7
    assert all(r["kind"] == "roguelike_relic" for r in result["records"])
    assert result["relic_effects"]["all_requested_effects_available"] is True
    assert len(result["relic_effects"]["catalog_proofs"]) == 7
    assert all(t["complete_relic_membership_verified"] for t in result["relic_effects"]["catalog_proofs"])
    assert "<@rogue.attention>" not in result["answer"]
    assert "<@rogue.attention>" in result["claims"][0]["value"]
    assert_originals(result, k)


def test_five_positive_themes_and_two_actual_catalog_gaps_are_explicit(make_knowledge):
    k = clone(make_knowledge(), remove=("rogue_2", "act12d6"), usage="每有5源石锭，所有我方单位的攻击速度+7")
    result = ask(k)
    assert result["status"] == "answered"
    assert result["relic_effects"]["available_theme_count"] == 5
    assert result["relic_effects"]["all_requested_effects_available"] is False
    gaps = [t["theme_id"] for t in result["relic_effects"]["themes"] if t["status"] == "not_in_fixed_catalog"]
    assert set(gaps) == {"rogue_2", "act12d6"}
    assert len(result["claims"]) == 5 and "未收录不代表游戏中不存在" in result["answer"]
    assert "原始ID" not in result["answer"] and "攻击速度+7" in result["answer"]
    assert_originals(result, k)


def test_all_to_short_theme_followup_keeps_named_group_and_reports_real_missing_source(make_knowledge):
    k = clone(make_knowledge(), remove=("rogue_2", "act12d6"))
    first = ask(k)
    frozen = copy.deepcopy(first)
    second = ask(k, args(entity_name=None, theme_scope="single", theme_id="rogue_2",
                         theme_evidence="水月", reference="previous_entity"), "水月", first)
    assert first == frozen
    assert second["status"] == "needs_verification" and not second["claims"] and not second["citations"]
    assert second["dialogue"]["entity_name"] == "共享藏品" and second["dialogue"]["context_used"] is True
    assert "水月与深蓝之树" in second["answer"] and "未收录" in second["answer"]
    assert "不能套用其他主题" in second["answer"] and "ID" not in second["answer"]
    third = ask(k, args(entity_name=None, theme_scope="single", theme_id="rogue_3", reference="previous_entity"),
                "那银凇呢", second)
    assert third["status"] == "answered" and len(third["records"]) == 1
    assert third["records"][0]["parent_native_id"] == "rogue_3"
    assert_originals(third, k)


def test_complete_ordinary_name_and_theme_returns_source_gap_without_requesting_internal_id(make_knowledge):
    k = clone(make_knowledge(), remove=("rogue_2",))
    result = ask(k, args(theme_scope="single", theme_id="rogue_2"), "水月肉鸽的共享藏品")
    assert result["status"] == "needs_verification" and not result["claims"]
    assert "无法核实该主题效果" in result["answer"] and "ID" not in result["answer"]


def test_single_theme_uses_its_own_actual_usage_not_another_theme(make_knowledge):
    k = make_knowledge()
    first = ask(k)
    result = ask(k, args(entity_name=None, reference="previous_entity", theme_scope="single", theme_id="rogue_3"),
                 "只说银凇", first)
    assert result["status"] == "answered" and len(result["claims"]) == 1
    assert result["claims"][0]["value"] == "<3>完整效果 <@rogue.attention>+3%</>"
    assert result["dialogue"]["entity_native_id"].startswith("rogue_3:")


def test_current_missing_name_is_not_extracted_from_raw_question(make_knowledge):
    k = make_knowledge()
    result = ask(k, args(entity_name=None), "共享藏品在每期的作用")
    assert result["status"] == "needs_verification" and not result["claims"]


def test_previous_unselected_candidates_never_select_first_named_group(make_knowledge):
    k = make_knowledge()
    old = ask(k)
    old["candidates"] = [{"number": 1, "native_id": old["records"][0]["native_id"]}]
    result = ask(k, args(reference="previous_entity", entity_name=None), "接着说", old)
    assert result["status"] == "needs_clarification" and not result["claims"]


@pytest.mark.parametrize("mutation", ["target_id", "data_hash", "theme", "context_release", "source_name"])
def test_named_group_cannot_inherit_forged_or_changed_source_metadata(make_knowledge, mutation):
    k = make_knowledge()
    old = ask(k)
    if mutation == "context_release":
        old["named_relic_context"]["release_id"] = "another"
    elif mutation == "source_name":
        old["named_relic_context"]["name"] = "另一个藏品"
    else:
        key = {"target_id": "native_id", "data_hash": "data_sha256", "theme": "theme_id"}[mutation]
        old["named_relic_context"]["targets"][0][key] = "forged"
    if mutation in {"context_release", "source_name"}:
        result = ask(k, args(reference="previous_entity", entity_name=None), "继续", old)
        assert result["status"] == "needs_clarification" and not result["claims"]
    else:
        with pytest.raises(Conflict):
            ask(k, args(reference="previous_entity", entity_name=None), "继续", old)


@pytest.mark.parametrize("field", ["release_id", "manifest_hash"])
def test_previous_cross_release_or_manifest_never_inherits(make_knowledge, field):
    k = make_knowledge()
    old = ask(k)
    old[field] = "other"
    with pytest.raises(InvalidData):
        ask(k, args(reference="previous_entity", entity_name=None), "水月", old)


def test_previous_source_record_data_cannot_change_even_with_valid_named_context(make_knowledge):
    k = make_knowledge()
    old = ask(k)
    old["records"][0]["data"]["usage"] = "编造效果"
    with pytest.raises(Conflict):
        ask(k, args(reference="previous_entity", entity_name=None), "继续", old)


def test_real_same_kind_ambiguity_is_not_hidden_by_first_match(make_knowledge):
    k = make_knowledge(ambiguous=True)
    result = ask(k, args(theme_scope="single", theme_id="rogue_1"))
    assert result["status"] == "needs_verification" and not result["claims"]
    assert result["relic_effects"]["themes"][0]["status"] == "same_kind_source_ambiguity"


def test_missing_original_usage_does_not_become_empty_positive_answer(make_knowledge):
    k = clone(make_knowledge(), usage="")
    result = ask(k)
    assert result["status"] == "needs_verification" and not result["claims"]
    assert all(x["status"] == "usage_not_recorded" for x in result["relic_effects"]["themes"])
    assert "原始效果文案缺失" in result["answer"]


def test_unknown_condition_is_preserved_quoted_and_never_answered_as_known_fact(make_knowledge):
    k = make_knowledge()
    p = args(conditions=[{"type": "unknown", "name": None, "value": None, "quote": "有蓝色限制"}])
    raw = copy.deepcopy(p)
    result = ask(k, p, "共享藏品有蓝色限制")
    assert p == raw and result["status"] == "needs_clarification" and not result["claims"]
    assert "「有蓝色限制」" in result["answer"]
    state = result["dialogue"]["pending_user_reported_conditions"]
    assert state[0]["type"] == "unknown" and state[0]["raw_text"] == "有蓝色限制" and state[0]["citation_ids"] == []


def test_unresolved_preserves_literal_quote_not_generic_clarification(make_knowledge):
    k = make_knowledge()
    result = ask(k, args(unresolved=["蓝色轮换"]), "共享藏品在蓝色轮换的效果")
    assert result["status"] == "needs_clarification" and "「蓝色轮换」" in result["answer"]
    assert not result["claims"]


def test_known_state_remains_user_only_no_dynamic_mechanic_is_inferred(make_knowledge):
    k = make_knowledge()
    result = ask(k, args(theme_scope="single", theme_id="rogue_3", conditions=[
        {"type": "floor", "name": None, "value": 8, "quote": "8层"}]), "共享藏品在8层有什么效果")
    assert result["status"] == "answered" and "没有据此推算实际触发结果" in result["answer"]
    assert result["dialogue"]["user_reported_conditions"][0]["citation_ids"] == []
    assert result["conditions"]["runtime_mechanics_verified"] is False


@pytest.mark.parametrize("extra", [
    {"theme_scope": "all", "theme_id": "rogue_2"},
    {"theme_scope": "unknown"},
    {"theme_id": "rogue_99"},
    {"entity_evidence": "另一个未出现的引文"},
    {"conditions": [{"type": "floor", "name": None, "value": True, "quote": "共享藏品"}]},
    {"unresolved": ["模型自行总结"]},
])
def test_invalid_scope_quote_and_bool_as_number_fail_closed(make_knowledge, extra):
    k = make_knowledge()
    with pytest.raises(InvalidData):
        ask(k, args(**extra))


def test_raw_table_and_normalized_catalog_disagreement_fails_before_absence_claim(make_knowledge):
    k = make_knowledge()
    record = k.session.scalar(select(models.KnowledgeRecord).where(
        models.KnowledgeRecord.dataset_id == k.release.manifest["datasets"]["roguelike"]["dataset_id"],
        models.KnowledgeRecord.kind == "roguelike_relic"))
    record.native_id += "_forged"
    k.session.flush()
    with pytest.raises(Conflict):
        ask(k)


def test_raw_snapshot_tampering_cannot_be_presented_as_source_gap(make_knowledge):
    k = make_knowledge()
    snapshots = [services.get_snapshot(k.session, k.release, ident)
                 for ident in k.release.manifest["datasets"]["roguelike"]["source_snapshot_ids"]]
    snapshot = next(s for s in snapshots if s["source_key"].endswith("roguelike_topic_table.json"))
    k.store.path_for(snapshot["content_hash"]).write_bytes(b"corrupt")
    with pytest.raises((Conflict, InvalidData)):
        ask(k)


@pytest.mark.parametrize("field", ["snapshot_hash", "field_hash", "record_id", "field_value"])
def test_previous_usage_citation_is_rechecked_not_just_named_context(make_knowledge, field):
    k = make_knowledge()
    old = ask(k)
    old["citations"][0][field] = "forged"
    with pytest.raises(Conflict):
        ask(k, args(reference="previous_entity", entity_name=None), "继续", old)


def test_full_identity_conflicting_with_typed_theme_does_not_retarget_by_group_name(make_knowledge):
    k = make_knowledge()
    native = "rogue_1:/details/rogue_1/items/relic_a"
    result = ask(k, args(entity_name=native, theme_scope="single", theme_id="rogue_2"), native)
    assert result["status"] == "needs_clarification" and not result["claims"]
    assert result["dialogue"]["proposal_repair_request"]["code"] == "reference_theme_mismatch"


def test_exact_three_human_questions_with_synthetic_model_wire_and_real_temporary_parent_audits(
    make_knowledge, monkeypatch, tmp_path
):
    k = clone(make_knowledge(), remove=("rogue_2", "act12d6"), name="金酒之杯",
              usage="每有5源石锭，所有我方单位的攻击速度+7")
    fixture = SimpleNamespace(context=SimpleNamespace(settings=Settings(
        _env_file=None, database_url=str(k.session.get_bind().url), snapshot_root=k.store.root,
        deepseek_api_key=None, model_live_enabled=False)))
    questions = ["金酒之杯在每个肉鸽的作用", "水月", "水月肉鸽的金酒之杯"]
    proposals = [args(entity_name="金酒之杯", entity_evidence="金酒之杯"),
                 args(entity_name=None, reference="previous_entity", theme_scope="single",
                      theme_id="rogue_2", theme_evidence="水月"),
                 args(entity_name="金酒之杯", reference="current", theme_scope="single",
                      theme_id="rogue_2", theme_evidence="水月", entity_evidence="金酒之杯")]
    previous = None
    captured_wires, answers = [], []
    monkeypatch.setattr(agent, "dispatch_question", lambda *_a, **_k: pytest.fail("local parser"))
    monkeypatch.setattr(roguelike_dialogue, "answer_roguelike_dialogue",
                        lambda *_a, **_k: pytest.fail("local dialogue parser"))
    for index, (question, proposal) in enumerate(zip(questions, proposals, strict=True)):
        cfg, provider, captured = context(fixture, tmp_path, [response([
            call("roguelike_query", {"interpretation": proposal}, f"synthetic-{index}")])],
            agent_max_prompt_bytes=MAX_WIRE_BYTES)
        answer = run_chat(k.session.get_bind(), k.store, cfg, question, release_id=k.release.id,
                          module="roguelike", backend="deepseek", provider=provider,
                          previous_run_id=previous["run_id"] if previous else None)
        assert answer["agent_runtime"]["simulation"] is True
        assert answer["agent_runtime"]["model_calls"] == len(captured) == 1
        assert answer["dialogue_state"]["questions"] == questions[:index + 1]
        assert answer["dialogue_state"]["previous_run_id"] == (previous["run_id"] if previous else None)
        assert "原始ID" not in answer["answer"] and "请明确主题" not in answer["answer"]
        public = json.loads(captured[0]["messages"][1]["content"])
        assert public["module"] == "roguelike" and public["release_id"] == k.release.id
        if index:
            state = json.loads(captured[0]["messages"][2]["content"])["verified_previous_query_context"]
            assert state["named_relic_context"] == previous["named_relic_context"]
            assert state["origin"] == "verified_server_audit"
            assert state["reference_state"] == "named_relic_source_group"
            assert state["entity_native_id"] is None
            bound = json.loads(public["question"])
            assert bound == {"current_question": question, "previous_questions": questions[:index]}
        else:
            assert public["question"] == question
        if index == 0:
            assert answer["status"] == "answered" and answer["relic_effects"]["available_theme_count"] == 5
            assert EvidenceContext(k.session, k.release, k.store).audit(answer["citations"], "roguelike").passed
        else:
            assert answer["status"] == "needs_verification" and not answer["claims"] and not answer["citations"]
            assert "水月与深蓝之树" in answer["answer"] and "无法核实该主题效果" in answer["answer"]
            assert "不能套用其他主题" in answer["answer"]
        answer = agent.record_run(k.session, k.release, question, answer)
        k.session.commit()
        answers.append(answer)
        previous = answer
        captured_wires.extend(captured)
    assert len(answers) == len(captured_wires) == 3
    for wire in captured_wires:
        assert len(json.dumps(wire, ensure_ascii=False, separators=(",", ":")).encode()) <= MAX_WIRE_BYTES
        assert "expectations" not in json.dumps(wire, ensure_ascii=False)


@pytest.mark.parametrize("scope,theme", [("all", None), ("single", "rogue_3")])
def test_all_scope_followup_cannot_bypass_previous_unknown_or_drop_numeric_state(make_knowledge, scope, theme):
    k = make_knowledge()
    first = ask(k, args(conditions=[
        {"type": "unknown", "name": None, "value": None, "quote": "有蓝色限制"},
        {"type": "floor", "name": None, "value": 8, "quote": "8层"},
    ]), "共享藏品有蓝色限制，我在8层")
    frozen = copy.deepcopy(first)
    assert first["status"] == "needs_clarification" and first["named_relic_context"]
    second = ask(k, args(entity_name=None, reference="previous_entity", theme_scope=scope, theme_id=theme),
                 "继续" if scope == "all" else "只说银凇", first)
    assert first == frozen and second["status"] == "needs_clarification" and not second["claims"]
    state = second["dialogue"]["pending_user_reported_conditions"]
    assert [(s["type"], s["value"], s["raw_text"]) for s in state] == [
        ("unknown", None, "有蓝色限制"), ("floor", 8, "8层")]
    assert "「有蓝色限制」" in second["answer"] and all(s["citation_ids"] == [] for s in state)


@pytest.mark.parametrize("scope,theme", [("all", None), ("single", "rogue_3")])
def test_empty_followup_unresolved_array_does_not_resolve_actual_prior_uncertainty(make_knowledge, scope, theme):
    k = make_knowledge()
    first = ask(k, args(unresolved=["蓝色轮换"]), "共享藏品在蓝色轮换的效果")
    assert first["status"] == "needs_clarification" and not first["claims"]
    assert first["dialogue"]["pending_user_reported_conditions"][0]["raw_text"] == "蓝色轮换"
    second = ask(k, args(entity_name=None, reference="previous_entity", theme_scope=scope,
                         theme_id=theme, unresolved=[]), "再说一下", first)
    assert second["status"] == "needs_clarification" and not second["claims"]
    assert "「蓝色轮换」" in second["answer"]
    unknown = second["dialogue"]["pending_user_reported_conditions"]
    assert len(unknown) == 1 and unknown[0]["type"] == "unknown" and unknown[0]["value"] is None
    assert unknown[0]["citation_ids"] == [] and unknown[0]["verification_status"] == "user_reported"
