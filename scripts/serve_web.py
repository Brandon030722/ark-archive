"""Serve the local web app with an explicitly configured shared paid policy.

Only the managed workbench renews its public proofs. Paid queries begin when
a user submits a real-model task. Stale evidence leaves paid requests unavailable.
"""

from __future__ import annotations

import argparse
import threading
from contextlib import ExitStack, asynccontextmanager
from datetime import UTC, datetime
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

import httpx
import uvicorn

from arknights_agent.api import create_app
from arknights_agent.config import Settings
from arknights_agent.runtime.budget import BudgetError
from arknights_agent.runtime.providers import DeepSeekProvider
from arknights_agent.runtime.stage40_budget import load_runtime
from arknights_agent.runtime.wire_journal import recording_provider


def load_live(args, configured, pricing, model, provider_factory=None):
    book, profile, settings = load_runtime(
        configured, pricing_sources=pricing, current_model_sources=model,
        legacy_source_root=args.legacy_source_root, request_limit=args.request_limit,
        unlimited=args.unlimited, semantic_review=args.semantic_review,
        model_first=args.unlimited and args.semantic_review,
    )
    return {
        "settings": settings.model_copy(update={"agent_execution_engine": args.execution_engine}),
        "ledger": book, "profile": profile, "journal_path": str(args.journal),
        # All generations append via the same lock and sequence counter.
        "provider_factory": provider_factory or recording_provider(DeepSeekProvider, book, args.journal),
    }


def manage_proofs(app, args, configured):
    from scripts.workbench_proofs import RENEW_BEFORE, PriceSourceChanged, expiry, refresh

    stop = threading.Event()
    wake = threading.Event()
    app.state.credential_changed = wake.set
    app.state.runtime_live_startup_reason = "proof_refresh_in_progress"
    previous_lifespan = app.router.lifespan_context
    root = args.managed_proofs.resolve()

    def install(bundle):
        old = getattr(app.state, "workbench_runtime", {})
        current = app.state.credential_manager.apply(configured)
        binding = load_live(args, current, bundle / "pricing", bundle / "model", old.get("provider_factory"))
        app.state.workbench_runtime = binding
        return expiry(binding["settings"].model_price_verified_at.isoformat())

    def worker():
        deadline = None
        try:
            deadline = install((root / "current").resolve(strict=True))
        except (OSError, BudgetError):
            pass
        while not stop.is_set():
            try:
                app.state.provider_settings.refresh_catalog_prices()
            except (OSError, ValueError):
                # Keep the previous encrypted configuration and its timestamp.
                print("Model catalog renewal unavailable", flush=True)
            now = datetime.now(UTC)
            changed = wake.is_set()
            wake.clear()
            if changed or deadline is None or now >= deadline - RENEW_BEFORE:
                try:
                    if changed and deadline and now < deadline - RENEW_BEFORE:
                        deadline = install((root / "current").resolve(strict=True))
                    else:
                        deadline = install(refresh(root, args.current_model_sources))
                except PriceSourceChanged:
                    reason = "price_source_changed"
                except BudgetError as error:
                    reason = ("configuration_missing" if error.code == "configured_provider_missing"
                              else "runtime_profile_unavailable")
                except (OSError, ValueError, httpx.HTTPError):
                    reason = "proof_refresh_failed"
                else:
                    reason = None
                app.state.runtime_live_startup_reason = reason
                if reason:
                    print(f"DeepSeek proof renewal: {reason}", flush=True)
            wake.wait(1800)

    @asynccontextmanager
    async def lifespan(application):
        async with previous_lifespan(application):
            thread = threading.Thread(target=worker, name="workbench-proof-renewal", daemon=True)
            thread.start()
            try:
                yield
            finally:
                stop.set()
                wake.set()

    app.router.lifespan_context = lifespan


