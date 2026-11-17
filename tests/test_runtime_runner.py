"""Synthetic tool cycles with real database reads and zero paid/model requests."""

import json
import time
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr
from test_recruitment import synthetic_sources as synthetic_sources
from test_runtime_tools import tool_context as tool_context

from arknights_agent import services
from arknights_agent.errors import InvalidData
from arknights_agent.runtime.providers import DeepSeekProvider
from arknights_agent.runtime.runner import run_chat, runtime_status
from arknights_agent.runtime.security import contains_secret, redact

QUESTION = "陈一技能专三的技力消耗和初始技力是多少？"
USAGE = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
         "prompt_cache_hit_tokens": 25, "prompt_cache_miss_tokens": 75}


def call(name, args, ident="call-1"):
    return {"id": ident, "type": "function", "function": {
        "name": name, "arguments": json.dumps(args, ensure_ascii=False),
    }}


def response(calls=None, *, content=None):
    message = {"role": "assistant", "content": content, "tool_calls": calls or []}
    return {"model": "deepseek-flash", "usage": dict(USAGE), "choices": [{
        "index": 0, "finish_reason": "tool_calls" if calls else "stop", "message": message,
    }]}


def context(tool_context, tmp_path, responses, **overrides):
    cfg = tool_context.context.settings.model_copy(update={
        "deepseek_api_key": SecretStr("synthetic-test-credential"),
        "deepseek_model": "deepseek-flash", "model_live_enabled": False,
        "deepseek_timeout_seconds": 1.0,
        "model_budget_path": tmp_path / "synthetic-budget.db",
        "model_input_cny_per_million": Decimal("2"),
        "model_output_cny_per_million": Decimal("8"),
        "model_price_verified_at": datetime.now(UTC),
        **overrides,
    })
    captured = []
    pending = iter(responses)

    def handle(request):
        captured.append(json.loads(request.content))
        item = next(pending)
        if isinstance(item, Exception):
            raise item
        return httpx.Response(200, json=item)

    provider = DeepSeekProvider(cfg, httpx.MockTransport(handle), simulation=True)
    return cfg, provider, captured


def run(tool_context, cfg, provider=None, **kwargs):
    return run_chat(
        tool_context.context.session.get_bind(), tool_context.context.store, cfg, QUESTION,
        release_id=tool_context.context.release.id, backend="deepseek", provider=provider, **kwargs,
    )


def test_local_and_live_block_never_construct_provider_or_read_secret(tool_context, monkeypatch, tmp_path):
    monkeypatch.setattr(SecretStr, "get_secret_value", lambda _: pytest.fail("secret dereferenced"))
    cfg = tool_context.context.settings.model_copy(update={
        "model_budget_path": tmp_path / "unused-budget.sqlite3",
    })
    answer = run(tool_context, cfg)
    assert answer["status"] == "refused"
    assert answer["claims"] == answer["citations"] == []
    assert answer["agent_runtime"]["reason"] == "live_disabled"
    assert answer["agent_runtime"]["model_calls"] == 0
    assert not cfg.model_budget_path.exists()


def test_typed_query_direct_delivery_preserves_full_authoritative_answer(tool_context, tmp_path):
    cfg, provider, captured = context(tool_context, tmp_path, [
        response([call("source_query", {"module": "operator", "intent": "operator_skill",
            "entity_names": ["陈"], "skill_index": 1, "rank": 10})]),
    ])
    answer = run(tool_context, cfg, provider)
    local = run_chat(tool_context.context.session.get_bind(), tool_context.context.store, cfg, QUESTION,
                     release_id=tool_context.context.release.id)
    assert answer["claims"] == local["claims"]
    assert answer["citations"] == local["citations"]
    trace = answer["agent_runtime"]
    assert trace["used"] == "deepseek_simulation" and trace["simulation"]
    assert trace["model_calls"] == trace["tool_calls"] == 1
    assert trace["budget"]["settled_count"] == 1
    assert trace["model_interpreted"] and trace["server_committed"]
    assert len(captured) == 1


