import argparse
import json
from collections import Counter
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from arknights_agent import services
from arknights_agent.config import get_settings
from arknights_agent.db import make_engine, session_factory
from arknights_agent.errors import DomainError
from arknights_agent.ingestion.sample import DEFAULT_COMMIT, PARSER_VERSION, fetch_sample
from arknights_agent.models import Release
from arknights_agent.runtime.security import redact
from arknights_agent.storage import SnapshotStore


def output(value):
    from arknights_agent.runtime.security import contains_secret, redact

    if contains_secret(value) or redact(value) != value:
        raise services.InvalidData("sensitive command output withheld")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def migrate():
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    command.upgrade(config, "head")


def main():
    parser = argparse.ArgumentParser(description="Versioned Arknights knowledge foundation")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("backend-status", help="show safe readiness flags; no paid model requests")
    chat = sub.add_parser("agent-chat", help="run the bounded evidence-preserving backend")
    chat.add_argument("question")
    chat.add_argument("--release")
    chat.add_argument("--module", choices=["auto", "operator", "recruitment", "garrison", "roguelike"], default="auto")
    chat.add_argument("--backend", choices=["local", "deepseek"], default="local")
    sub.add_parser("migrate", help="apply database migrations")
    ingest = sub.add_parser("ingest-sample", help="fetch fixed real-source sample and publish it")
    ingest.add_argument("--commit", default=DEFAULT_COMMIT)
    ingest.add_argument("--label")
    ingest.add_argument("--activate", action="store_true")
    ingest.add_argument("--cache-dir", default=".local/ingestion-cache")
    stage2 = sub.add_parser(
        "ingest-stage2", help="publish full operators, reviewed recruitment and phase metadata"
    )
    stage2.add_argument("--commit", default=DEFAULT_COMMIT)
    stage2.add_argument("--label", default="cn-stage2")
    stage2.add_argument("--activate", action="store_true")
    stage2.add_argument("--cache-dir", default=".local/ingestion-cache")
    history = sub.add_parser(
        "ingest-garrison-history",
        help="extend a sealed release with pinned history and field-level phase mappings",
    )
    history.add_argument("--base-release", help="defaults to the active release")
    history.add_argument("--label", default="cn-stage5-garrison-history")
    history.add_argument("--activate", action="store_true")
    history.add_argument("--cache-dir", default=".local/garrison-history")
    expansion = sub.add_parser(
        "ingest-garrison-expansion",
        help="append fixed Wiki corroboration for golden equipment and bond fields",
    )
    expansion.add_argument("--base-release", help="defaults to a phase-reviewed history release")
    expansion.add_argument("--label", default="cn-stage6-garrison-fields")
    expansion.add_argument("--activate", action="store_true")
    expansion.add_argument("--cache-dir", default=".local/garrison-expansion")
    mechanics = sub.add_parser(
        "ingest-garrison-mechanics",
        help="append phase-specific descriptions and incomplete-mechanism review evidence",
    )
    mechanics.add_argument("--base-release", help="defaults to the active stage6 release")
    mechanics.add_argument("--label", default="cn-stage7-garrison-mechanics")
    mechanics.add_argument("--activate", action="store_true")
    mechanics.add_argument("--cache-dir", default=".local/garrison-mechanics")
    regulation = sub.add_parser(
        "ingest-garrison-regulation", help="append the reviewed CN Regulation Sequence I snapshot",
    )
    regulation.add_argument("--base-release", help="defaults to the active release")
    regulation.add_argument("--label", default="cn-garrison-regulation-sequence1")
    regulation.add_argument("--source-dir", required=True, help="directory of the pinned client JSON and official HTML")
    roguelike = sub.add_parser(
        "ingest-roguelike", help="append pinned Integrated Strategies source documents"
    )
    roguelike.add_argument("--commit", default=DEFAULT_COMMIT)
    roguelike.add_argument("--base-release", required=True)
    roguelike.add_argument("--label", default="cn-stage15-integrated-strategies")
    roguelike.add_argument("--cache-dir", default=".local/stage15-roguelike-source-cache")
    roguelike.add_argument("--offline", action="store_true")
    roguelike.add_argument("--activate", action="store_true")
    ask = sub.add_parser("ask", help="answer a natural-language question with pinned evidence")
    ask.add_argument("question")
    ask.add_argument("--release")
    ask.add_argument(
        "--module", choices=["auto", "operator", "recruitment", "garrison", "roguelike"], default="auto"
    )
    ask.add_argument("--planner", choices=["local", "openai"], default="local")
    recruit = sub.add_parser(
        "recruit", help="calculate tag combinations conditional on retained tags"
    )
    recruit.add_argument("tags", nargs="*")
    recruit.add_argument("--duration", type=int, default=540)
    recruit.add_argument("--release")
    evaluation = sub.add_parser("evaluate", help="run a pinned source-derived evaluation suite")
    evaluation.add_argument("--suite", default="evals/arknights_cn_v1")
    evaluation.add_argument("--split", choices=["dev", "holdout", "all"], default="all")
    evaluation.add_argument("--release")
    evaluation.add_argument("--previous-release")
    evaluation.add_argument("--output-dir")
    evaluation.add_argument("--provider", choices=["none", "openai", "deepseek"], default="none")
    evaluation.add_argument("--closed-book-predictions")
    evaluation.add_argument("--source-packet-predictions")
    evaluation.add_argument("--baseline-model", help="declared model name for external predictions")
    export = sub.add_parser(
        "export-eval-inputs", help="export fixed model inputs without gold or API calls"
    )
    export.add_argument("--suite", default="evals/arknights_cn_v1")
    export.add_argument("--split", choices=["dev", "holdout", "all"], default="holdout")
    export.add_argument("--release")
    export.add_argument("--previous-release")
    export.add_argument("--output-dir")
    publish = sub.add_parser(
        "publish", help="seal staging datasets and publish an immutable manifest"
    )
    publish.add_argument("--dataset", action="append", required=True, metavar="MODULE=ID")
    publish.add_argument("--label", required=True)
    activate = sub.add_parser("activate", help="atomically switch or roll back the active release")
    activate.add_argument("release_id")
    sub.add_parser("releases")
    query = sub.add_parser("query")
    query.add_argument("--release")
    query.add_argument("--module")
    query.add_argument("--kind")
    query.add_argument("--name")
    query.add_argument("--limit", type=int, default=20)
    evidence = sub.add_parser("evidence")
    evidence.add_argument("evidence_id")
    evidence.add_argument("--release")
    diff = sub.add_parser("diff")
    diff.add_argument("before")
    diff.add_argument("after")
    verify = sub.add_parser("verify-release")
    verify.add_argument("release_id", nargs="?")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.action == "migrate":
        migrate()
        output({"status": "migrated"})
        return
    if args.action == "serve":
        import uvicorn

        uvicorn.run(
            "arknights_agent.api:create_app", factory=True, host="127.0.0.1", port=args.port,
            access_log=False,
        )
        return
    settings = get_settings()
    if args.action == "backend-status":
        from arknights_agent.runtime.runner import runtime_status

        output(runtime_status(settings))
        return
    if args.action == "export-eval-inputs":
        from arknights_agent.evaluation.export import export_inputs

        try:
            output(
                export_inputs(
                    settings,
                    suite=args.suite,
                    split=args.split,
                    current_id=args.release,
                    previous_id=args.previous_release,
                    output_dir=args.output_dir,
                )
            )
        except (DomainError, ValueError, OSError) as exc:
            parser.exit(1, "error: " + redact(str(exc)) + "\n")
        return
    if args.action == "evaluate":
        from arknights_agent.evaluation.runner import run_evaluation

        try:
            result = run_evaluation(
                settings,
                suite=args.suite,
                split=args.split,
                current_id=args.release,
                previous_id=args.previous_release,
                output_dir=args.output_dir,
                provider=args.provider,
                baseline_model=args.baseline_model,
                external_paths={
                    arm: path
                    for arm, path in (
                        ("closed_book", args.closed_book_predictions),
                        ("source_packet", args.source_packet_predictions),
                    )
                    if path is not None
                },
            )
            output(result)
        except (DomainError, ValueError, OSError) as exc:
            parser.exit(1, "error: " + redact(str(exc)) + "\n")
        return
    engine = make_engine(settings.database_url)
    store = SnapshotStore(settings.snapshot_root)
    try:
        if args.action == "agent-chat":
            from types import SimpleNamespace

            from arknights_agent.agent import record_run
            from arknights_agent.runtime.runner import run_chat

            result = run_chat(
                engine, store, settings, args.question, release_id=args.release,
                module=args.module, backend=args.backend,
            )
            if result["status"] != "timeout":
                with session_factory(engine)() as audit, audit.begin():
                    record_run(audit, SimpleNamespace(id=result["release_id"]), args.question, result)
            output(result)
            return
        # Fetch before opening a DB transaction or acquiring publication locks.
        batch = (
            fetch_sample(args.commit, cache_dir=args.cache_dir)
            if args.action == "ingest-sample"
            else None
        )
        parser_version = PARSER_VERSION
        if args.action == "ingest-stage2":
            from arknights_agent.ingestion.stage2 import PARSER_VERSION as STAGE2_PARSER
            from arknights_agent.ingestion.stage2 import fetch_stage2

            batch = fetch_stage2(args.commit, cache_dir=args.cache_dir)
            parser_version = STAGE2_PARSER
        base_dataset_ids = None
        base_release_id = None
        if args.action == "ingest-roguelike":
            from arknights_agent.roguelike_sources import PARSER_VERSION as ROGUELIKE_PARSER
            from arknights_agent.roguelike_sources import collect_roguelike

            with session_factory(engine)() as base_session, base_session.begin():
                base_release = services.resolve_release(base_session, args.base_release)
                base_release_id = base_release.id
                base_dataset_ids = {
                    module: entry["dataset_id"]
                    for module, entry in base_release.manifest["datasets"].items()
                }
            batch = collect_roguelike(
                args.commit, cache_dir=args.cache_dir, offline=args.offline
            )
            parser_version = ROGUELIKE_PARSER
        if args.action == "ingest-garrison-history":
            from arknights_agent.ingestion.garrison_history import (
                PARSER_VERSION as HISTORY_PARSER,
            )
            from arknights_agent.ingestion.garrison_history import build_garrison_history_batch
            from arknights_agent.ingestion.releases import export_module_batch

            # Pin and verify the base before network work. Its other module IDs
            # remain unchanged even if the active release switches during fetch.
            with session_factory(engine)() as base_session, base_session.begin():
                base_release = services.resolve_release(base_session, args.base_release)
                base_release_id = base_release.id
                base_dataset_ids = {
                    module: entry["dataset_id"]
                    for module, entry in base_release.manifest["datasets"].items()
                }
                base_batch = export_module_batch(base_session, base_release, "garrison", store)
            batch = build_garrison_history_batch(base_batch, cache_dir=args.cache_dir)
            parser_version = HISTORY_PARSER
        if args.action == "ingest-garrison-expansion":
            from arknights_agent.ingestion.garrison_expansion import (
                PARSER_VERSION as EXPANSION_PARSER,
            )
            from arknights_agent.ingestion.garrison_expansion import build_garrison_expansion_batch
            from arknights_agent.ingestion.releases import export_module_batch

            with session_factory(engine)() as base_session, base_session.begin():
                base_release = services.resolve_release(base_session, args.base_release)
                base_release_id = base_release.id
                base_dataset_ids = {
                    module: entry["dataset_id"]
                    for module, entry in base_release.manifest["datasets"].items()
                }
                base_batch = export_module_batch(base_session, base_release, "garrison", store)
            batch = build_garrison_expansion_batch(base_batch, cache_dir=args.cache_dir)
            parser_version = EXPANSION_PARSER
        if args.action == "ingest-garrison-mechanics":
            from arknights_agent.ingestion.garrison_mechanics import (
                PARSER_VERSION as MECHANICS_PARSER,
            )
            from arknights_agent.ingestion.garrison_mechanics import build_garrison_mechanics_batch
            from arknights_agent.ingestion.releases import export_module_batch

            with session_factory(engine)() as base_session, base_session.begin():
                base_release = services.resolve_release(base_session, args.base_release)
                base_release_id = base_release.id
                base_dataset_ids = {
                    module: entry["dataset_id"]
                    for module, entry in base_release.manifest["datasets"].items()
                }
                base_batch = export_module_batch(base_session, base_release, "garrison", store)
            batch = build_garrison_mechanics_batch(base_batch, cache_dir=args.cache_dir)
            parser_version = MECHANICS_PARSER
        if args.action == "ingest-garrison-regulation":
            from arknights_agent.ingestion.garrison_regulation import (
                PARSER_VERSION as REGULATION_PARSER,
            )
            from arknights_agent.ingestion.garrison_regulation import (
                build_garrison_regulation_batch,
            )
            from arknights_agent.ingestion.releases import export_module_batch

            with session_factory(engine)() as base_session, base_session.begin():
                base_release = services.resolve_release(base_session, args.base_release)
                base_release_id = base_release.id
                base_dataset_ids = {
                    module: entry["dataset_id"]
                    for module, entry in base_release.manifest["datasets"].items()
                }
                base_batch = export_module_batch(base_session, base_release, "garrison", store)
            batch = build_garrison_regulation_batch(base_batch, args.source_dir)
            parser_version = REGULATION_PARSER
        with session_factory(engine)() as session, session.begin():
            if args.action in (
                "ingest-sample",
                "ingest-stage2",
                "ingest-garrison-history",
                "ingest-garrison-expansion",
                "ingest-garrison-mechanics",
                "ingest-garrison-regulation",
                "ingest-roguelike",
            ):
                datasets = services.ingest_batch(session, batch, store, parser_version)
                dataset_ids = dict(base_dataset_ids or {})
                dataset_ids.update({module: dataset.id for module, dataset in datasets.items()})
                release = services.publish_release(
                    session,
                    dataset_ids,
                    label=args.label or f"cn-sample-{batch.revision[:12]}",
                    store=store,
                )
                activated = getattr(args, "activate", False)
                if activated:
                    services.activate_release(session, release.id)
                result = {
                    "release_id": release.id,
                    "manifest_hash": release.manifest_hash,
                    "source_revision": batch.revision,
                    "snapshots": len(batch.snapshots),
                    "records": dict(Counter(r.module for r in batch.records)),
                    "dataset_ids": dataset_ids,
                    "activated": activated,
                    "coverage": batch.coverage,
                }
                if base_release_id:
                    result["base_release_id"] = base_release_id
            elif args.action == "ask":
                from arknights_agent.agent import dispatch_question, record_run

                release = services.resolve_release(session, args.release)
                result = dispatch_question(
                    session,
                    release,
                    args.question,
                    store,
                    settings,
                    module=args.module,
                    planner=args.planner,
                )
                record_run(session, release, args.question, result)
            elif args.action == "recruit":
                from arknights_agent.agent import record_run, render_recruitment_answer
                from arknights_agent.recruitment import calculate

                release = services.resolve_release(session, args.release)
                result = calculate(session, release, args.tags, args.duration, store=store)
                result["answer"] = render_recruitment_answer(result)
                record_run(session, release, "公开招募标签组合计算", result)
            elif args.action == "publish":
                dataset_ids = {}
                for entry in args.dataset:
                    module, separator, ident = entry.partition("=")
                    if not separator or not ident or module in dataset_ids:
                        raise services.InvalidData("datasets must be unique MODULE=ID pairs")
                    dataset_ids[module] = ident
                result = services.release_view(
                    services.publish_release(session, dataset_ids, args.label, store)
                )
            elif args.action == "activate":
                result = services.release_view(services.activate_release(session, args.release_id))
            elif args.action == "releases":
                result = [
                    services.release_view(services.resolve_release(session, row.id))
                    for row in session.scalars(select(Release).order_by(Release.created_at.desc()))
                ]
            elif args.action == "query":
                release = services.resolve_release(session, args.release)
                result = {
                    "release_id": release.id,
                    "items": services.list_records(
                        session,
                        release,
                        module=args.module,
                        kind=args.kind,
                        name=args.name,
                        limit=args.limit,
                    ),
                    "coverage": {k: v["coverage"] for k, v in release.manifest["datasets"].items()},
                }
            elif args.action == "evidence":
                release = services.resolve_release(session, args.release)
                result = {
                    "release_id": release.id,
                    "evidence": services.get_evidence(session, release, args.evidence_id),
                }
            elif args.action == "diff":
                result = services.diff_releases(session, args.before, args.after)
            elif args.action == "verify-release":
                release = services.resolve_release(session, args.release_id)
                for entry in release.manifest["datasets"].values():
                    services.seal_dataset(session, entry["dataset_id"], store)
                result = {
                    "release_id": release.id,
                    "status": "verified",
                    "manifest_hash": release.manifest_hash,
                }
            else:
                raise services.InvalidData("unsupported command")
        output(result)  # Only report success after the deferred DB checks commit.
    except (DomainError, ValueError) as exc:
        parser.exit(1, "error: " + redact(str(exc)) + "\n")
    except SQLAlchemyError:
        parser.exit(1, "error: database operation failed; transaction rolled back\n")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
