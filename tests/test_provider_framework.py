"""Unified connections: public discovery, encrypted commit and shared accounting."""

import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from arknights_agent.api import create_app
from arknights_agent.config import Settings
from arknights_agent.runtime.credentials import effective_settings
from arknights_agent.runtime.provider_registry import (
    PROVIDERS,
    extract_catalog,
    model_defaults,
    read_catalog,
)
from arknights_agent.runtime.provider_settings import ProviderSettings, discover_models

KEY = "sk-synthetic-framework-not-real-1234567890"


@pytest.fixture
def owner(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, database_url="sqlite:///:memory:",
        deepseek_api_key=None, credential_store_path=tmp_path / "vault/deepseek.enc",
        credential_master_key_path=tmp_path / "owner/master.key", model_budget_path=tmp_path / "budget.db")
    app = create_app(settings)
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234))
    client.headers.update({"Origin": "http://127.0.0.1", "X-Credential-Nonce": app.state.credential_manager.nonce})
    monkeypatch.setattr("arknights_agent.runtime.account_overview.read_balance", lambda *_: pytest.fail("no balance request in connection management"))
    return settings, app, client


def post(client, action, protocol, connection=None):
    return client.post("/v1/account/providers", json={"action": action, "protocol": protocol, "connection": connection, "acknowledged": True})


@pytest.mark.parametrize("protocol", list(PROVIDERS))
def test_key_discovery_save_switch_remove_without_paid_calls(owner, monkeypatch, protocol):
    settings, app, client = owner
    catalog = read_catalog()
    # Use catalog model IDs, not an assertion that any real account can use them.
    model = next((k for k in catalog["providers"].get(protocol, {}) if model_defaults(protocol, k, catalog)), "custom-fixture")
    observed = []
    def discovery(c):
        observed.append(c)
        return {"ok": True, "complete": True, "models": [{"id": model}], "paid_generation_tested": False}
    monkeypatch.setattr("arknights_agent.runtime.provider_settings.discover_models", discovery)
    base = PROVIDERS[protocol]["base_url"] or "https://example.invalid/v1"
    found = post(client, "discover", protocol, {"protocol": protocol, "base_url": base, "token": KEY})
    assert found.status_code == 200 and found.json()["ok"]
    assert not found.json()["paid_generation_tested"]
    assert KEY not in found.text and not app.state.provider_settings.path.exists()
    values = {"protocol": protocol, "base_url": base, "model": model, "token": "", "draft_id": found.json()["draft_id"]}
    if protocol == "compatible":
        assert post(client, "save", protocol, values).json()["detail"] == "provider_metadata_required"
        values.update(context_tokens=32768, max_output_tokens=4096, input_cny_per_million="2", output_cny_per_million="8")
    saved = post(client, "save", protocol, values)
    assert saved.status_code == 200, saved.text
    assert saved.json()["active"] == protocol and KEY not in saved.text
    assert len(observed) == 1
    applied = effective_settings(app, settings)
    assert applied.api_connection_id == "profile:" + protocol
    assert applied.api_protocol == protocol and applied.deepseek_api_key.get_secret_value() == KEY
    vault = app.state.provider_settings.path
    assert KEY.encode() not in vault.read_bytes() and vault.stat().st_mode & 0o777 == 0o600
    assert ProviderSettings(settings).connections[protocol].token == KEY
    assert not client.get("/v1/account/credential").json()["setup_required"]
    assert post(client, "remove", protocol).json()["active"] is None
    assert effective_settings(app, settings).deepseek_api_key is None
    assert ProviderSettings(settings).connections == {}
    assert not settings.model_budget_path.exists()