def test_tool_error_recovery_is_bounded_and_does_not_rewrite_conditions(tool_context, tmp_path):
    cfg, provider, captured = context(tool_context, tmp_path, [
        response([call("query_records", {"module": "operator", "limit": 1000}, "bad")]),
        response([call("source_query", {"module": "operator", "intent": "operator_skill",
            "entity_names": ["陈"], "skill_index": 1, "rank": 10}, "good")]),
    ])
    answer = run(tool_context, cfg, provider)
    assert answer["agent_runtime"]["reason"] == "source_verified_query_delivered"
    assert "invalid_arguments" in captured[1]["messages"][-1]["content"]


def test_search_and_records_never_supply_final_facts(tool_context, tmp_path):
    cfg, provider, _ = context(tool_context, tmp_path, [
        response([call("web_search", {"query": "明日方舟 陈", "limit": 1})]),
        response([call("query_records", {"module": "operator", "name": "陈", "limit": 1})]),
        response(content="模型自编概率99.99%"),
    ])
    answer = run(tool_context, cfg, provider)
    assert answer["agent_runtime"]["reason"] == "uncertified_model_output"
    assert "99.99" not in json.dumps(answer, ensure_ascii=False)
    assert answer["claims"] == answer["citations"] == []


@pytest.mark.parametrize("args", [
    {"question": "塞雷娅一技能7级"}, {"module": "recruitment"},
    {"release_id": "other-release"}, {"rank": 7}, {"module": None},
    {"module": []}, {"module": {}},
])
def test_old_raw_question_tool_is_not_available_for_model_semantics(tool_context, tmp_path, args):
    cfg, provider, _ = context(tool_context, tmp_path, [response([call("knowledge_answer", args)])])
    cfg = cfg.model_copy(update={"agent_max_model_calls": 1})
    answer = run(tool_context, cfg, provider)
    assert answer["agent_runtime"]["steps"][0]["status"] == "tool_not_available_in_phase"
    assert answer["release_id"] == tool_context.context.release.id


@pytest.mark.parametrize("name,args,reason", [
    ("submit_answer", {"answer_ref": "invented", "answer": "fake"}, "tool_not_available_in_phase"),
    ("write_sql", {"query": "DELETE FROM release"}, "unknown_tool"),
])
def test_invalid_tools_cannot_produce_facts(tool_context, tmp_path, name, args, reason):
    cfg, provider, _ = context(tool_context, tmp_path, [response([call(name, args)])],
                                agent_max_model_calls=1)
    answer = run(tool_context, cfg, provider)
    assert answer["agent_runtime"]["steps"][0]["status"] == reason
    assert answer["status"] == "refused"
    assert answer["claims"] == answer["citations"] == []


def test_cycle_stops_before_repeated_tool_and_has_no_retry(tool_context, tmp_path):
    cfg, provider, captured = context(tool_context, tmp_path, [
        response([call("knowledge_answer", {})]), response([call("knowledge_answer", {}, "again")]),
    ])
    answer = run(tool_context, cfg, provider)
    assert answer["agent_runtime"]["reason"] == "tool_cycle"
    assert answer["agent_runtime"]["tool_calls"] == 1
    assert len(captured) == 2


def test_missing_usage_retains_reservation_and_freezes_restart(tool_context, tmp_path):
    bad = response(content="untrusted")
    del bad["usage"]
    cfg, provider, _ = context(tool_context, tmp_path, [bad])
    answer = run(tool_context, cfg, provider)
    assert answer["agent_runtime"]["budget"]["frozen"]
    assert Decimal(answer["agent_runtime"]["budget"]["reserved_cny"]) > 4
    assert answer["agent_runtime"]["budget"]["accounted_cny"] == "0.00000000"
    # A second runtime with the same durable file cannot reset or retry the fee.
    next_provider = DeepSeekProvider(cfg, httpx.MockTransport(lambda _: pytest.fail("retried")), True)
    again = run(tool_context, cfg, next_provider)
    assert again["agent_runtime"]["reason"] == "campaign_frozen"
    assert again["agent_runtime"]["model_calls"] == 0


