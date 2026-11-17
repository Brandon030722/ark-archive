"""Personal installs keep counted attempts and unknown holds across restarts."""
import hashlib

import httpx
import pytest
from test_multi_provider import MESSAGES, TOOLS, cfg, reply

from arknights_agent.runtime.budget import BudgetError
from arknights_agent.runtime.multi_provider import MultiProvider
from arknights_agent.runtime.provider_budget import ProviderLedger, ProviderProfile
from scripts.first_run import ensure_book


def test_unknown_then_new_turn_survives_reinstallation(tmp_path):
    settings = cfg(tmp_path).model_copy(update={"web_task_failure_isolation": True})
    path = settings.model_budget_path
    ensure_book(path)
    assert path.stat().st_mode & 0o777 == 0o600
    provider = MultiProvider(settings, httpx.MockTransport(
        lambda _: httpx.Response(200, json=reply("openai"))), simulation=True)
    profile = ProviderProfile(settings)
    bound = profile.bind_request(provider, MESSAGES, TOOLS, max_output_tokens=2048)

    def reserve(book):
        return book.reserve(bound.input_tokens, bound.output_tokens,
                            settings.model_input_cny_per_million, settings.model_output_cny_per_million,
                            bounded_request=bound, continuation_id=profile.continuation_id)

    failed = ProviderLedger(settings, "first-question")
    assert failed.status()["reservation_count"] == 0
    failed.fail_unknown(reserve(failed), "timeout")
    held = failed.status()["reserved_cny"]
    digest = hashlib.sha256(path.read_bytes()).digest()
    ensure_book(path)
    assert hashlib.sha256(path.read_bytes()).digest() == digest
    with pytest.raises(BudgetError, match="task_already_failed"):
        reserve(ProviderLedger(settings, "first-question"))
    following = ProviderLedger(settings, "follow-up")
    following.settle(reserve(following), provider.complete(MESSAGES, TOOLS, max_output_tokens=2048).usage)
    state = following.status()
    assert state["unknown_count"] == state["settled_count"] == 1
    assert state["reserved_cny"] == held
    assert state["reservation_count"] == state["stage40_requests"] == 2


def test_first_connection_enables_personal_workbench_without_legacy_proofs(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from scripts.serve_web import build_app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ARK_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("ARK_CREDENTIAL_MASTER_KEY_PATH", str(tmp_path / "owner.key"))
    # Mirror the fixed ledger path of a separately extracted installation.
    budget = tmp_path / ".local/model-budget.sqlite3"
    monkeypatch.setattr("arknights_agent.runtime.runner.LIVE_BUDGET_PATH", budget)
    monkeypatch.setenv("ARK_MODEL_BUDGET_PATH", str(budget))
    ensure_book(budget)
    args = SimpleNamespace(live=True, personal=True)
    origin = "http://127.0.0.1"

    def client():
        return TestClient(build_app(args), base_url=origin, client=("127.0.0.1", 1234))

    with client() as first:
        assert first.get("/v1/agent/status").json()["web_model"]["reason"] == "configuration_missing"
        credentials = first.get("/v1/account/credential").json()
        saved = first.post("/v1/account/providers", headers={
            "Origin": origin, "X-Credential-Nonce": credentials["nonce"],
        }, json={"action": "save", "acknowledged": True, "protocol": "compatible",
                 "connection": {"protocol": "compatible", "base_url": "https://example.invalid/v1",
                                "token": "synthetic-onboarding-local-profile", "model": "demo",
                                "context_tokens": 32768, "max_output_tokens": 2048,
                                "input_cny_per_million": "3", "output_cny_per_million": "12"}})
        assert saved.status_code == 200, saved.text
        assert first.get("/v1/agent/status").json()["web_model"]["ready"]
    with client() as restarted:
        assert restarted.get("/v1/agent/status").json()["web_model"]["ready"]
    assert b"synthetic-onboarding-local-profile" not in (tmp_path / ".local/credentials/providers.enc").read_bytes()
