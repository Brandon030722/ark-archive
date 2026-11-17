"""Independent model review gates immutable source answers; no paid requests."""

import copy
import json
import sqlite3

import httpx
import pytest
from pydantic import ValidationError
from test_recruitment import synthetic_sources as synthetic_sources
from test_runtime_runner import QUESTION, call, context, response
from test_runtime_tools import tool_context as tool_context

from arknights_agent import agent, services
from arknights_agent.config import Settings
from arknights_agent.runtime import runner
from arknights_agent.runtime.budget import RESERVATION_INPUT_TOKENS
from arknights_agent.runtime.providers import DeepSeekProvider

QUERY = {"module": "operator", "intent": "operator_skill", "entity_names": ["陈"],
         "skill_index": 1, "rank": 10}


def review_data(wire):
    return json.loads(wire["messages"][-1]["content"])["staged_source_answer_data_only"]


def review(wire, decision="accept", **changes):
    data = review_data(wire)
    args = {key: data[key] for key in ("answer_ref", "answer_sha256")}
    args.update(decision=decision, reason="完整回答当前问题，无须更改来源事实。")
    args.update(changes)
    return response([call("review_source_answer", args, f"review-{data['answer_ref']}-{decision}")])


def invoke(k, tmp_path, replies, *, previous_run_id=None, question=QUESTION, progress_observer=None, **changes):
    cfg, _, _ = context(k, tmp_path, [], **{
        "agent_semantic_review": True, "agent_max_model_calls": 6, **changes})
    captured = []
    pending = iter(replies)

    def handle(request):
        wire = json.loads(request.content)
        captured.append(wire)
        item = next(pending)
        if callable(item):
            item = item(wire)
        if isinstance(item, Exception):
            raise item
        return httpx.Response(200, json=item)

    provider = DeepSeekProvider(cfg, httpx.MockTransport(handle), simulation=True)
    result = runner.run_chat(k.context.session.get_bind(), k.context.store, cfg, question,
        release_id=k.context.release.id, backend="deepseek", provider=provider,
        previous_run_id=previous_run_id, progress_observer=progress_observer)
    return result, captured, cfg


def query(arguments=None, ident="query"):
    return response([call("source_query", arguments or QUERY, ident)])


def test_review_accept_commits_exact_original_answer_and_accounts_both_calls(tool_context, tmp_path, monkeypatch):
    original = runner.execute_tool
    staged = []

    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[0] == "source_query" and result.get("ok"):
            staged.append(copy.deepcopy(result["data"]))
        return result

    monkeypatch.setattr(runner, "execute_tool", capture)
    answer, wire, cfg = invoke(tool_context, tmp_path, [query(), review])
    trace = answer["agent_runtime"]
    assert trace["semantic_review_enabled"] and trace["execution_model_call_limit"] == 6
    assert trace["reason"] == "source_verified_query_semantically_reviewed"
    assert trace["submission_mode"] == "verified_query_semantic_review"
    assert trace["model_calls"] == trace["tool_calls"] == trace["budget"]["settled_count"] == 2
    assert trace["server_committed"] and trace["model_interpreted"]
    expected_hash = services.content_digest(staged[0])
    assert trace["committed_answer_hash"] == trace["semantic_reviews"][0]["answer_sha256"] == expected_hash
    assert trace["staged_source_answers"][0]["committed_at_staging"] is False
    assert {k: v for k, v in answer.items() if k != "agent_runtime"} == staged[0]
    assert review_data(wire[1])["visible_answer"] == staged[0]["answer"]
    assert "claims" not in review_data(wire[1]) and "citations" not in review_data(wire[1])
    assert wire[0]["messages"][1] == wire[1]["messages"][1]
    assert [t["function"]["name"] for t in wire[1]["tools"]] == ["review_source_answer"]
    with sqlite3.connect(cfg.model_budget_path) as db:
        rows = db.execute("SELECT input_tokens, output_tokens, state FROM reservation").fetchall()
    assert rows == [(RESERVATION_INPUT_TOKENS, 1024, "settled")] * 2