def test_timeout_stays_unknown_and_does_not_log_body(tool_context, tmp_path):
    cfg, provider, _ = context(tool_context, tmp_path, [httpx.ReadTimeout("synthetic-private-body")])
    answer = run(tool_context, cfg, provider)
    assert answer["agent_runtime"]["reason"] == "timeout"
    assert "synthetic-private-body" not in json.dumps(answer)
    assert answer["agent_runtime"]["budget"]["unknown_count"] == 1


@pytest.mark.parametrize("limits,reason", [
    ({"agent_max_prompt_bytes": 10}, "prompt_limit"),
    ({"agent_max_total_seconds": 0.01}, "time_limit"),
    ({"agent_max_total_tokens": 1}, "token_limit"),
    ({"agent_max_tool_calls": 1}, "tool_limit"),
])
def test_limits_fall_back_without_uncertified_output(tool_context, tmp_path, limits, reason):
    cfg, provider, _ = context(tool_context, tmp_path, [
        response([call("knowledge_answer", {}), call("submit_answer", {"answer_ref": "answer_1"}, "2")]),
    ], **limits)
    assert run(tool_context, cfg, provider)["agent_runtime"]["reason"] == reason


@pytest.mark.parametrize("secret", [
    "sk-" + "synthetic1234567890", "Bearer syntheticlongcredential",
    "password=syntheticvalue", "postgresql+psycopg://synthetic:syntheticpass@localhost/db",
    '{"api_key":"synthetic"}', '{"access_token":"synthetic"}',
])
def test_sensitive_questions_rejected_before_db_or_model(tool_context, secret):
    with pytest.raises(InvalidData, match="sensitive"):
        run_chat(None, None, tool_context.context.settings, f"陈技能 {secret}")
    assert contains_secret(secret)
    assert secret not in redact(secret)


def test_status_cannot_read_or_expose_key(tool_context, monkeypatch):
    monkeypatch.setattr(SecretStr, "get_secret_value", lambda _: pytest.fail("secret read"))
    public = runtime_status(tool_context.context.settings)
    assert not public["model"]["ready"]
    assert not public["model"]["legacy_openai_enabled"]
    assert "synthetic-key-not-used" not in json.dumps(public)
    assert "deepseek_model" in public["model"]["missing_fields"]


def test_active_release_resolved_only_once_even_if_pointer_changes(tool_context, tmp_path, monkeypatch):
    resolve = services.resolve_release
    calls = []

    def pinned(session, release_id=None):
        calls.append(release_id)
        if len(calls) > 1:
            pytest.fail("active pointer resolved twice")
        return resolve(session, tool_context.context.release.id)

    monkeypatch.setattr(services, "resolve_release", pinned)
    cfg, provider, _ = context(tool_context, tmp_path, [
        response([call("knowledge_answer", {})]),
        response([call("submit_answer", {"answer_ref": "answer_1"})]),
    ])
    answer = run_chat(tool_context.context.session.get_bind(), tool_context.context.store, cfg, QUESTION,
                      backend="deepseek", provider=provider)
    assert answer["release_id"] == tool_context.context.release.id
    assert calls == [None]


def test_wall_deadline_includes_slow_local_fallback(tool_context, monkeypatch):
    from arknights_agent import agent

    original = agent.dispatch_question

    def slow(*args, **kwargs):
        time.sleep(0.25)
        return original(*args, **kwargs)

    monkeypatch.setattr(agent, "dispatch_question", slow)
    cfg = tool_context.context.settings.model_copy(update={"agent_max_total_seconds": 0.12})
    started = time.monotonic()
    result = run_chat(tool_context.engine, tool_context.context.store, cfg, QUESTION,
                      release_id=tool_context.a.id)
    elapsed = time.monotonic() - started
    assert result["status"] == "timeout" and elapsed < 0.22
    assert result["claims"] == result["citations"] == []
    assert result["agent_runtime"]["elapsed_seconds"] >= 0.1
    assert not result["agent_runtime"]["version_verified"]
    # Let the bounded synthetic read worker close before fixture teardown.
    time.sleep(0.3)