def build_app(args):
    configured = Settings()
    configured = configured.model_copy(update={
        "credential_store_path": configured.credential_store_path or Path(".local/credentials/deepseek.enc"),
        "credential_master_key_path": configured.credential_master_key_path or
        Path.home() / ".config/arknights-agent/master.key",
    })
    from arknights_agent.runtime.credentials import CredentialManager

    configured = CredentialManager(configured).apply(configured)
    if not args.live:
        return create_app(configured.model_copy(update={"model_live_enabled": False}))
    if getattr(args, "personal", False):
        app = create_app(configured.model_copy(update={"model_live_enabled": True}))
        app.state.runtime_live_startup_reason = "configuration_missing"
        stop = threading.Event()
        previous = app.router.lifespan_context

        def renew_prices():
            while not stop.is_set():
                try:
                    app.state.provider_settings.refresh_catalog_prices()
                except (OSError, ValueError, httpx.HTTPError):
                    print("Could not renew public catalog prices", flush=True)
                stop.wait(6 * 3600)

        @asynccontextmanager
        async def lifespan(application):
            async with previous(application):
                thread = threading.Thread(target=renew_prices, daemon=True)
                thread.start()
                try:
                    yield
                finally:
                    stop.set()

        app.router.lifespan_context = lifespan
        return app
    if getattr(args, "managed_proofs", None) and args.current_model_sources:
        app = create_app(configured.model_copy(update={"model_live_enabled": True}))
        manage_proofs(app, args, configured)
        return app
    if not args.pricing_sources or not args.current_model_sources:
        raise ValueError("live requires explicit verified pricing and model source directories")

    def enable_credential_reload(app):
        def reload_runtime():
            current = app.state.credential_manager.apply(configured)
            old = getattr(app.state, "workbench_runtime", {})
            factory = old.get("provider_factory", getattr(app.state, "runtime_provider_factory", None))
            try:
                binding = load_live(args, current, args.pricing_sources, args.current_model_sources, factory)
            except BudgetError as error:
                app.state.runtime_live_startup_reason = (
                    "configuration_missing" if error.code == "configured_provider_missing"
                    else "runtime_policy_unavailable")
            except (OSError, ValueError):
                app.state.runtime_live_startup_reason = "runtime_profile_unavailable"
            else:
                app.state.workbench_runtime = binding
                app.state.runtime_live_startup_reason = None
        app.state.credential_changed = reload_runtime
        return app

    try:
        book, profile, live_settings = load_runtime(
            configured,
            pricing_sources=args.pricing_sources,
            current_model_sources=args.current_model_sources,
            legacy_source_root=args.legacy_source_root,
            request_limit=args.request_limit,
            unlimited=getattr(args, "unlimited", False),
            semantic_review=getattr(args, "semantic_review", False),
            model_first=bool(getattr(args, "unlimited", False) and getattr(args, "semantic_review", False)),
        )
    except BudgetError as exc:
        if not getattr(args, "keep_web_available", False):
            raise
        startup_reason = {
            "stage26_official_price_not_fresh": "price_verification_expired",
            "stage26_official_price_invalid": "price_verification_invalid",
            "configured_provider_missing": "configuration_missing",
        }.get(exc.code, "runtime_policy_unavailable")
        app = create_app(configured.model_copy(update={"model_live_enabled": True}))
        app.state.runtime_live_startup_reason = startup_reason
        return enable_credential_reload(app)
    live_settings = live_settings.model_copy(update={
        "agent_execution_engine": getattr(args, "execution_engine", "langgraph")
    })
    app = create_app(live_settings)
    app.state.runtime_settings = live_settings
    app.state.runtime_budget_ledger = book
    app.state.runtime_budget_profile = profile
    app.state.runtime_provider_factory = recording_provider(DeepSeekProvider, book, args.journal)
    return enable_credential_reload(app)


def lan_ipv4(value: str) -> str:
    private_networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    try:
        address = IPv4Address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use the Mac's private IPv4 address") from exc
    if not any(address in IPv4Network(network) for network in private_networks):
        raise argparse.ArgumentTypeError("use the Mac's private IPv4 address")
    return str(address)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--personal", action="store_true", help="use locally managed provider profiles")
    parser.add_argument("--keep-web-available", action="store_true",
                        help="keep the archive visible if paid-model proof checks fail at startup")
    parser.add_argument("--pricing-sources", type=Path)
    parser.add_argument("--current-model-sources", type=Path)
    parser.add_argument("--managed-proofs", type=Path, help="renew weekly proof bundles in the background")
    parser.add_argument(
        "--legacy-source-root", type=Path, default=Path(".local/stage18-official-sources")
    )
    parser.add_argument("--journal", type=Path, default=Path(".local/stage40-web-live"))
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--lan-host", type=lan_ipv4, metavar="PRIVATE_IPV4",
        help="also listen on this LAN IPv4 address for devices on a trusted network",
    )
    parser.add_argument("--request-limit", type=int, default=27,
                        help="Cumulative web attempt limit; keeps every historical request count")
    parser.add_argument("--unlimited", action="store_true",
                        help="Explicitly waive spending and attempt caps, preserving accounting, full holds and unknown freezes")
    parser.add_argument("--semantic-review", action="store_true",
                        help="Require a separately accounted model review before delivering typed source answers")
    parser.add_argument("--execution-engine", choices=("legacy", "langgraph"), default="langgraph",
                        help="Model-first orchestration; legacy remains an explicit rollback option")
    args = parser.parse_args()
    app = build_app(args)
    config = uvicorn.Config(app, host="127.0.0.1", port=args.port)
    server = uvicorn.Server(config)
    if args.lan_host is None:
        server.run()
        return
    with ExitStack() as listeners:
        loopback = listeners.enter_context(config.bind_socket())
        port = loopback.getsockname()[1]
        lan = listeners.enter_context(uvicorn.Config(app, host=args.lan_host, port=port).bind_socket())
        server.run(sockets=[loopback, lan])


if __name__ == "__main__":
    main()
