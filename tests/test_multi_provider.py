"""Real protocol envelopes with synthetic keys and HTTP transports only."""

import json
import sqlite3
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from responses_fixture import native_reply

from arknights_agent.api import create_app
from arknights_agent.config import Settings
from arknights_agent.runtime.budget import BudgetError, BudgetLedger
from arknights_agent.runtime.credentials import CredentialManager
from arknights_agent.runtime.multi_provider import MultiProvider, validate_base_url
from arknights_agent.runtime.provider_budget import ProviderLedger, ProviderProfile
from arknights_agent.runtime.provider_settings import Connection, ProviderSettings, check_connection
from arknights_agent.runtime.providers import ProviderError
from arknights_agent.runtime.wire_journal import recording_provider

KEY = "sk-synthetic-provider-test-not-real-11111111"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Read evidence",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }
]
MESSAGES = [
    {"role": "system", "content": "Use cited evidence."},
    {"role": "user", "content": "温蒂的公招标签？"},
]


def cfg(tmp_path, protocol="openai"):
    return Settings(
        _env_file=None,
        database_url="sqlite:///:memory:",
        api_protocol=protocol,
        api_base_url={
            "openai": "https://api.openai.com/v1",
            "anthropic": "https://api.anthropic.com/v1",
            "compatible": "https://example.invalid/v1",
        }[protocol],
        deepseek_model="synthetic-model",
        deepseek_api_key=KEY,
        agent_context_tokens=32768,
        agent_max_output_tokens=2048,
        agent_large_output_enabled=True,
        agent_model_first=True,
        agent_semantic_review=True,
        model_input_cny_per_million="3",
        model_output_cny_per_million="12",
        model_price_verified_at=datetime.now(UTC),
        model_price_source="owner_configured_conservative_cny",
        model_budget_path=tmp_path / "budget.db",
        credential_store_path=tmp_path / "vault/deepseek.enc",
        credential_master_key_path=tmp_path / "owner/key",
    )


def reply(protocol, **overrides):
    if protocol == "anthropic":
        body = {
            "id": "msg_synthetic",
            "model": "synthetic-model",
            "stop_reason": "tool_use",
            "content": [
                {"type": "tool_use", "id": "call1", "name": "lookup", "input": {"query": "温蒂"}}
            ],
            "usage": {
                "input_tokens": 80,
                "cache_creation_input_tokens": 10,
                "cache_read_input_tokens": 10,
                "output_tokens": 20,
            },
        }
    else:
        body = {
            "id": "chatcmpl_synthetic",
            "model": "synthetic-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": '{"query":"温蒂"}'},
                            }
                        ],
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "prompt_tokens_details": {"cached_tokens": 10},
            },
        }
    if protocol == "openai":
        body = native_reply(body)
    return {**body, **overrides}


@pytest.mark.parametrize("protocol", ["openai", "compatible", "anthropic"])
def test_native_request_tool_return_and_usage(tmp_path, protocol):
    settings = cfg(tmp_path, protocol)
    captured = []

    def handle(request):
        captured.append(request)
        return httpx.Response(200, json=reply(protocol))

    provider = MultiProvider(settings, httpx.MockTransport(handle), simulation=True)
    turn = provider.complete(MESSAGES, TOOLS, max_output_tokens=2048)
    assert turn.tool_calls[0].arguments == {"query": "温蒂"}
    assert turn.usage == {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "prompt_cache_hit_tokens": 10,
        "prompt_cache_miss_tokens": 90,
    }
    body = json.loads(captured[0].content)
    assert "thinking" not in body
    assert str(captured[0].url) == settings.api_base_url + (
        {"anthropic": "/messages", "openai": "/responses"}.get(protocol, "/chat/completions")
    )
    assert KEY not in captured[0].content.decode()
    messages = MESSAGES + [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"query":"温蒂"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call1", "content": "verified evidence"},
    ]
    wire = provider._request(messages, TOOLS, 2048)
    if protocol == "anthropic":
        assert (
            captured[0].headers["x-api-key"] == KEY and "authorization" not in captured[0].headers
        )
        assert wire["messages"][-1]["content"][0]["tool_use_id"] == "call1"
        assert wire["tools"][0]["input_schema"] == TOOLS[0]["function"]["parameters"]
        assert wire["tool_choice"] == {"type": "tool", "name": "lookup"}
    elif protocol == "openai":
        assert captured[0].headers["authorization"] == "Bearer " + KEY
        assert wire["input"][-1] == {"type": "function_call_output", "call_id": "call1", "output": "verified evidence"}
        assert wire["max_output_tokens"] == 2048 and wire["store"] is False
        assert wire["tool_choice"] == {"type": "function", "name": "lookup"}
        assert wire["tools"][0]["parameters"] == TOOLS[0]["function"]["parameters"]
        assert wire["tools"][0]["strict"] is False
    else:
        assert captured[0].headers["authorization"] == "Bearer " + KEY
        assert wire["messages"][-1]["role"] == "tool"
        assert "max_tokens" in wire


