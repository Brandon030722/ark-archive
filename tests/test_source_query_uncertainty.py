"""Synthetic model repairs on an isolated PostgreSQL release, never a paid retest.

The hand-authored second proposals intentionally demonstrate the tool contract;
passing them does not prove that a live model will choose those corrections.
"""

import copy
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from test_recruitment import synthetic_sources as synthetic_sources
from test_runtime_runner import call, context, response
from test_stage2_api import _batch
from test_versions import pg_context as pg_context

from arknights_agent import agent, qa, services
from arknights_agent.config import Settings
from arknights_agent.runtime.runner import run_chat
from arknights_agent.runtime.tools import SourceQueryArguments, ToolContext, execute_tool

pytestmark = pytest.mark.postgres


@pytest.fixture
def uncertainty_context(pg_context, synthetic_sources, tmp_path, monkeypatch):
    """Use the existing source-query source batch in a new migrated PG schema."""
    def forbidden_network(*_args, **_kwargs):
        pytest.fail("external HTTP transport must not run in a local regression")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden_network)
    datasets = services.ingest_batch(
        pg_context.session, _batch("a" * 40, 20, synthetic_sources), pg_context.store,
        parser_version="synthetic-source-uncertainty",
    )
    release = services.publish_release(
        pg_context.session, {module: dataset.id for module, dataset in datasets.items()},
        label="synthetic uncertainty regression", store=pg_context.store,
    )
    services.activate_release(pg_context.session, release.id)
    pg_context.session.commit()
    settings = Settings(
        _env_file=None,
        database_url=pg_context.engine.url.render_as_string(hide_password=False),
        snapshot_root=pg_context.store.root,
        model_budget_path=tmp_path / "unused-synthetic-ledger.sqlite3",
        model_live_enabled=False,
    )
    yield SimpleNamespace(
        context=ToolContext(pg_context.session, release, pg_context.store, settings,
                            "六星干员的组合词条有哪些"),
        session=pg_context.session, a=release, engine=pg_context.engine,
    )