def test_ticket_expiry_host_binding_and_error_sanitizing(owner, monkeypatch):
    _, app, client = owner
    monkeypatch.setattr("arknights_agent.runtime.provider_settings.discover_models", lambda c: {"ok":True,"models":[],"complete":True,"paid_generation_tested":False})
    base = {"protocol":"compatible", "base_url":"https://example.invalid/v1", "token":KEY}
    ticket = post(client, "discover", "compatible", base).json()["draft_id"]
    altered = {**base, "base_url":"https://other.invalid/v1", "token":"", "draft_id":ticket, "model":"custom"}
    assert post(client,"save","compatible",altered).json()["detail"] == "provider_draft_expired"
    app.state.provider_settings.drafts[ticket]["expires"] = time.monotonic() - 1
    altered["base_url"] = base["base_url"]
    assert post(client,"save","compatible",altered).json()["detail"] == "provider_draft_expired"
    bad = post(client,"save","openai",{"protocol":"openai","token":KEY,"model":"bad"})
    assert bad.status_code == 422 and KEY not in bad.text


@pytest.mark.parametrize("protocol", list(PROVIDERS))
def test_discovery_is_get_only_and_does_not_forward_keys(protocol):
    calls=[]
    def handle(r):
        calls.append(r)
        assert r.method == "GET" and r.url.path.endswith("/models")
        assert r.url.host == ("api.anthropic.com" if protocol == "anthropic" else "example.invalid")
        if protocol == "anthropic":
            assert r.headers["x-api-key"] == KEY and "authorization" not in r.headers
        else:
            assert r.headers["authorization"] == "Bearer " + KEY and "x-api-key" not in r.headers
        return httpx.Response(200,json={"data":[{"id":"valid-model"},{"id":"<script>"},{"id":"valid-model"},{"id":KEY},{"id":"echo","display_name":KEY}]})
    c=SimpleNamespace(protocol=protocol,base_url="https://api.anthropic.com/v1" if protocol=="anthropic" else "https://example.invalid/v1",token=KEY)
    result=discover_models(c,transport=httpx.MockTransport(handle))
    assert len(calls)==1 and result["models"]==[{"id":"valid-model","name":"valid-model"}]


def test_anthropic_pagination_and_auth_failure():
    calls=[]
    def handle(r):
        calls.append(r)
        if len(calls)==1:
            return httpx.Response(200,json={"data":[{"id":"first"}],"has_more":True,"last_id":"first"})
        assert r.url.params["after_id"] == "first"
        return httpx.Response(200,json={"data":[{"id":"second"}],"has_more":False})
    c=SimpleNamespace(protocol="anthropic",base_url=PROVIDERS["anthropic"]["base_url"],token=KEY)
    assert len(discover_models(c,transport=httpx.MockTransport(handle))["models"])==2
    denied=discover_models(c,transport=httpx.MockTransport(lambda _:httpx.Response(401,text=KEY)))
    assert denied=={"ok":False,"reason":"authentication_failed"} and KEY not in json.dumps(denied)


def test_catalog_cost_tiers_and_unknown_are_explicit():
    raw={"openai":{"models":{"fixture":{"limit":{"context":1050000,"input":900000,"output":128000},"cost":{"input":2,"output":12,"cache_write":2.5,"tiers":[{"input":4,"output":18,"cache_write":5}]},"tool_call":True,"modalities":{"output":["text"]}}}}}
    catalog=extract_catalog(raw,datetime.now(UTC).isoformat())
    defaults=model_defaults("openai","fixture",catalog)
    assert defaults["context_tokens"]==524288 and defaults["max_output_tokens"]==8192
    assert defaults["input_cny_per_million"]=="40" and defaults["output_cny_per_million"]=="144"
    assert model_defaults("compatible","fixture",catalog) is None
    assert model_defaults("openai","fixture",{**catalog,"stale":True}) is None


def test_local_host_nonce_and_remote_permissions(owner):
    _, app, client=owner
    assert client.get("/v1/account/providers").json()["can_manage"]
    remote=TestClient(app,base_url="http://192.168.1.2",client=("127.0.0.1",1234))
    assert not remote.get("/v1/account/providers").json()["can_manage"]
    assert post(remote,"activate","openai").status_code==403
    client.headers["X-Credential-Nonce"]="wrong"
    assert post(client,"activate","openai").status_code==403