@pytest.mark.parametrize("protocol", ["openai", "compatible", "anthropic"])
def test_unknown_usage_and_wrong_model_are_never_success(tmp_path, protocol):
    for overrides in [{"usage": {}}, {"model": "unrelated-model"}]:
        p = MultiProvider(
            cfg(tmp_path, protocol),
            httpx.MockTransport(lambda _: httpx.Response(200, json=reply(protocol, **overrides))),
            simulation=True,
        )
        with pytest.raises(ProviderError):
            p.complete(MESSAGES, TOOLS, max_output_tokens=2048)


def connection(protocol="openai"):
    return Connection(
        protocol=protocol,
        base_url={
            "openai": "https://api.openai.com/v1",
            "anthropic": "https://api.anthropic.com/v1",
            "compatible": "https://example.invalid/v1",
        }[protocol],
        model="synthetic-model",
        context_tokens=32768,
        max_output_tokens=2048,
        input_cny_per_million="3",
        output_cny_per_million="12",
        token=KEY,
        updated_at=datetime.now(UTC).isoformat(),
    ).checked()


def test_encrypted_profiles_do_not_replace_legacy_key_or_leak(tmp_path):
    settings = cfg(tmp_path)
    legacy = CredentialManager(settings)
    legacy.replace(KEY + "legacy", checker=lambda _: {})
    old = settings.credential_store_path.read_bytes()
    manager = ProviderSettings(settings)
    manager.persist("openai", {"openai": connection(), "anthropic": connection("anthropic")})
    assert KEY.encode() not in manager.path.read_bytes()
    assert KEY not in json.dumps(manager.public(True))
    assert manager.path.stat().st_mode & 0o777 == 0o600
    restored = ProviderSettings(settings)
    assert restored.apply(settings).deepseek_api_key.get_secret_value() == KEY
    restored.persist("deepseek", restored.connections)
    assert settings.credential_store_path.read_bytes() == old
    assert CredentialManager(settings).key.get_secret_value() == KEY + "legacy"


def test_provider_routes_origin_host_key_reuse_and_no_paid_request(tmp_path, monkeypatch):
    settings = cfg(tmp_path)
    app = create_app(settings)
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234))
    headers = {
        "Origin": "http://127.0.0.1",
        "X-Credential-Nonce": app.state.credential_manager.nonce,
    }
    body = {
        "action": "save",
        "protocol": "compatible",
        "connection": connection("compatible").model_dump(mode="json"),
        "acknowledged": True,
    }
    assert client.post("/v1/account/providers", json=body).status_code == 403
    response = client.post("/v1/account/providers", headers=headers, json=body)
    assert response.status_code == 200 and response.json()["active"] == "compatible"
    assert KEY not in response.text
    body["connection"]["base_url"] = "https://other.invalid/v1"
    body["connection"]["token"] = ""
    assert (
        client.post("/v1/account/providers", headers=headers, json=body).json()["detail"]
        == "provider_new_host_requires_key"
    )
    body["connection"]["base_url"] = "https://example.invalid/v1"
    assert client.post("/v1/account/providers", headers=headers, json=body).status_code == 200
    monkeypatch.setattr(
        "arknights_agent.runtime.account_overview.read_balance",
        lambda *_: pytest.fail("must not send this key to DeepSeek"),
    )
    assert client.get("/v1/account/overview").json()["account"]["state"] == "unsupported"
    assert not client.get("/v1/account/credential").json()["setup_required"]
    remote = TestClient(app, base_url="http://192.168.1.2", client=("192.168.1.9", 1234))
    assert not remote.get("/v1/account/providers").json()["can_manage"]
    assert remote.post("/v1/account/providers", headers=headers, json=body).status_code == 403
    assert not settings.model_budget_path.exists()