@pytest.fixture
def forbid_local_semantics(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("local natural-language parsing or fallback was invoked")

    monkeypatch.setattr(agent, "dispatch_question", forbidden)
    monkeypatch.setattr(agent, "_recruitment_tags", forbidden)
    monkeypatch.setattr(agent, "_duration", forbidden)
    monkeypatch.setattr(qa, "plan_question", forbidden)


def repair_from_wire(captured):
    return json.loads(captured["messages"][-1]["content"])


def assert_interpretation_repair(result):
    assert result["ok"] is False
    assert result["error"]["code"] == "query_interpretation_rejected"
    repair = result["error"]["repair"]
    assert repair["reason"] == "unresolved_requires_explicit_clarification"
    assert repair["fields"] == ["intent", "unresolved"]
    assert "clarify" in repair["instruction"] and "unsupported" in repair["instruction"]
    assert "不能" in repair["instruction"] and "条件" in repair["instruction"]
    assert "data" not in result


def simulate(tool_context, tmp_path, question, proposals, **settings):
    cfg, provider, captured = context(
        tool_context, tmp_path,
        [response([call("source_query", proposal, f"proposal-{index}")])
         for index, proposal in enumerate(proposals)],
        agent_roguelike_delivery="verified_query", agent_max_prompt_bytes=16384, **settings,
    )
    assert provider.simulation and cfg.model_live_enabled is False
    assert cfg.model_budget_path.is_relative_to(tmp_path)
    answer = run_chat(
        tool_context.engine, tool_context.context.store, cfg, question,
        release_id=tool_context.a.id, module="recruitment", backend="deepseek", provider=provider,
    )
    assert answer["agent_runtime"]["simulation"] is True
    # Captured bodies are the actual MockTransport HTTP wire, including the
    # model/stream/max_tokens/tool_choice envelope, rather than messages alone.
    assert captured and all(
        len(json.dumps(body, ensure_ascii=False, allow_nan=False,
                       separators=(",", ":")).encode("utf-8")) <= 16384 for body in captured
    )
    return answer, captured, cfg


@pytest.mark.parametrize("question,quote", [
    ("六星干员的组合词条有哪些", "六星干员的组合词条"),
    ("公招里六星那一档能挂哪些词，给我列个目录", "六星那一档"),
    ("别算概率，想翻翻公招六星关联标签", "公招六星关联标签"),
])
def test_model_can_repair_request_text_without_local_word_rules(
    uncertainty_context, tmp_path, forbid_local_semantics, question, quote,
):
    first = {"module": "recruitment", "intent": "catalog", "rarity": 6,
             "unresolved": [quote]}
    corrected = {**first, "unresolved": []}  # Hand-authored simulated model correction.
    originals = copy.deepcopy((first, corrected))
    answer, captured, _ = simulate(uncertainty_context, tmp_path, question, [first, corrected])
    assert (first, corrected) == originals
    assert len(captured) == answer["agent_runtime"]["model_calls"] == 2
    assert_interpretation_repair(repair_from_wire(captured[1]))
    sent_first = captured[1]["messages"][-2]["tool_calls"][0]
    assert json.loads(sent_first["function"]["arguments"]) == first
    assert answer["status"] == "answered"
    assert answer["catalog"]["rarity_specific_tags"] == ["高级资深干员"]
    assert answer["catalog"]["members"][0]["name"] == "合成样本6"
    assert answer["claims"] and answer["citations"]
    assert answer["query_interpretation"]["proposal"]["unresolved"] == []
    assert answer["question"] == question
    assert answer["agent_runtime"]["server_committed"] is True
    assert answer["agent_runtime"]["committed_answer_hash"] == services.content_digest(
        {key: value for key, value in answer.items() if key != "agent_runtime"}
    )
    assert all(citation["content_hash"] for citation in answer["citations"])


def test_real_unknown_condition_is_preserved_until_explicit_model_clarification(
    uncertainty_context, tmp_path, forbid_local_semantics,
):
    quote = "只要处于我说的蓝色轮换档"
    question = f"看看公招六星词条，{quote}"
    first = {"module": "recruitment", "intent": "catalog", "rarity": 6,
             "unresolved": [quote]}
    clarification = {**first, "intent": "clarify"}
    original = copy.deepcopy(first)
    rejected = execute_tool("source_query", first, replace(
        uncertainty_context.context, original_question=question,
    ))
    assert_interpretation_repair(rejected)
    assert first == original  # The server neither drops the unknown nor rewrites it.
    answer, captured, _ = simulate(uncertainty_context, tmp_path, question, [first, clarification])
    assert len(captured) == 2
    assert_interpretation_repair(repair_from_wire(captured[1]))
    assert answer["status"] == "needs_clarification"
    assert f"「{quote}」" in answer["answer"]
    assert answer["query_interpretation"]["proposal"]["unresolved"] == [quote]
    assert answer["claims"] == answer["citations"] == []
    assert "catalog" not in answer


EXECUTABLE = [
    ("recruitment", "catalog"), ("recruitment", "recruitment_calculation"),
    ("operator", "operator_overview"), ("operator", "operator_skill"),
    ("operator", "operator_talent"), ("operator", "operator_module"),
    ("operator", "operator_attributes"), ("garrison", "garrison_metadata"),
    ("garrison", "garrison_field"), ("garrison", "garrison_document"),
]


@pytest.mark.parametrize("module,intent", EXECUTABLE)
def test_every_executable_intent_rejects_unresolved_without_source_handler(
    uncertainty_context, monkeypatch, module, intent,
):
    from arknights_agent.runtime import source_queries

    def source_must_wait(*_args, **_kwargs):
        pytest.fail("a source handler ran before interpretation repair")

    for handler in ("_operator", "_recruitment", "_garrison"):
        monkeypatch.setattr(source_queries, handler, source_must_wait)
    quote = "我说的未定义限制"
    query = {"module": module, "intent": intent, "unresolved": [quote]}
    original = copy.deepcopy(query)
    result = execute_tool("source_query", query, replace(
        uncertainty_context.context, original_question=f"照资料查，但要满足{quote}",
    ))
    assert_interpretation_repair(result)
    assert query == original


@pytest.mark.parametrize("intent", ["catalog", "clarify", "unsupported"])
def test_quote_provenance_is_checked_before_any_unknown_outcome(uncertainty_context, intent):
    result = execute_tool("source_query", {
        "module": "recruitment", "intent": intent, "unresolved": ["模型新增的虚假限制"],
    }, uncertainty_context.context)
    assert result["ok"] is False
    assert result["error"]["code"] == "query_interpretation_rejected"
    assert result["error"]["repair"]["reason"] == "quote_not_in_verified_user_questions"
    assert result["error"]["repair"]["fields"] == ["unresolved"]
    assert "data" not in result


@pytest.mark.parametrize("unresolved", [[], ["实际到账的概率"]])
def test_explicit_unsupported_remains_a_deliverable_unknown(uncertainty_context, unresolved):
    query = {"module": "recruitment", "intent": "unsupported", "unresolved": unresolved}
    answer = execute_tool("source_query", query, replace(
        uncertainty_context.context, original_question="想知道实际到账的概率",
    ))
    assert answer["ok"] is True
    assert answer["data"]["status"] == "unsupported"
    assert answer["data"]["claims"] == answer["data"]["citations"] == []
    assert answer["data"]["query_interpretation"]["proposal"]["unresolved"] == unresolved


def test_three_interpretation_errors_stop_at_existing_call_limit_with_full_wire_under_cap(
    uncertainty_context, tmp_path, forbid_local_semantics,
):
    question = "六星词条给我看看"
    base = SourceQueryArguments(module="recruitment", intent="catalog", rarity=6,
                                unresolved=["六星词条"]).model_dump()
    proposals = [{**base, "limit": limit} for limit in (1, 2, 3)]
    proposals.append({**base, "unresolved": []})  # Must never be requested after the cap.
    answer, captured, cfg = simulate(uncertainty_context, tmp_path, question, proposals)
    assert answer["agent_runtime"]["reason"] == "model_call_limit"
    assert len(captured) == answer["agent_runtime"]["model_calls"] == 3
    assert answer["agent_runtime"]["server_committed"] is False
    assert answer["claims"] == answer["citations"] == []
    for payload in captured[1:]:
        assert_interpretation_repair(repair_from_wire(payload))
        assert json.loads(payload["messages"][-2]["tool_calls"][0]["function"]["arguments"])["unresolved"] == ["六星词条"]
    with sqlite3.connect(cfg.model_budget_path) as ledger:
        assert ledger.execute("SELECT COUNT(*) FROM reservation").fetchone()[0] == 3
    assert answer["agent_runtime"]["budget"]["settled_count"] == 3


def test_interpretation_repair_does_not_weaken_snapshot_hash_verification(uncertainty_context):
    query = {"module": "recruitment", "intent": "catalog", "rarity": 6, "unresolved": []}
    baseline = execute_tool("source_query", query, uncertainty_context.context)
    assert baseline["ok"] and baseline["data"]["citations"]
    source_hash = baseline["data"]["citations"][0]["content_hash"]
    path = uncertainty_context.context.store.path_for(source_hash)
    assert path.is_relative_to(uncertainty_context.context.store.root)
    path.write_bytes(path.read_bytes() + b"\nsynthetic-tampering")
    result = execute_tool("source_query", query, uncertainty_context.context)
    assert result["ok"] is False and result["error"]["code"] == "integrity_conflict"
    assert "data" not in result