def test_legacy_key_is_adopted_and_cannot_resurrect_after_removal(tmp_path):
    settings=Settings(_env_file=None,database_url="sqlite:///:memory:",deepseek_api_key=KEY,
        deepseek_model="deepseek-flash",model_input_cny_per_million="2",model_output_cny_per_million="8",
        model_price_verified_at=datetime.now(UTC),credential_store_path=tmp_path/"vault/deepseek.enc",
        credential_master_key_path=tmp_path/"owner/key")
    app=create_app(settings)
    managed=app.state.provider_settings
    assert managed.connections["deepseek"].rate_source=="legacy_verified_price"
    assert effective_settings(app,settings).api_connection_id=="profile:deepseek"
    managed.persist(None,{})
    assert app.state.credential_manager.key.get_secret_value()==KEY
    assert effective_settings(app,settings).deepseek_api_key is None
    assert ProviderSettings(settings,app.state.credential_manager).apply(settings).deepseek_api_key is None


def test_catalog_renewal_keeps_key_context_and_previous_profile(owner, monkeypatch):
    settings,app,client=owner
    from arknights_agent.runtime.provider_settings import Connection
    original=Connection(protocol="openai",base_url=PROVIDERS["openai"]["base_url"],token=KEY,
        model="fixture",context_tokens=16384,max_output_tokens=2048,input_cny_per_million="2",
        output_cny_per_million="8",updated_at=datetime.now(UTC).isoformat(),rate_source="models_dev_estimate_cny8")
    app.state.provider_settings.persist("openai",{"openai":original})
    catalog=extract_catalog({"openai":{"models":{"fixture":{"limit":{"context":524288,"output":8192},"cost":{"input":3,"output":10},"tool_call":True,"modalities":{"output":["text"]}}}}},datetime.now(UTC).isoformat())
    monkeypatch.setattr("arknights_agent.runtime.provider_settings.read_catalog",lambda *a,**k:catalog)
    app.state.provider_settings.refresh_catalog_prices()
    changed=app.state.provider_settings.connections["openai"]
    assert str(changed.input_cny_per_million)=="24" and str(changed.output_cny_per_million)=="80"
    assert changed.token==KEY and changed.context_tokens==16384 and changed.max_output_tokens==2048
    assert original.input_cny_per_million==2 and not settings.model_budget_path.exists()


def saved_mobile_profiles(owner):
    from arknights_agent.runtime.provider_settings import Connection
    settings, app, _ = owner
    manager = app.state.provider_settings
    values = dict(context_tokens=32768, max_output_tokens=4096,
                  input_cny_per_million="2", output_cny_per_million="8", token=KEY,
                  updated_at=datetime.now(UTC).isoformat())
    manager.persist("deepseek", {p:Connection(protocol=p,base_url=PROVIDERS[p]["base_url"],model="fixture-model",**values)
                                 for p in ("deepseek","openai")})
    return settings, app, manager


def test_lan_can_switch_but_never_add_or_edit_credentials(owner):
    settings, app, manager = saved_mobile_profiles(owner)
    remote=TestClient(app,base_url="http://192.168.1.2",client=("192.168.1.9",1234))
    remote.headers["Origin"]="http://192.168.1.2"
    public=remote.get("/v1/account/providers").json()
    assert public["can_switch"] and not public["can_manage"]
    assert public["switch_nonce"] != app.state.credential_manager.nonce
    assert remote.get("/v1/account/credential").json()["nonce"] is None
    assert KEY not in json.dumps(public)
    old_profiles={p:c.model_dump() for p,c in manager.connections.items()}
    remote.headers["X-Model-Switch-Nonce"]=public["switch_nonce"]
    switched=remote.post("/v1/account/providers/activate",json={"protocol":"openai"})
    assert switched.status_code==200 and switched.json()["active"]=="openai"
    assert switched.json()["can_switch"] and not switched.json()["can_manage"]
    assert {p:c.model_dump() for p,c in manager.connections.items()}==old_profiles
    assert ProviderSettings(settings).active=="openai"
    vault=manager.path.read_bytes()
    assert remote.post("/v1/account/providers/activate",json={"protocol":"openai"}).status_code==200
    assert manager.path.read_bytes()==vault, "same selection must not rewrite the vault"
    # Switch nonce must not authorize credential operations, even when supplied as an owner nonce.
    remote.headers["X-Credential-Nonce"]=public["switch_nonce"]
    for action in ("save","discover","remove","activate"):
        assert post(remote,action,"openai",{"token":KEY}).status_code==403
    for extra in ({"token":KEY},{"model":"other"},{"connection":{}},{"base_url":"https://evil.invalid"},{"action":"remove"}):
        assert remote.post("/v1/account/providers/activate",json={"protocol":"deepseek",**extra}).status_code==422
    assert remote.post("/v1/account/providers/activate",json={"protocol":"anthropic"}).status_code==409
    assert manager.path.read_bytes()==vault and not settings.model_budget_path.exists()


