"""Full model protocol over temporary real sources and simulated billing only."""

import json
from types import SimpleNamespace

import pytest
from test_runtime_runner import call, response
from test_stage15_roguelike_qa import make_knowledge
from test_stage50_relic_effects import clone
from test_stage52_semantic_review import invoke, review, review_data

from arknights_agent import agent
from arknights_agent.config import Settings
from arknights_agent.evaluation.evidence import EvidenceContext
from arknights_agent.runtime.tools import ToolContext, execute_tool

__all__ = ["make_knowledge"]
QUESTION = "肉鸽难度超过11级后，花队还能电宝倒转吗？请从遗物角度分析"


@pytest.fixture
def knowledge(make_knowledge):
    k = make_knowledge()
    k.context = SimpleNamespace(session=k.session, store=k.store, release=k.release,
        settings=Settings(_env_file=None, database_url=str(k.session.get_bind().url),
                          snapshot_root=k.store.root, model_live_enabled=False))
    return k


def search_call(query="共享藏品 完整效果", ident="search", **changes):
    return response([call("analysis_search", {"query": query, "theme_id": "rogue_5",
                                              "limit": 3, **changes}, ident)])


def last_hits(wire):
    for message in reversed(wire["messages"]):
        if message["role"] != "tool":
            continue
        result = json.loads(message["content"])
        if result.get("ok") and "hits" in result.get("data", {}):
            return result["data"]["hits"]
    raise AssertionError("Provider must actually receive the source search result")


def args_for(hit, question=QUESTION, *, current_index=0, previous_quotes=()):
    return {"theme_id": "rogue_5", "request_summary": "分析该构筑在超过11级的难度范围，保留遗物角度与未知机制",
        "request_quotes": [*previous_quotes, {"question_index": current_index, "quote": question}],
        "observations": [{"evidence_ref": hit["evidence_ref"], "quote": hit["quote"]}],
        "analysis": [{"text": "上述效果提供了资料依据；它没有证明难度超过11后是否禁止该循环，不能据此下绝对结论。",
                      "evidence_refs": [hit["evidence_ref"]]}],
        "limitations": ["缺少循环触发与高难适用机制的证据，暂时不能核实该阈值结论。"]}


def answer_after_search(wire, *, saved=None, question=QUESTION, current_index=0, previous_quotes=()):
    hits = last_hits(wire)
    hit = next((h for h in hits if "完整效果" in h["quote"]), hits[0])
    args = args_for(hit, question, current_index=current_index, previous_quotes=previous_quotes)
    if saved is not None:
        saved.update(args)
    return response([call("evidence_analysis", args, "analysis")])


def assert_reviewed(answer, count):
    trace = answer["agent_runtime"]
    assert trace["simulation"] is True and trace["used"] == "deepseek_simulation"
    assert trace["model_calls"] == trace["budget"]["settled_count"] == count
    assert trace["reason"] == "source_verified_query_semantically_reviewed"
    assert trace["submission_mode"] == "verified_query_semantic_review"
    assert trace["server_committed"] and trace["semantic_reviews"][-1]["decision"] == "accept"
    assert trace["staged_source_answers"][-1]["committed_at_staging"] is False


def test_analysis_search_answer_and_independent_accept_replay_real_sources(knowledge, tmp_path, monkeypatch):
    k = knowledge
    monkeypatch.setattr(agent, "dispatch_question", lambda *_a, **_k: pytest.fail("No local oral parser"))
    answer, wire, _ = invoke(k, tmp_path, [search_call(), answer_after_search, review], question=QUESTION)
    assert_reviewed(answer, 3)
    assert len(wire) == 3 and answer["dialogue"]["intent"] == "evidence_analysis"
    assert answer["claims"] and EvidenceContext(k.session, k.release, k.store).audit(
        answer["citations"], "roguelike").passed
    assert all(claim["value"] == observation["quote"]
               for claim, observation in zip(answer["claims"], answer["source_observations"], strict=True))
    staged = review_data(wire[2])
    assert staged["model_analysis"] and staged["source_observations"]
    assert staged["source_authority"]["model_inferences_source_verified"] is False
    assert staged["source_authority"]["inference_entailment_requires_model_review"] is True
    assert "难度超过11" in staged["analysis_request"]["original_user_questions"][0]
    assert [s["function"]["name"] for s in wire[2]["tools"]] == ["review_source_answer"]
    assert wire[0]["messages"][1] == wire[1]["messages"][1] == wire[2]["messages"][1]