def test_review_revise_requires_new_source_query_then_new_review(tool_context, tmp_path):
    first = {**QUERY, "include_blackboard": True}
    second = {**QUERY, "include_blackboard": False}
    answer, wire, _ = invoke(tool_context, tmp_path, [query(first),
        lambda w: review(w, "revise", reason="用户只问初始技力和消耗，去掉未请求的原始数组。"),
        query(second, "corrected-query"), review])
    trace = answer["agent_runtime"]
    assert trace["model_calls"] == trace["tool_calls"] == trace["budget"]["settled_count"] == 4
    assert [r["decision"] for r in trace["semantic_reviews"]] == ["revise", "accept"]
    staged = trace["staged_source_answers"]
    assert [s["answer_ref"] for s in staged] == ["staged_answer_1", "staged_answer_2"]
    assert staged[0]["answer_sha256"] != staged[1]["answer_sha256"]
    assert trace["committed_answer_hash"] == staged[1]["answer_sha256"]
    assert "actual_model_semantic_review_feedback_data_only" in wire[2]["messages"][-2]["content"]
    assert "用户只问初始技力和消耗" in wire[2]["messages"][-2]["content"]
    assert all(w["messages"][1] == wire[0]["messages"][1] for w in wire)
    assert "blackboard" not in answer["answer"]


def test_review_can_reject_adjacent_facts_for_an_unverifiable_question(tool_context, tmp_path, monkeypatch):
    from arknights_agent import qa

    monkeypatch.setattr(qa, "plan_question", lambda *_a, **_k: pytest.fail("no local language parser"))
    question = "陈的基建技能有什么作用？我问的是基建，不是战斗技能或干员介绍。"
    wrong = {"module": "operator", "intent": "operator_overview", "entity_names": ["陈"]}
    corrected = {"module": "operator", "intent": "unsupported", "entity_names": ["陈"]}
    answer, wire, _ = invoke(tool_context, tmp_path, [query(wrong),
        lambda w: review(w, "revise", reason="用户问基建技能，概要不能替答；能力未核验应明确未知。"),
        query(corrected, "unsupported-query"), review], question=question)
    assert answer["status"] == "unsupported"
    assert answer["claims"] == answer["citations"] == []
    assert answer["agent_runtime"]["server_committed"]
    assert [item["decision"] for item in answer["agent_runtime"]["semantic_reviews"]] == ["revise", "accept"]
    assert review_data(wire[1])["typed_query"]["intent"] == "operator_overview"
    assert review_data(wire[3])["typed_query"]["intent"] == "unsupported"


def test_staged_hash_is_rechecked_before_model_accept_can_commit(tool_context, tmp_path, monkeypatch):
    original = runner._semantic_review_messages

    def mutate_after_message(*args):
        messages = original(*args)
        args[-1]["answer"]["answer"] = "unexpected local mutation"
        return messages

    monkeypatch.setattr(runner, "_semantic_review_messages", mutate_after_message)
    answer, _, _ = invoke(tool_context, tmp_path, [query(), review], agent_max_model_calls=2)
    assert answer["status"] == "refused"
    assert not answer["agent_runtime"]["server_committed"]
    assert answer["agent_runtime"]["steps"][-1]["status"] == "invalid_semantic_review_reference"
    assert "unexpected local mutation" not in answer["answer"]


def test_repeating_same_proposal_after_revision_cannot_loop_or_commit(tool_context, tmp_path):
    answer, wire, _ = invoke(tool_context, tmp_path, [query(),
        lambda w: review(w, "revise", reason="请求尚未回答，请纠正查询。"),
        query(ident="repeated"), query(ident="repeated-again")])
    assert answer["agent_runtime"]["reason"] == "tool_cycle"
    assert not answer["agent_runtime"]["server_committed"]
    assert answer["claims"] == answer["citations"] == []
    assert len(wire) == 4
    rejected = [step for step in answer["agent_runtime"]["steps"]
                if step["status"] == "unchanged_review_rejected_proposal"]
    assert len(rejected) == 1 and rejected[0]["executed"] is False
    assert len(answer["agent_runtime"]["staged_source_answers"]) == 1


@pytest.mark.parametrize("change", [
    {"answer_ref": "fake-answer"}, {"answer_sha256": "0" * 64},
    {"facts": "reviewer invented fact 123"}, {"decision": []},
    {"decision": "accept", "reason": ""}, {"reason": "x" * 1201},
])
def test_fake_or_rewriting_reviews_do_not_commit_and_can_be_corrected(tool_context, tmp_path, change):
    saved = {}

    def invalid(wire):
        saved.update(review_data(wire))
        return review(wire, **change)

    def valid(_wire):
        return response([call("review_source_answer", {
            **{key: saved[key] for key in ("answer_ref", "answer_sha256")},
            "decision": "accept", "reason": "只提交最新完整原件。"}, "corrected-review")])

    answer, wire, _ = invoke(tool_context, tmp_path, [query(), invalid, valid])
    assert answer["agent_runtime"]["server_committed"]
    assert answer["agent_runtime"]["model_calls"] == 3
    assert len(answer["agent_runtime"]["semantic_reviews"]) == 1
    assert "invalid_semantic_review_reference" in wire[2]["messages"][-1]["content"]
    assert "reviewer invented fact" not in answer["answer"]