@pytest.mark.parametrize("host,peer,forwarded", [
    ("http://evil.example","192.168.1.9",{}),
    ("http://192.168.1.2","8.8.8.8",{}),
    ("http://192.168.1.2","192.168.1.9",{"X-Forwarded-For":"127.0.0.1"}),
    ("http://192.168.1.2","192.168.1.9",{"Forwarded":"for=127.0.0.1"}),
])
def test_switch_rejects_public_hosts_and_forwarded_clients(owner,host,peer,forwarded):
    _,app,manager=saved_mobile_profiles(owner)
    remote=TestClient(app,base_url=host,client=(peer,1234),headers=forwarded)
    public=remote.get("/v1/account/providers").json()
    assert not public["can_switch"] and public["switch_nonce"] is None
    remote.headers.update({"Origin":host,"X-Model-Switch-Nonce":manager.switch_nonce})
    assert remote.post("/v1/account/providers/activate",json={"protocol":"openai"}).status_code==403
    assert manager.active=="deepseek"


@pytest.mark.parametrize("headers", [
    {}, {"Origin":"http://192.168.1.2"},
    {"Origin":"null","X-Model-Switch-Nonce":"correct"},
    {"Origin":"https://evil.example","X-Model-Switch-Nonce":"correct"},
    {"Origin":"http://192.168.1.2","X-Model-Switch-Nonce":"correct","Sec-Fetch-Site":"cross-site"},
    {"Origin":"http://192.168.1.2","X-Model-Switch-Nonce":"wrong"},
])
def test_switch_requires_its_own_nonce_and_same_origin(owner,headers):
    _,app,manager=saved_mobile_profiles(owner)
    headers={k:manager.switch_nonce if v=="correct" else v for k,v in headers.items()}
    remote=TestClient(app,base_url="http://192.168.1.2",client=("192.168.1.9",1234))
    assert remote.post("/v1/account/providers/activate",headers=headers,json={"protocol":"openai"}).status_code==403
    assert manager.active=="deepseek"


def test_expired_switch_session_rejects_before_mutation_then_refreshes(owner):
    settings,app,manager=saved_mobile_profiles(owner)
    remote=TestClient(app,base_url="http://192.168.1.2",client=("192.168.1.9",1234))
    remote.headers.update({"Origin":"http://192.168.1.2", "X-Model-Switch-Nonce":manager.switch_nonce})
    vault=manager.path.read_bytes()
    manager.switch_nonce="rotated-synthetic-session"
    failed=remote.post("/v1/account/providers/activate",json={"protocol":"openai"})
    assert failed.status_code==403 and failed.json()["detail"]=="provider_switch_session_expired"
    assert manager.active=="deepseek" and manager.path.read_bytes()==vault
    remote.headers["X-Model-Switch-Nonce"]=remote.get("/v1/account/providers").json()["switch_nonce"]
    remote.headers["Origin"]="null"
    rejected=remote.post("/v1/account/providers/activate",json={"protocol":"openai"})
    assert rejected.status_code==403 and rejected.json()["detail"]=="provider_switch_origin_rejected"
    assert manager.path.read_bytes()==vault
    remote.headers["Origin"]="http://192.168.1.2"
    assert remote.post("/v1/account/providers/activate",json={"protocol":"openai"}).status_code==200
    assert manager.active=="openai" and not settings.model_budget_path.exists()