@pytest.mark.parametrize("protocol", ["openai", "anthropic", "compatible"])
def test_check_connection_only_reads_models(tmp_path, protocol):
    def handle(request):
        assert request.method == "GET" and request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "synthetic-model"}]})

    result = check_connection(connection(protocol), transport=httpx.MockTransport(handle))
    assert result == {"ok": True, "model_listed": True, "paid_generation_tested": False}


@pytest.mark.parametrize(
    "url",
    [
        "http://unsafe.invalid/v1",
        "https://key@host.invalid/v1",
        "https://host.invalid/v1?key=secret",
        "https://host.invalid:abc/v1",
        "https://host.invalid/v1/messages",
    ],
)
def test_bad_endpoint_rejected(url):
    with pytest.raises(ValueError):
        validate_base_url("compatible", url)


def test_new_provider_accounting_retains_old_rows_and_global_freeze(tmp_path):
    settings = cfg(tmp_path).model_copy(update={"web_task_failure_isolation": False})
    BudgetLedger(settings.model_budget_path, price_verified_at=datetime.now(UTC))
    book = ProviderLedger(settings, "task1")
    provider = MultiProvider(
        settings,
        httpx.MockTransport(lambda _: httpx.Response(200, json=reply("openai"))),
        simulation=True,
    )
    profile = ProviderProfile(settings)

    def reserve(ledger):
        b = profile.bind_request(provider, MESSAGES, TOOLS, max_output_tokens=2048)
        return ledger.reserve(
            b.input_tokens,
            b.output_tokens,
            3,
            12,
            bounded_request=b,
            continuation_id=profile.continuation_id,
        )

    first = reserve(book)
    turn = provider.complete(MESSAGES, TOOLS, max_output_tokens=2048)
    settled = book.settle(first, turn.usage)
    assert settled["stage40_requests"] == 1 and settled["reservation_state"] == "settled"
    with book._readonly() as db:
        old = tuple(
            db.execute("SELECT * FROM reservation WHERE reservation_id=?", (first,)).fetchone()
        )
    second = reserve(book.for_task("task2"))
    book.for_task("task2").fail_unknown(second, "timeout")
    assert not book.available()["ready"] and book.status()["unknown_count"] == 1
    with pytest.raises(BudgetError):
        reserve(book.for_task("task3"))
    with book._readonly() as db:
        assert (
            tuple(
                db.execute("SELECT * FROM reservation WHERE reservation_id=?", (first,)).fetchone()
            )
            == old
        )
        assert db.execute("SELECT count(*) FROM provider_attempt").fetchone()[0] == 2


def test_journal_factories_share_append_counter(tmp_path):
    settings = cfg(tmp_path)
    BudgetLedger(settings.model_budget_path, price_verified_at=datetime.now(UTC))
    book = ProviderLedger(settings, "task1")
    profile = ProviderProfile(settings)
    classes = [recording_provider(MultiProvider, book, tmp_path / "journal") for _ in range(2)]
    for n in range(3):
        p = classes[n % 2](
            settings,
            httpx.MockTransport(lambda _: httpx.Response(200, json=reply("openai"))),
            simulation=True,
        )
        b = profile.bind_request(p, MESSAGES, TOOLS, max_output_tokens=2048)
        rid = book.reserve(
            b.input_tokens,
            b.output_tokens,
            3,
            12,
            bounded_request=b,
            continuation_id=profile.continuation_id,
        )
        book.settle(rid, p.complete(MESSAGES, TOOLS, max_output_tokens=2048).usage)
    rows = [
        json.loads(line)
        for line in (tmp_path / "journal/model-turns.jsonl").read_text().splitlines()
    ]
    assert [r["sequence"] for r in rows if r["phase"] == "started"] == [1, 2, 3]
    assert KEY not in json.dumps(rows)