def test_reviewer_cannot_submit_source_tool_or_legacy_reference(tool_context, tmp_path):
    saved = {}

    def wrong_phase(wire):
        saved.update(review_data(wire))
        return response([call("submit_answer", {"answer_ref": "staged_answer_1"})])

    def corrected(_wire):
        return response([call("review_source_answer", {"answer_ref": saved["answer_ref"],
            "answer_sha256": saved["answer_sha256"], "decision": "accept", "reason": "按原问交付原件。"})])

    answer, wire, _ = invoke(tool_context, tmp_path, [query(), wrong_phase, corrected])
    assert answer["agent_runtime"]["server_committed"]
    assert answer["agent_runtime"]["model_calls"] == 3
    assert "tool_not_available_in_phase" in wire[2]["messages"][-1]["content"]


@pytest.mark.parametrize("final", ["no_review_budget", "prose", "timeout", "missing_usage"])
def test_no_review_error_or_unknown_never_falls_back_or_commits(tool_context, tmp_path, monkeypatch, final):
    monkeypatch.setattr(agent, "dispatch_question", lambda *_a, **_k: pytest.fail("no local fallback"))
    changes = {"agent_max_model_calls": 1} if final == "no_review_budget" else {}
    replies = [query()]
    if final == "prose":
        replies.append(response(content="acceptable, but not an authenticated review tool call"))
    elif final == "timeout":
        replies.append(httpx.ReadTimeout("synthetic review timeout"))
    elif final == "missing_usage":
        raw = response([call("review_source_answer", {})])
        raw.pop("usage")
        replies.append(raw)
    answer, wire, _ = invoke(tool_context, tmp_path, replies, **changes)
    trace = answer["agent_runtime"]
    assert answer["status"] == "refused" and answer["claims"] == answer["citations"] == []
    assert not trace["server_committed"] and "committed_answer_hash" not in trace
    assert trace["model_calls"] == len(wire) == (0 if final == "no_review_budget" else 2)
    if final in {"timeout", "missing_usage"}:
        assert trace["budget"]["frozen"] and trace["budget"]["unknown_count"] == 1
        assert trace["budget"]["settled_count"] == 1


def test_review_accept_from_old_stage_is_rejected_after_new_query(tool_context, tmp_path):
    saved = {}

    def reject(wire):
        saved.update(review_data(wire))
        return review(wire, "revise", reason="不要附加原始数组。")

    def stale(wire):
        saved["new"] = review_data(wire)
        return response([call("review_source_answer", {"answer_ref": saved["answer_ref"],
            "answer_sha256": saved["answer_sha256"], "decision": "accept", "reason": "过时引用。"}, "stale")])

    def accept(_wire):
        current = saved["new"]
        return response([call("review_source_answer", {"answer_ref": current["answer_ref"],
            "answer_sha256": current["answer_sha256"], "decision": "accept", "reason": "当前原件正确。"}, "latest")])

    answer, wire, _ = invoke(tool_context, tmp_path, [query({**QUERY, "include_blackboard": True}),
        reject, query({**QUERY, "include_blackboard": False}, "new-query"), stale, accept])
    assert answer["agent_runtime"]["model_calls"] == 5
    assert "invalid_semantic_review_reference" in wire[4]["messages"][-1]["content"]
    assert answer["agent_runtime"]["committed_answer_hash"] == saved["new"]["answer_sha256"]


