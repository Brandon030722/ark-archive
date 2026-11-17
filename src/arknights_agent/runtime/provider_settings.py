"""Owner-local credentials; LAN clients can select an already saved profile."""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from ipaddress import ip_address, ip_network
from typing import Literal

import httpx
from cryptography.fernet import Fernet
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from arknights_agent.config import Settings, semantic_review_required_seconds

from .credentials import (
    CredentialStore,
    _atomic_private,
    _read_private,
    owner_local,
    validate_token,
)
from .multi_provider import validate_base_url
from .provider_registry import MODEL_ID, PROVIDERS, model_defaults, read_catalog


def model_switch_client(request: Request) -> bool:
    """Only direct local/LAN access; no forwarded host or public DNS names."""
    def local_address(value):
        address = ip_address(value)
        ranges = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
        return address.is_loopback or any(address in ip_network(net) for net in ranges)
    try:
        host = request.url.hostname
        return bool(request.client and local_address(request.client.host)
                    and (host == "localhost" or local_address(host))
                    and not any(name in request.headers for name in
                                ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto")))
    except (ValueError, TypeError):
        return False


class Connection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: Literal["deepseek", "compatible", "openai", "anthropic"]
    base_url: str = Field(max_length=500)
    model: str = Field(min_length=1, max_length=150, pattern=r"^[A-Za-z0-9_.:/-]+$")
    context_tokens: int = Field(ge=4096, le=1048576, strict=True)
    max_output_tokens: int = Field(ge=1024, le=8192, strict=True)
    input_cny_per_million: Decimal = Field(gt=0, le=100000)
    output_cny_per_million: Decimal = Field(gt=0, le=100000)
    token: str = Field(min_length=16, max_length=2048, repr=False)
    updated_at: str = ""
    rate_source: str = "owner_configured_conservative_cny"
    rate_verified_at: str = ""

    def checked(self):
        validate_token(self.token)
        self.base_url = validate_base_url(self.protocol, self.base_url)
        if self.max_output_tokens >= self.context_tokens:
            raise ValueError("provider_context_invalid")
        return self

    def public(self):
        return {**self.model_dump(mode="json", exclude={"token"}), "configured": True}


class ProviderSettings:
    def __init__(self, settings, legacy=None):
        self.lock = threading.RLock()
        self.path = (
            settings.credential_store_path.with_name("providers.enc")
            if settings.credential_store_path
            else None
        )
        self.key_path = settings.credential_master_key_path
        self.active, self.connections, self.error = "deepseek", {}, None
        self.drafts = {}
        # Separate capability: this nonce can only activate an existing profile.
        self.switch_nonce = secrets.token_urlsafe(32)
        self.legacy_adopted = False
        if self.path and (self.path.exists() or self.path.is_symlink()):
            try:
                payload = json.loads(
                    Fernet(_read_private(self.key_path)).decrypt(_read_private(self.path))
                )
                if set(payload) != {"active", "connections"} or payload["active"] not in {
                    None, "deepseek",
                    *payload["connections"],
                }:
                    raise ValueError()
                self.connections = {
                    k: Connection.model_validate(v).checked()
                    for k, v in payload["connections"].items()
                }
                if any(k != v.protocol for k, v in self.connections.items()):
                    raise ValueError()
                self.active = payload["active"]
                for c in self.connections.values():
                    if datetime.fromisoformat(c.updated_at).tzinfo is None:
                        raise ValueError()
            except Exception:
                self.error = "provider_configuration_unavailable"
        elif legacy and legacy.key and settings.api_protocol == "deepseek":
            # Adopt legacy credentials in memory. The next deliberate profile
            # edit persists all four together; the old vault and ledger stay intact.
            try:
                self.connections["deepseek"] = Connection(
                    protocol="deepseek", base_url=PROVIDERS["deepseek"]["base_url"],
                    model=settings.deepseek_model or "deepseek-flash",
                    token=legacy.key.get_secret_value(), context_tokens=settings.agent_context_tokens,
                    max_output_tokens=settings.agent_max_output_tokens,
                    input_cny_per_million=settings.model_input_cny_per_million,
                    output_cny_per_million=settings.model_output_cny_per_million,
                    updated_at=(settings.model_price_verified_at or datetime.now(UTC)).isoformat(),
                    rate_source="legacy_verified_price",
                ).checked()
                self.legacy_adopted = True
            except (ValueError, TypeError):
                pass

    def persist(self, active, connections):
        if not self.path or not self.key_path:
            raise ValueError("provider_management_unavailable")
        # Reuse private-key creation without ever overwriting the legacy vault.
        if not self.key_path.exists():
            bootstrap = CredentialStore(
                self.path.with_name(".provider-bootstrap.enc"), self.key_path
            )
            bootstrap.write(None)
            bootstrap.path.unlink()
        data = {
            "active": active,
            "connections": {k: v.model_dump(mode="json") for k, v in connections.items()},
        }
        _atomic_private(
            self.path, Fernet(_read_private(self.key_path)).encrypt(json.dumps(data).encode())
        )
        self.active, self.connections, self.error = active, connections, None
        self.legacy_adopted = False

    def public(self, manage=False, *, switch=False):
        with self.lock:
            can_switch = bool(switch and self.path and self.key_path and not self.error)
            return {
                "active": self.active,
                "connections": {k: v.public() for k, v in self.connections.items()},
                "can_manage": bool(manage and self.path),
                "can_switch": can_switch,
                "switch_nonce": self.switch_nonce if can_switch else None,
                "error": self.error,
                "protocols": list(PROVIDERS), "providers": PROVIDERS,
            }

    def refresh_catalog_prices(self):
        """Weekly public metadata renewal; never probes a model or changes keys."""
        with self.lock:
            needed = any(c.rate_source == "models_dev_estimate_cny8" for c in self.connections.values())
        if not needed:
            return
        catalog = read_catalog(self.path.with_name("model-catalog.json") if self.path else None, refresh=True)
        if catalog.get("stale"):
            return
        with self.lock:
            changed, connections = False, dict(self.connections)
            for protocol, c in connections.items():
                defaults = model_defaults(protocol, c.model, catalog)
                if c.rate_source != "models_dev_estimate_cny8" or not defaults or c.rate_verified_at == catalog["checked_at"]:
                    continue
                connections[protocol] = c.model_copy(update={
                    "input_cny_per_million": Decimal(defaults["input_cny_per_million"]),
                    "output_cny_per_million": Decimal(defaults["output_cny_per_million"]),
                    "rate_verified_at": catalog["checked_at"],
                })
                changed = True
            if changed:
                self.persist(self.active, connections)

    def apply(self, settings):
        with self.lock:
            if self.error:
                return settings.model_copy(update={"model_live_enabled": False})
            # Startup proof loading is asynchronous; import once it supplies
            # the verified legacy model/rates, but never resurrect a removed key.
            if (not self.connections and self.active == "deepseek" and self.path
                    and not self.path.exists() and settings.deepseek_api_key
                    and settings.api_protocol == "deepseek"
                    and settings.model_input_cny_per_million and settings.model_output_cny_per_million
                    and settings.model_price_verified_at):
                from types import SimpleNamespace
                adopted = ProviderSettings(settings, SimpleNamespace(key=settings.deepseek_api_key))
                self.connections, self.legacy_adopted = adopted.connections, adopted.legacy_adopted
            if self.active is None:
                return settings.model_copy(update={"deepseek_api_key": None})
            if self.active == "deepseek" and self.active not in self.connections:
                return settings
            c = self.connections[self.active]
            if self.legacy_adopted and c.protocol == "deepseek":
                if not settings.deepseek_api_key:
                    return settings.model_copy(update={"deepseek_api_key": None})
                c = c.model_copy(update={"token": settings.deepseek_api_key.get_secret_value()})
            # Legacy weekly verification can refresh independently of a profile
            # edit, without changing its key, model or historical receipt prices.
            verified_at = datetime.fromisoformat(c.rate_verified_at or c.updated_at)
            input_price, output_price = c.input_cny_per_million, c.output_cny_per_million
            if c.rate_source == "legacy_verified_price" and settings.deepseek_model == c.model and settings.model_price_verified_at:
                verified_at = settings.model_price_verified_at
                input_price, output_price = settings.model_input_cny_per_million, settings.model_output_cny_per_million
            calls, tools = settings.agent_progress_max_calls, settings.agent_progress_max_tools
            return Settings.model_validate(
                {
                    **settings.model_dump(),
                    "api_protocol": c.protocol,
                    "api_connection_id": "profile:" + self.active,
                    "api_base_url": c.base_url,
                    "deepseek_model": c.model,
                    "deepseek_api_key": SecretStr(c.token),
                    "agent_context_tokens": c.context_tokens,
                    "agent_max_output_tokens": c.max_output_tokens,
                    "agent_large_output_enabled": True,
                    "agent_model_first": True,
                    "agent_semantic_review": True,
                    "agent_execution_engine": "langgraph",
                    "agent_roguelike_delivery": "verified_query",
                    "agent_max_model_calls": calls,
                    "agent_max_tool_calls": tools,
                    "deepseek_timeout_seconds": 60,
                    "agent_max_prompt_bytes": 8 * 1024 * 1024,
                    "tool_result_max_bytes": max(262144, settings.tool_result_max_bytes),
                    "agent_max_total_tokens": min(34_000_000, c.context_tokens * calls),
                    "agent_max_total_seconds": semantic_review_required_seconds(
                        model_calls=calls,
                        provider_timeout_seconds=60,
                        tool_calls=tools,
                        database_timeout_ms=settings.database_tool_timeout_ms,
                        web_search_enabled=settings.web_search_enabled,
                        web_timeout_seconds=settings.web_search_timeout_seconds,
                    ),
                    "model_input_cny_per_million": input_price,
                    "model_output_cny_per_million": output_price,
                    "model_price_source": c.rate_source,
                    "model_price_verified_at": verified_at,
                }
            )


def discover_models(connection, *, transport=None):
    headers = {"Authorization": f"Bearer {connection.token}"}
    suffix = "/models"
    if connection.protocol == "anthropic":
        headers = {"x-api-key": connection.token, "anthropic-version": "2023-06-01"}
    try:
        with httpx.Client(
            timeout=10, follow_redirects=False, trust_env=False, transport=transport
        ) as client:
          rows, cursor, complete = [], None, False
          for _page in range(5):
            params = {"limit": 100, **({"after_id": cursor} if cursor else {})} if connection.protocol == "anthropic" else None
            with client.stream("GET", connection.base_url + suffix, headers=headers, params=params) as response:
                if response.status_code != 200:
                    return {
                        "ok": False,
                        "reason": "authentication_failed"
                        if response.status_code in {401, 403}
                        else "models_endpoint_unavailable",
                    }
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 1048576:
                        raise ValueError()
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError()
                models = body.get("data")
                if not isinstance(models, list):
                    raise ValueError()
                rows.extend({"id": m["id"], "name": str(m.get("display_name", m["id"]))[:160]}
                            for m in models if isinstance(m, dict) and isinstance(m.get("id"), str)
                            and MODEL_ID.fullmatch(m["id"]) and connection.token not in m["id"]
                            and connection.token not in str(m.get("display_name", "")))
                if not body.get("has_more"):
                    complete = True
                    break
                next_cursor = body.get("last_id")
                if not isinstance(next_cursor, str) or not MODEL_ID.fullmatch(next_cursor) or next_cursor == cursor:
                    break
                cursor = next_cursor
          unique = {m["id"]: m for m in rows}
          return {"ok": True, "models": list(unique.values()), "complete": complete, "paid_generation_tested": False}
    except (httpx.HTTPError, ValueError, TypeError):
        return {"ok": False, "reason": "connection_failed"}


def check_connection(connection, *, transport=None):
    result = discover_models(connection, transport=transport)
    if not result["ok"]:
        return result
    return {"ok": True, "model_listed": any(m["id"] == connection.model for m in result["models"]), "paid_generation_tested": False}


def install_provider_settings(app, settings):
    manager = ProviderSettings(settings, app.state.credential_manager)
    app.state.provider_settings = manager
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}

    @app.get("/v1/account/providers")
    def status(request: Request):
        from .credentials import effective_settings
        effective_settings(app, settings)
        return JSONResponse(manager.public(owner_local(request), switch=model_switch_client(request)), headers=headers)

    @app.post("/v1/account/providers/activate")
    async def activate_saved(request: Request):
        # No connection, endpoint, model ID, price or key fields are accepted.
        # Keep credential mutations on the separate owner-local route below.
        rejection = None
        if not model_switch_client(request):
            rejection = "provider_switch_client_forbidden"
        elif (request.headers.get("origin") != f"{request.url.scheme}://{request.url.netloc}"
              or request.headers.get("sec-fetch-site", "same-origin") != "same-origin"):
            rejection = "provider_switch_origin_rejected"
        elif not secrets.compare_digest(request.headers.get("x-model-switch-nonce", ""), manager.switch_nonce):
            rejection = "provider_switch_session_expired"
        if rejection:
            # Classify without logging headers, keys, nonce or request body.
            logging.getLogger(__name__).warning("model_switch_rejected: %s", rejection)
            raise HTTPException(403, rejection)
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "provider_invalid_request")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 1024:
                raise HTTPException(413, "provider_invalid_request")
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) != {"protocol"} or data["protocol"] not in PROVIDERS:
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(422, "provider_invalid_request") from None
        from starlette.concurrency import run_in_threadpool

        def select():
            with manager.lock:
                if manager.error or not manager.path or not manager.key_path:
                    raise HTTPException(409, "provider_configuration_unavailable")
                if data["protocol"] not in manager.connections:
                    raise HTTPException(409, "provider_not_configured")
                if manager.active != data["protocol"]:
                    try:
                        manager.persist(data["protocol"], manager.connections)
                    except (ValueError, OSError):
                        raise HTTPException(503, "provider_activation_unconfirmed") from None
                return manager.public(owner_local(request), switch=True)
        return JSONResponse(await run_in_threadpool(select), headers=headers)

    @app.post("/v1/account/providers")
    async def change(request: Request):
        cm = app.state.credential_manager
        if (
            not owner_local(request)
            or request.headers.get("origin") != f"{request.url.scheme}://{request.url.netloc}"
            or request.headers.get("sec-fetch-site", "same-origin") != "same-origin"
            or not secrets.compare_digest(request.headers.get("x-credential-nonce", ""), cm.nonce)
        ):
            raise HTTPException(403, "credential_owner_required")
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "provider_invalid_request")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 8192:
                raise HTTPException(413, "provider_invalid_request")
        from starlette.concurrency import run_in_threadpool

        def perform(data):
            with manager.lock:
                if (
                    set(data) != {"action", "protocol", "connection", "acknowledged"}
                    or data["acknowledged"] is not True
                ):
                    raise ValueError()
                action, protocol = data["action"], data["protocol"]
                if protocol not in PROVIDERS:
                    raise ValueError()
                connections = dict(manager.connections)
                if action in {"save", "check", "discover"}:
                    if not isinstance(data["connection"], dict):
                        raise ValueError()
                    values = dict(data["connection"])
                    if values.get("protocol") != protocol:
                        raise ValueError()
                    values["base_url"] = validate_base_url(protocol, values.get("base_url", ""))
                    ticket = values.pop("draft_id", None)
                    manager.drafts = {k: v for k, v in manager.drafts.items() if v["expires"] > time.monotonic()}
                    if ticket:
                        draft = manager.drafts.get(ticket)
                        if not draft or (draft["protocol"], draft["base_url"]) != (protocol, values["base_url"]):
                            raise ValueError("provider_draft_expired")
                        values["token"] = draft["token"]
                    if not values.get("token") and protocol in connections:
                        # Reuse only this exact connection's key on its original host.
                        old = connections[protocol]
                        if validate_base_url(protocol, values.get("base_url")) != old.base_url:
                            raise ValueError("provider_new_host_requires_key")
                        values["token"] = old.token
                    validate_token(values.get("token", ""))
                    if action == "discover":
                        from types import SimpleNamespace
                        result = discover_models(SimpleNamespace(**values))
                        if not result["ok"]:
                            return result
                        catalog = read_catalog(manager.path.with_name("model-catalog.json") if manager.path else None, refresh=True)
                        for model in result["models"]:
                            model["defaults"] = model_defaults(protocol, model["id"], catalog)
                            meta = catalog.get("providers", {}).get(protocol, {}).get(model["id"])
                            model["supported"] = None if meta is None else bool(meta.get("tool_call") and meta.get("text_output") and meta.get("supported_wire") is not False and meta.get("status") != "retired")
                        # Opaque server-side draft survives check -> save. Keys
                        # never return to the browser or enter its localStorage.
                        ticket = secrets.token_urlsafe(24)
                        manager.drafts = {k: v for k, v in manager.drafts.items() if v["protocol"] != protocol}
                        manager.drafts[ticket] = {"protocol": protocol, "base_url": values["base_url"], "token": values["token"], "expires": time.monotonic() + 600}
                        return {**result, "draft_id": ticket, "catalog_source": catalog.get("source"), "catalog_stale": catalog.get("stale", False)}
                    model = values.get("model", "")
                    catalog = read_catalog(manager.path.with_name("model-catalog.json") if manager.path else None)
                    defaults = model_defaults(protocol, model, catalog)
                    meta = catalog.get("providers", {}).get(protocol, {}).get(model)
                    if meta and (not meta.get("tool_call") or not meta.get("text_output") or meta.get("supported_wire") is False or meta.get("status") == "retired"):
                        raise ValueError("provider_model_unsupported")
                    fields = {"context_tokens", "max_output_tokens", "input_cny_per_million", "output_cny_per_million"}
                    provided = {k: values[k] for k in fields if values.get(k) not in (None, "")}
                    if len(provided) < len(fields):
                        if not defaults:
                            raise ValueError("provider_metadata_required")
                        values.update({**defaults, **provided})
                        values["rate_source"] = "models_dev_estimate_cny8"
                    # Client cannot self-certify official pricing or a catalog source.
                    elif defaults and all(str(values[k]) == str(defaults[k]) for k in fields):
                        values["rate_source"] = "models_dev_estimate_cny8"
                    else:
                        values["rate_source"] = "owner_configured_conservative_cny"
                    old = connections.get(protocol)
                    if old and old.model == model and old.base_url == values["base_url"] and all(Decimal(str(values[k])) == Decimal(str(getattr(old, k))) for k in fields):
                        values["rate_source"] = old.rate_source
                    connection = Connection.model_validate(values).checked()
                    if action == "check":
                        return check_connection(connection)
                    connection.updated_at = datetime.now(UTC).isoformat()
                    connection.rate_verified_at = catalog["checked_at"] if connection.rate_source == "models_dev_estimate_cny8" else connection.updated_at
                    if old and old.model == connection.model and old.base_url == connection.base_url and connection.rate_source == old.rate_source and all(getattr(connection, k) == getattr(old, k) for k in fields):
                        connection.rate_verified_at = old.rate_verified_at or old.updated_at
                    connections[protocol] = connection
                    manager.persist(protocol, connections)
                    if ticket:
                        manager.drafts.pop(ticket, None)
                elif action == "activate":
                    if protocol not in connections:
                        raise ValueError()
                    manager.persist(protocol, connections)
                elif action == "remove":
                    connections.pop(protocol, None)
                    manager.persist(
                        next(iter(connections), None) if manager.active == protocol else manager.active, connections
                    )
                else:
                    raise ValueError()
                return manager.public(True, switch=model_switch_client(request))

        try:
            data = json.loads(raw)
            result = await run_in_threadpool(perform, data)
        except Exception as exc:
            code = (
                str(exc)
                if str(exc)
                in {
                    "provider_new_host_requires_key",
                    "provider_endpoint_invalid",
                    "provider_endpoint_mismatch",
                    "provider_context_invalid",
                    "provider_draft_expired", "provider_metadata_required", "provider_model_unsupported",
                }
                else "provider_invalid_configuration"
            )
            return JSONResponse({"detail": code}, status_code=422, headers=headers)
        return JSONResponse(result, headers=headers)