def make_clarification(k, folder):
    first = response([call("roguelike_query", {"interpretation": {"intent": "clarify",
                        "conditions": [], "unresolved": []}}, "clarify")])
    answer, _, _ = invoke(k, folder, [first, review], question=QUESTION)
    assert answer["status"] == "needs_clarification" and not answer["claims"]
    saved = agent.record_run(k.session, k.release, QUESTION, answer)
    k.session.commit()
    return saved


def test_real_theme_clarification_followup_keeps_complete_task_and_range(knowledge, tmp_path):
    k = knowledge
    parent = make_clarification(k, tmp_path / "first")
    question = "界园"
    def propose(wire):
        return answer_after_search(wire, question=question, current_index=1,
            previous_quotes=[{"question_index": 0, "quote": QUESTION}])
    answer, wire, _ = invoke(k, tmp_path / "second", [search_call(), propose, review],
        question=question, previous_run_id=parent["run_id"])
    assert_reviewed(answer, 3)
    assert answer["analysis_request"]["original_user_questions"] == [QUESTION, question]
    assert answer["dialogue_state"]["previous_run_id"] == parent["run_id"]
    assert answer["dialogue"]["entity_native_id"] is None
    for payload in wire:
        envelope = json.loads(json.loads(payload["messages"][1]["content"])["question"])
        assert envelope == {"current_question": question, "previous_questions": [QUESTION]}
    staged = review_data(wire[2])
    assert "超过11" in json.dumps(staged["analysis_request"], ensure_ascii=False)
    assert "难度12" not in json.dumps(answer, ensure_ascii=False)


@pytest.mark.parametrize("searched", [False, True])
def test_unregistered_or_forged_evidence_never_stages_or_commits(knowledge, tmp_path, searched):
    fake = {"evidence_ref": "analysis_evidence_model_invented", "quote": "伪造证据"}
    def forged(_wire):
        return response([call("evidence_analysis", args_for(fake), "forged")])
    replies = ([search_call()] if searched else []) + [forged, response(content="stop")]
    answer, wire, _ = invoke(knowledge, tmp_path, replies, question=QUESTION)
    trace = answer["agent_runtime"]
    assert not trace["server_committed"] and answer["claims"] == answer["citations"] == []
    assert not trace.get("staged_source_answers") and not trace.get("semantic_reviews")
    assert "analysis_unknown_evidence_reference" in wire[-1]["messages"][-1]["content"]
    assert "伪造证据" not in answer["answer"]


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("tool_name", ["analysis_search", "evidence_analysis"])
def test_forced_analysis_tools_are_unavailable_without_review_or_in_legacy(
    knowledge, tmp_path, legacy, tool_name
):
    changes = {"agent_semantic_review": False, "agent_max_model_calls": 2,
               "agent_roguelike_delivery": "legacy_submit" if legacy else "verified_query"}
    forced_args = ({"query": "共享藏品"} if tool_name == "analysis_search" else
                   args_for({"evidence_ref": "unregistered", "quote": "fake"}))
    forced = response([call(tool_name, forced_args, "forced")])
    answer, wire, cfg = invoke(knowledge, tmp_path, [forced, response(content="stop")],
                              question=QUESTION, **changes)
    assert not answer["agent_runtime"]["server_committed"]
    assert answer["claims"] == answer["citations"] == []
    assert tool_name not in [s["function"]["name"] for s in wire[0]["tools"]]
    assert answer["agent_runtime"]["steps"][0]["status"] == "tool_not_available_in_phase"
    direct = ToolContext(knowledge.session, knowledge.release, knowledge.store, cfg, QUESTION,
                         analysis_evidence={})
    assert execute_tool(tool_name, forced_args, direct)["error"]["code"] == (
        "analysis_requires_semantic_review")