def test_every_review_and_revision_preserves_authenticated_full_parent_prefix(tool_context, tmp_path):
    first, _, _ = invoke(tool_context, tmp_path / "first", [query(), review])
    saved = agent.record_run(tool_context.session, tool_context.context.release, QUESTION, first)
    tool_context.session.commit()
    followup = "刚才等级说错了，改成7级，还是看同一个技能。"
    changed = {**QUERY, "rank": 7, "include_blackboard": True}
    answer, wire, _ = invoke(tool_context, tmp_path / "second", [query(changed),
        lambda w: review(w, "revise", reason="只要技力信息，不需数组。"),
        query({**changed, "include_blackboard": False}, "correction"), review],
        previous_run_id=saved["run_id"], question=followup)
    assert answer["agent_runtime"]["server_committed"]
    public = [json.loads(w["messages"][1]["content"]) for w in wire]
    assert all(item == public[0] for item in public)
    assert json.loads(public[0]["question"]) == {
        "current_question": followup, "previous_questions": [QUESTION]}
    assert all(saved["run_id"] in w["messages"][0]["content"] for w in (wire[1], wire[3]))
    assert answer["dialogue_state"]["questions"] == [QUESTION, followup]


def test_extended_execution_only_opens_with_explicit_review_setting():
    assert Settings(_env_file=None).agent_max_model_calls == 3
    with pytest.raises(ValidationError):
        Settings(_env_file=None, agent_max_model_calls=4)
    assert Settings(_env_file=None, agent_semantic_review=True, agent_max_model_calls=6).agent_max_model_calls == 6
    with pytest.raises(ValidationError):
        Settings(_env_file=None, agent_semantic_review=True, agent_max_model_calls=7)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, agent_semantic_review=True, agent_roguelike_delivery="legacy_submit")


@pytest.mark.parametrize("enabled", [False, True])
def test_server_load_injects_review_only_when_explicit_without_accounting_changes(monkeypatch, enabled):
    from contextlib import contextmanager
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from arknights_agent.runtime import stage40_budget

    @contextmanager
    def readonly():
        yield SimpleNamespace(execute=lambda _query: [(json.dumps({
            "profile_id": stage40_budget.STAGE16_PROFILE_ID,
            "continuation_id": "synthetic-retained-continuation",
        }),)])

    captured = {}
    book = SimpleNamespace(_readonly=readonly)

    def create_book(*_args, **kwargs):
        captured.update(kwargs)
        return book

    monkeypatch.setattr(stage40_budget, "Stage40BudgetLedger", create_book)
    monkeypatch.setattr(stage40_budget, "price_receipt", lambda _p: {"checked_at": datetime.now(UTC).isoformat()})
    monkeypatch.setattr(stage40_budget, "CurrentContextToolProfile", lambda **kw: kw)
    settings = Settings(_env_file=None, deepseek_model="deepseek-flash", deepseek_api_key="synthetic-only")
    actual_book, profile, configured = stage40_budget.load_runtime(settings,
        pricing_sources="synthetic-price", current_model_sources="synthetic-model",
        legacy_source_root="synthetic-old", semantic_review=enabled, unlimited=True)
    assert actual_book is book and profile["receipt"]["continuation_id"] == "synthetic-retained-continuation"
    assert configured.agent_semantic_review is enabled
    assert configured.agent_max_model_calls == (8 if enabled else 3)
    status = runner.runtime_status(configured)
    assert status["limits"]["max_model_calls"] == (8 if enabled else 3)
    assert status["limits"].get("semantic_review_enabled", False) is enabled
    assert status["model"].get("semantic_review_enabled", False) is enabled
    assert captured["unlimited"] is True


def test_cli_review_flag_and_app_injection_do_not_start_a_provider(monkeypatch, tmp_path):
    import importlib.util
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location("stage52_review_server", Path(__file__).resolve().parents[1] / "scripts/serve_web.py")
    web = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(web)
    captured = {}
    settings = Settings(_env_file=None)
    app = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(web, "Settings", lambda: settings)

    def load(configured, **kwargs):
        captured.update(kwargs)
        return "unstarted-ledger", "unstarted-profile", configured

    monkeypatch.setattr(web, "load_runtime", load)
    monkeypatch.setattr(web, "create_app", lambda _settings: app)
    monkeypatch.setattr(web, "recording_provider", lambda *_args: "unstarted-factory")
    monkeypatch.setattr(web.uvicorn.Server, "run", lambda server: captured.update(app=server.config.app))
    monkeypatch.setattr(sys, "argv", ["serve_web", "--live", "--unlimited", "--semantic-review",
        "--pricing-sources", str(tmp_path / "price"), "--current-model-sources", str(tmp_path / "model"),
        "--journal", str(tmp_path / "journal")])
    web.main()
    assert captured["semantic_review"] is True and captured["unlimited"] is True
    assert captured["app"] is app
    assert app.state.runtime_provider_factory == "unstarted-factory"
    assert not (tmp_path / "journal").exists()