def test_native_malformed_arguments_keep_known_usage(tmp_path):
    body = reply("openai")
    body["output"][1]["arguments"] = "not-json"
    p = MultiProvider(
        cfg(tmp_path),
        httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
        simulation=True,
    )
    with pytest.raises(ProviderError) as caught:
        p.complete(MESSAGES, TOOLS, max_output_tokens=2048)
    assert p.failure_receipt(caught.value)["usage"]["total_tokens"] == 120


def test_recorded_context_keeps_original_provider_limit(tmp_path):
    from arknights_agent.runtime.context_usage import context_usage

    settings = cfg(tmp_path)
    BudgetLedger(settings.model_budget_path, price_verified_at=datetime.now(UTC))
    p = MultiProvider(
        settings,
        httpx.MockTransport(lambda _: httpx.Response(200, json=reply("openai"))),
        simulation=True,
    )
    book = ProviderLedger(settings, "task1")
    profile = ProviderProfile(settings)
    b = profile.bind_request(p, MESSAGES, TOOLS, max_output_tokens=2048)
    rid = book.reserve(
        b.input_tokens,
        b.output_tokens,
        3,
        12,
        bounded_request=b,
        continuation_id=profile.continuation_id,
    )
    book.settle(rid, p.complete(MESSAGES, TOOLS, max_output_tokens=2048).usage)
    tasks = tmp_path / "tasks.db"
    with sqlite3.connect(tasks) as db:
        db.execute("CREATE TABLE http_chat_task(task_id TEXT,conversation_id TEXT)")
        db.execute("INSERT INTO http_chat_task VALUES ('task1','conv1')")
    data = context_usage(
        settings.model_budget_path,
        tasks,
        limit_tokens=524288,
        task_id="task1",
        conversation_id="conv1",
    )
    assert (
        data["limit_tokens"] == 32768
        and data["provider"] == "openai"
        and data["used_tokens"] == 120
    )


def test_native_web_ready_without_deepseek_binding_is_readonly(tmp_path, monkeypatch):
    from arknights_agent.runtime import providers, runner

    settings = cfg(tmp_path).model_copy(update={"model_live_enabled": True})
    BudgetLedger(settings.model_budget_path, price_verified_at=datetime.now(UTC))
    app = create_app(settings)
    app.state.provider_settings.persist("anthropic", {"anthropic": connection("anthropic")})
    monkeypatch.setattr(providers, "LIVE_CALLS_AUTHORIZED", True)
    monkeypatch.setattr(runner, "LIVE_BUDGET_PATH", settings.model_budget_path)
    before = settings.model_budget_path.read_bytes()
    status = TestClient(app).get("/v1/agent/status").json()
    assert status["web_model"]["ready"] and status["model"]["provider"] == "anthropic"
    assert (
        status["limits"]["execution_engine"] == "langgraph"
        and status["limits"]["max_model_calls"] >= 12
    )
    assert status["limits"]["context_tokens"] == 32768
    assert settings.model_budget_path.read_bytes() == before


def test_anthropic_malformed_body_preserves_confirmed_usage(tmp_path):
    p = MultiProvider(
        cfg(tmp_path, "anthropic"),
        httpx.MockTransport(lambda _: httpx.Response(200, json=reply("anthropic", content=None))),
        simulation=True,
    )
    with pytest.raises(ProviderError) as caught:
        p.complete(MESSAGES, TOOLS, max_output_tokens=2048)
    assert p.failure_receipt(caught.value)["usage"]["total_tokens"] == 120