def test_review_rejects_overclaim_then_corrected_limitations_need_new_accept(knowledge, tmp_path):
    saved = {}
    def overclaim(wire):
        result = answer_after_search(wire, saved=saved)
        args = json.loads(result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
        args["analysis"][0]["text"] = "超过11级后绝对无法循环。"
        saved.update(args)
        return response([call("evidence_analysis", args, "overclaim")])
    def corrected(_wire):
        args = {**saved, "analysis": [{"text": "资料未证明该阈值是禁止条件，不能把它作为确定结论。",
                                     "evidence_refs": saved["analysis"][0]["evidence_refs"]}],
                "limitations": ["所引效果文案未说明高难运行阈值；仍需该机制的独立证据。"]}
        return response([call("evidence_analysis", args, "corrected-analysis")])
    answer, wire, _ = invoke(knowledge, tmp_path, [search_call(), overclaim,
        lambda w: review(w, "revise", reason="引文不证明高难阈值，必须撤去绝对结论并明确未核实。"),
        corrected, review], question=QUESTION)
    assert_reviewed(answer, 5)
    assert [r["decision"] for r in answer["agent_runtime"]["semantic_reviews"]] == ["revise", "accept"]
    assert "绝对无法循环" not in answer["answer"] and "独立证据" in answer["answer"]
    assert review_data(wire[2])["answer_sha256"] != review_data(wire[4])["answer_sha256"]
    assert "actual_model_semantic_review_feedback_data_only" in wire[3]["messages"][-2]["content"]


def test_explicit_new_question_keeps_history_but_does_not_force_old_analysis(knowledge, tmp_path):
    k = knowledge
    parent = make_clarification(k, tmp_path / "first")
    question = "不聊刚才那队了，换题：只根据指挥分队的原文分析用途"
    def propose(wire):
        hit = next(h for h in last_hits(wire) if "完整效果" in h["quote"])
        args = args_for(hit, question, current_index=1)
        args.update(request_summary="换题，仅按指挥分队的资料解释用途；旧构筑任务已撤回",
                    analysis=[{"text": "该分队的用途可从所引效果描述理解，未验证运行机制。",
                               "evidence_refs": [hit["evidence_ref"]]}], limitations=[])
        return response([call("evidence_analysis", args, "new-topic")])
    answer, wire, _ = invoke(k, tmp_path / "second", [search_call("指挥分队 完整效果"), propose, review],
                              question=question, previous_run_id=parent["run_id"])
    assert_reviewed(answer, 3)
    assert answer["analysis_request"]["original_user_questions"] == [QUESTION, question]
    assert answer["analysis_request"]["quotes"] == [{"question_index": 1, "quote": question}]
    assert "超过11" not in answer["answer"]
    assert review_data(wire[2])["analysis_request"]["summary"].endswith("旧构筑任务已撤回")


def test_missing_review_budget_never_delivers_analysis(knowledge, tmp_path):
    answer, wire, _ = invoke(knowledge, tmp_path, [search_call(), answer_after_search],
                            question=QUESTION, agent_max_model_calls=1)
    assert len(wire) == 0 and not answer["agent_runtime"]["server_committed"]
    assert answer["claims"] == answer["citations"] == []
    assert not answer["agent_runtime"].get("staged_source_answers")
    assert not answer["agent_runtime"].get("semantic_reviews")
    assert answer["agent_runtime"]["model_calls"] == 0


def test_long_chinese_quotes_fail_explicitly_without_review_or_condition_deletion(knowledge, tmp_path):
    k = clone(knowledge, usage="仅当所有前置条件满足时才适用。" + "长中文原始效果文本。" * 180)
    k.context.release = k.release
    def oversized(wire):
        hit = next(h for h in last_hits(wire) if "长中文" in h["quote"])
        args = args_for(hit)
        args["observations"] *= 12
        return response([call("evidence_analysis", args, "oversized-analysis")])
    answer, wire, _ = invoke(k, tmp_path, [search_call("共享藏品 长中文", limit=1), oversized,
        response(content="stop")], question=QUESTION)
    assert not answer["agent_runtime"]["server_committed"]
    assert answer["claims"] == answer["citations"] == []
    assert not answer["agent_runtime"].get("semantic_reviews")
    error = json.loads(wire[-1]["messages"][-1]["content"])["error"]
    assert error["code"] == "query_interpretation_rejected"
    assert error["repair"]["reason"] == "analysis_output_too_large"
    assert "保留必要前置条件" in error["repair"]["instruction"]
    assert json.loads(wire[-1]["messages"][1]["content"])["question"] == QUESTION


def wire_bytes(wire):
    return len(json.dumps(wire, ensure_ascii=False, separators=(",", ":")).encode())


def review_capacity_case(k, folder):
    """Measure real complete provider bodies, without replacing size checks."""
    saved = {}
    def verbose(wire):
        hit = last_hits(wire)[0]
        args = args_for(hit)
        args["analysis"] = [{"text": "基于该资料仍然缺乏运行机制证据。" * 32,
                             "evidence_refs": [hit["evidence_ref"]]}] * 8
        saved.update(hit=hit, arguments=args)
        return response([call("evidence_analysis", args, "verbose")])
    answer, wire, _ = invoke(k, folder, [search_call(limit=1), verbose, review], question=QUESTION)
    assert_reviewed(answer, 3)
    # Its answer fits the tool boundary, while the complete review needs more
    # room than either query. The later failure must therefore be review-only.
    assert wire_bytes({key: value for key, value in answer.items() if key != "agent_runtime"}) < 32768
    query_size, review_size = max(map(wire_bytes, wire[:2])), wire_bytes(wire[2])
    assert query_size < review_size
    return verbose, saved, (query_size + review_size) // 2, wire_bytes(wire[0])


def review_capacity_feedback(wire):
    retained = next(json.loads(message["content"])["analysis_repair_data_only"]
                    for message in wire["messages"] if message["role"] == "assistant"
                    and "analysis_repair_data_only" in message["content"])
    assert retained["repair"]["reason"] == "analysis_review_context_too_large"
    assert retained["answer_not_delivered"] is True
    instructions = "\n".join(message["content"] for message in wire["messages"]
                             if message["role"] == "system")
    assert "保留完整原任务和范围" in instructions and "不得截断否定条件" in instructions
    public = json.loads(wire["messages"][1]["content"])
    assert public["question"] == QUESTION


def test_review_capacity_rejection_is_visible_and_never_stages_or_delivers(knowledge, tmp_path):
    verbose, _, maximum, initial_size = review_capacity_case(knowledge, tmp_path / "measurement")
    assert maximum > initial_size
    def stop_after_feedback(wire):
        review_capacity_feedback(wire)
        return response(content="资料超过完整复核容量，停止，不能跳过核验。")
    answer, wire, _ = invoke(knowledge, tmp_path / "limited", [search_call(limit=1), verbose,
        stop_after_feedback], question=QUESTION, agent_max_prompt_bytes=maximum)
    assert len(wire) == 3
    assert not answer["agent_runtime"]["server_committed"]
    assert not answer["agent_runtime"].get("staged_source_answers")
    assert not answer["agent_runtime"].get("semantic_reviews")
    assert answer["claims"] == answer["citations"] == []
    assert "analysis_review_context_too_large" in [step["status"] for step in answer["agent_runtime"]["steps"]]


def test_model_can_shorten_legal_observation_after_capacity_feedback_and_still_needs_review(knowledge, tmp_path):
    verbose, saved, maximum, initial_size = review_capacity_case(knowledge, tmp_path / "measurement")
    assert maximum > initial_size
    def corrected(wire):
        review_capacity_feedback(wire)
        assert saved["hit"]["evidence_ref"] in json.dumps(wire["messages"], ensure_ascii=False)
        args = args_for(saved["hit"])
        assert "完整效果" in saved["hit"]["quote"]
        args["observations"][0]["quote"] = "完整效果"
        return response([call("evidence_analysis", args, "smaller-analysis")])
    answer, wire, _ = invoke(knowledge, tmp_path / "limited", [search_call(limit=1), verbose,
        corrected, review], question=QUESTION, agent_max_prompt_bytes=maximum)
    assert_reviewed(answer, 4)
    assert len(wire) == 4 and all(wire_bytes(w) <= maximum for w in wire)
    trace = answer["agent_runtime"]
    assert len(trace["staged_source_answers"]) == len(trace["semantic_reviews"]) == 1
    assert trace["staged_source_answers"][0]["answer_ref"] == "staged_answer_1"
    assert answer["source_observations"][0]["quote"] == "完整效果"
    assert answer["analysis_request"]["original_user_questions"] == [QUESTION]
    assert "超过11" in answer["analysis_request"]["quotes"][0]["quote"]
    assert EvidenceContext(knowledge.session, knowledge.release, knowledge.store).audit(
        answer["citations"], "roguelike").passed
