"""HTTP entry points for bounded chat and release-pinned read capabilities."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from arknights_agent import agent, services
from arknights_agent.config import Settings
from arknights_agent.errors import InvalidData
from arknights_agent.runtime.security import contains_secret, redact
from arknights_agent.runtime.tools import ToolContext, execute_tool

_MODULE = Literal["auto", "operator", "recruitment", "garrison", "roguelike"]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("*", mode="after", check_fields=False)
    @classmethod
    def no_blank_strings(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("blank input")
        return value


class ChatRequest(_Request):
    question: str = Field(min_length=1, max_length=2000)
    release_id: str | None = Field(default=None, min_length=1, max_length=128)
    module: _MODULE = "auto"
    # Every conversational entry uses the model unless a caller explicitly
    # requests the local diagnostic backend. Readiness failures never select it.
    backend: Literal["local", "deepseek"] = "deepseek"
    previous_run_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )


class ChatTaskRequest(ChatRequest):
    backend: Literal["local", "deepseek"] = "deepseek"
    request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    conversation_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class QueryRequest(_Request):
    release_id: str | None = Field(default=None, min_length=1, max_length=128)
    module: Literal["operator", "recruitment", "garrison", "roguelike"]
    kind: str | None = Field(default=None, min_length=1, max_length=80)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    limit: int = Field(default=20, ge=1, le=50)
    offset: int = Field(default=0, ge=0, le=10000)


class EvidenceRequest(_Request):
    release_id: str | None = Field(default=None, min_length=1, max_length=128)
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class SearchRequest(_Request):
    release_id: str | None = Field(default=None, min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=5, ge=1, le=5)


class RagRequest(_Request):
    release_id: str | None = Field(default=None, min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=2000)
    theme_id: (
        Literal["act12d6", "rogue_1", "rogue_2", "rogue_3", "rogue_4", "rogue_5", "rogue_6"] | None
    ) = None
    limit: int = Field(default=5, ge=1, le=5)


class WikiReadRequest(_Request):
    release_id: str | None = Field(default=None, min_length=1, max_length=128)
    revision: int = Field(ge=1)
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=3000, ge=1, le=6000)


def _public_input(body: BaseModel):
    if contains_secret(body.model_dump()):
        raise InvalidData("invalid_input")


def install_routes(app: FastAPI, settings: Settings):
    """Register routes without loading keys, opening providers or writing state."""
    if getattr(app.state, "runtime_routes_installed", False):
        return
    app.state.runtime_routes_installed = True
    from arknights_agent.runtime.account_overview import AccountBalanceCache, local_usage
    from arknights_agent.runtime.credentials import effective_settings, install_credential_routes

    install_credential_routes(app, settings)
    from arknights_agent.runtime.provider_settings import install_provider_settings
    install_provider_settings(app, settings)
    account_cache = AccountBalanceCache()

    @app.get("/v1/account/overview")
    def account_overview(refresh: bool = False):
        from arknights_agent.runtime.provider_registry import PROVIDERS
        configured = effective_settings(app, settings)
        return JSONResponse(
            {"provider": configured.api_protocol, "model": configured.deepseek_model,
             "account": account_cache.snapshot(configured.deepseek_api_key, refresh=refresh)
             if PROVIDERS[configured.api_protocol]["balance"] else {"state":"unsupported", "balance":None,
                 "error":"provider_balance_unsupported", "checked_at":None},
             "usage": local_usage(configured.model_budget_path)},
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/v1/agent/context-usage")
    def get_context_usage(
        task_id: str | None = Query(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
        conversation_id: str | None = Query(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
    ):
        from arknights_agent.runtime.context_usage import context_usage

        if bool(task_id) != bool(conversation_id):
            raise HTTPException(422, "context_scope_required")
        configured = effective_settings(app, settings)
        task_store = getattr(app.state, "runtime_chat_task_store", None)
        tasks_path = task_store.path if task_store else settings.snapshot_root.parent / "http-chat-tasks.sqlite3"
        return JSONResponse(
            context_usage(configured.model_budget_path, tasks_path,
                          limit_tokens=configured.agent_context_tokens,
                          task_id=task_id, conversation_id=conversation_id,
                          legacy_limit_tokens=getattr(app.state, "workbench_runtime", {}).get("settings", settings).agent_context_tokens),
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    def live_runtime(task_id):
        from arknights_agent.runtime.context_profile import CurrentContextToolProfile
        from arknights_agent.runtime.long_context import LongContextToolProfile
        from arknights_agent.runtime.stage26_model_profile import CurrentModelToolProfile
        from arknights_agent.runtime.stage40_budget import Stage40BudgetLedger

        managed = getattr(app.state, "workbench_runtime", {})
        configured = effective_settings(app, settings)
        if getattr(app.state.provider_settings, "error", None):
            raise HTTPException(503, "provider_configuration_unavailable")
        if configured.api_connection_id.startswith("profile:") or configured.api_protocol != "deepseek":
            from arknights_agent.runtime.multi_provider import MultiProvider
            from arknights_agent.runtime.provider_budget import ProviderLedger, ProviderProfile
            from arknights_agent.runtime.runner import runtime_status
            from arknights_agent.runtime.wire_journal import recording_provider
            configured = configured.model_copy(update={"agent_execution_engine":"langgraph",
                "agent_model_first":True, "agent_semantic_review":True})
            status = runtime_status(configured)["model"]
            if not status["ready"]:
                raise HTTPException(503, status["reason"])
            try:
                ledger = ProviderLedger(configured, task_id)
                ready = ledger.available()
            except Exception:
                raise HTTPException(503, "runtime_policy_unavailable") from None
            if not ready["ready"]:
                raise HTTPException(503, ready["reason"])
            journal = managed.get("journal_path", ".local/stage40-web-live/api")
            def factory(cfg):
                return recording_provider(MultiProvider, ledger, journal)(cfg)
            return {"settings":configured, "ledger":ledger, "profile":ProviderProfile(configured),
                    "provider_factory":factory, "policy":None}
        startup_reason = getattr(app.state, "runtime_live_startup_reason", None)
        if startup_reason is not None and not managed:
            raise HTTPException(503, startup_reason)
        ledger = managed.get("ledger", getattr(app.state, "runtime_budget_ledger", None))
        profile = managed.get("profile", getattr(app.state, "runtime_budget_profile", None))
        if not isinstance(ledger, Stage40BudgetLedger) or not (
            isinstance(profile, CurrentModelToolProfile)
            or type(profile) is CurrentContextToolProfile
            or type(profile) is LongContextToolProfile
        ):
            raise HTTPException(503, "runtime_policy_unavailable")
        if not configured.model_live_enabled:
            raise HTTPException(503, "live_disabled")
        try:
            readiness = ledger.available()
        except Exception:
            raise HTTPException(503, "runtime_policy_unavailable") from None
        if not readiness.get("ready"):
            reason = readiness.get("reason", "runtime_policy_unavailable")
            if managed and reason == "price_verification_expired" and startup_reason:
                reason = startup_reason
            raise HTTPException(503, reason)
        if managed and startup_reason == "price_source_changed":
            raise HTTPException(503, startup_reason)
        # Each profile retains its own source contract. Current context uses
        # the current encoder directly; historical request proofs stay intact.
        # Status checks file integrity without calling a paid endpoint.
        from arknights_agent.runtime import stage26_current_model

        try:
            stage26_current_model.current_sources(profile.current_source_root)
        except Exception:
            raise HTTPException(503, "runtime_profile_unavailable") from None
        from arknights_agent.runtime.runner import runtime_status

        configured_status = runtime_status(configured)["model"]
        if not configured_status["ready"]:
            raise HTTPException(503, configured_status["reason"])
        policy = getattr(app.state, "runtime_live_policy", None)
        task_ledger = ledger.for_task(task_id)
        task_ledger.pause_campaign_on_unknown = not configured.web_task_failure_isolation
        return {
            "settings": configured, "ledger": task_ledger, "profile": profile,
            "provider": getattr(app.state, "runtime_provider", None),
            "provider_factory": managed.get("provider_factory", getattr(app.state, "runtime_provider_factory", None)),
            "policy": policy,
        }

    def task_service():
        from arknights_agent.runtime.chat_tasks import ChatTaskService, ChatTaskStore
        from arknights_agent.runtime.runner import run_chat

        if not hasattr(app.state, "runtime_chat_task_service"):
            store = getattr(app.state, "runtime_chat_task_store", None) or ChatTaskStore(
                settings.snapshot_root.parent / "http-chat-tasks.sqlite3",
                isolate_failures=settings.web_task_failure_isolation,
            )

            def execute(payload, runtime, observer):
                return run_chat(
                    app.state.engine, app.state.snapshot_store,
                    runtime["settings"], payload["question"],
                    release_id=payload["release_id"], module=payload["module"],
                    backend=payload["backend"], previous_run_id=payload.get("previous_run_id"),
                    provider=runtime.get("provider"),
                    provider_factory=runtime.get("provider_factory"),
                    budget_ledger=runtime.get("ledger"), budget_profile=runtime.get("profile"),
                    transport=getattr(app.state, "runtime_http_transport", None),
                    worker_observer=observer,
                    progress_observer=runtime.get("publish_progress"),
                )

            def audit(payload, result):
                release = SimpleNamespace(id=payload["release_id"])
                with app.state.session_factory() as session, session.begin():
                    if "dialogue_state" not in result:
                        from arknights_agent.runtime.dialogue_context import (
                            attach_state,
                            load_previous,
                        )
                        parent = load_previous(session, payload.get("previous_run_id"), payload["release_id"], allow_general=True)
                        attach_state(result, payload["question"], parent, payload.get("previous_run_id"))
                    return agent.record_run(session, release, payload["question"], result)

            app.state.runtime_chat_task_service = ChatTaskService(store, execute, audit)
        return app.state.runtime_chat_task_service

    def submit_task(body, task_id, conversation_id):
        from arknights_agent.db import readonly_session
        from arknights_agent.errors import Conflict
        from arknights_agent.runtime.dialogue_context import load_previous

        _public_input(body)
        request = body.model_dump(exclude={"request_id", "conversation_id"})
        request_hash = services.content_digest({
            **request, "conversation_id": conversation_id,
        })
        service = task_service()
        existing = service.store.find(task_id)
        if existing:
            if existing["request_hash"] != request_hash:
                raise Conflict("task_id_conflict")
            return service.store.public(task_id)
        runtime = live_runtime(task_id) if body.backend == "deepseek" else {"settings": settings}
        policy = runtime.get("policy")
        if policy is not None:
            policy.validate(
                question=body.question, previous_run_id=body.previous_run_id,
                release_id=body.release_id, module=body.module,
            )
        # Reject forged/cross-release context before enqueueing or reserving.
        with readonly_session(
            app.state.engine, timeout_ms=settings.database_tool_timeout_ms
        ) as session:
            previous = load_previous(
                session, body.previous_run_id, body.release_id,
                allow_general=body.backend == "deepseek",
            )
            release = services.resolve_release(
                session, previous["release_id"] if previous else body.release_id
            )
            request.update(release_id=release.id, manifest_hash=release.manifest_hash)
        return service.submit(task_id, conversation_id, request_hash, request, runtime)

    def tool_run(name: str, body: BaseModel, question: str):
        from arknights_agent.db import readonly_session

        _public_input(body)
        values = body.model_dump()
        release_id = values.pop("release_id", None)
        with readonly_session(
            app.state.engine, timeout_ms=settings.database_tool_timeout_ms
        ) as session:
            release = services.resolve_release(session, release_id)
            result = execute_tool(
                name,
                values,
                ToolContext(
                    session,
                    release,
                    app.state.snapshot_store,
                    settings,
                    question,
                    transport=getattr(app.state, "runtime_http_transport", None),
                ),
            )
            result = {
                **redact(result),
                "tool": name,
                "release_id": release.id,
                "manifest_hash": release.manifest_hash,
            }
            # Closing a rollback-only read session can expire attached ORM
            # values. The already resolved release is detached and never
            # replaced with a fresh active pointer during the audit write.
            session.expunge(release)
        with app.state.session_factory() as audit_session, audit_session.begin():
            return agent.record_run(audit_session, release, question, result)

    @app.post("/v1/agent/chat")
    def chat(body: ChatRequest):
        from arknights_agent.runtime import runner
        from arknights_agent.runtime.budget import LIVE_BUDGET_PATH
        from arknights_agent.runtime.contracts import ProviderError
        from arknights_agent.runtime.providers import DeepSeekProvider
        from arknights_agent.runtime.runner import run_chat

        _public_input(body)
        # Existing synthetic tests replace the provider with MockTransport and
        # use a disposable ledger. Construction performs no network request;
        # only a verified simulation bypasses the real-campaign task policy.
        isolated_provider = None
        if body.backend == "deepseek" and (
            settings.model_budget_path.resolve() != LIVE_BUDGET_PATH.resolve()
        ):
            try:
                candidate = getattr(app.state, "runtime_provider", None) or runner.DeepSeekProvider(
                    settings
                )
                if isinstance(candidate, DeepSeekProvider) and candidate.simulation is True:
                    candidate._preflight()
                    isolated_provider = candidate
            except ProviderError:
                pass
        isolated_simulation = isolated_provider is not None
        if body.backend == "deepseek" and not isolated_simulation:
            # Legacy callers share the same task policy and stable input key.
            # A disconnected HTTP request cannot silently start another call.
            digest = services.content_digest(body.model_dump())
            task_id = "legacy_" + digest
            conversation_id = "legacy_" + (body.previous_run_id or "first_turn")
            task = submit_task(body, task_id, conversation_id)
            configured = effective_settings(app, settings)
            deadline = time.monotonic() + configured.agent_max_total_seconds
            while not task["terminal"] and time.monotonic() < deadline:
                time.sleep(0.025)
                task = task_service().store.public(task_id)
            if task["state"] == "completed":
                return task["response"]
            return JSONResponse(
                status_code=502 if task["terminal"] else 504, content=task
            )
        result = run_chat(
            app.state.engine,
            app.state.snapshot_store,
            settings,
            body.question,
            release_id=body.release_id,
            module=body.module,
            backend=body.backend,
            previous_run_id=body.previous_run_id,
            provider=isolated_provider or getattr(app.state, "runtime_provider", None),
            budget_ledger=getattr(app.state, "runtime_budget_ledger", None),
            transport=getattr(app.state, "runtime_http_transport", None),
        )
        # A wall deadline may expire before the release has been verified.
        # This result has no certified version and cannot own an audit FK.
        if result.get("status") == "timeout":
            return JSONResponse(status_code=504, content=redact(result))
        # The runner has closed all of its read-only transactions. Audit the
        # returned pinned answer without resolving the mutable active pointer.
        release = SimpleNamespace(id=result["release_id"])
        with app.state.session_factory() as audit_session, audit_session.begin():
            return agent.record_run(audit_session, release, body.question, result)

    @app.get("/v1/agent/status")
    def status():
        from arknights_agent.runtime.runner import runtime_status

        managed = getattr(app.state, "workbench_runtime", {})
        configured = effective_settings(app, settings)
        result = runtime_status(configured)
        ledger = managed.get("ledger", getattr(app.state, "runtime_budget_ledger", None))
        if getattr(ledger, "unlimited", False) is True:
            result["limits"].update(
                total_budget_cny=None, effective_budget_cny=None,
                cumulative_model_calls=None, lifetime_calls_per_question=None,
                spending_caps_enforced=False, request_caps_enforced=False,
            )
        try:
            live_runtime("status_probe")
        except HTTPException as exc:
            web_model = {"ready": False, "reason": str(exc.detail)}
        else:
            web_model = {"ready": True, "reason": "ready"}
        return {**result, "web_tasks": {"enabled": True,
                "failure_scope": "task" if configured.web_task_failure_isolation else "campaign"}, "web_model": web_model}

    @app.post("/v1/agent/tasks", status_code=202)
    def create_chat_task(body: ChatTaskRequest):
        return submit_task(body, body.request_id, body.conversation_id)

    @app.get("/v1/agent/tasks/{task_id}")
    def get_chat_task(task_id: str):
        if not task_id or len(task_id) > 128 or not all(
            ch.isascii() and (ch.isalnum() or ch in "_-") for ch in task_id
        ):
            raise HTTPException(422, "invalid_task_id")
        return task_service().store.public(task_id)

    @app.post("/v1/tools/query")
    def query(body: QueryRequest):
        _public_input(body)
        question = "只读记录查询：" + services.canonical_json(
            body.model_dump(exclude={"release_id"}, exclude_none=True)
        )
        return tool_run("query_records", body, question)

    @app.post("/v1/tools/evidence")
    def evidence(body: EvidenceRequest):
        return tool_run("read_evidence", body, "只读证据查询：" + body.id)

    @app.post("/v1/tools/web-search")
    def search(body: SearchRequest):
        return tool_run("web_search", body, body.query)

    @app.post("/v1/tools/rag-search")
    def rag_search(body: RagRequest):
        return tool_run("rag_search", body, body.query)

    @app.post("/v1/tools/public-wiki")
    def public_wiki(body: WikiReadRequest):
        return tool_run("public_wiki_read", body, "公开PRTS修订读取：" + str(body.revision))
