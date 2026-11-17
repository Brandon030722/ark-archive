"""Reproducible evaluation runs over fixed releases, with non-fabricated baselines."""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from fastapi.testclient import TestClient
from sqlalchemy import select

from arknights_agent import models, services
from arknights_agent.api import create_app
from arknights_agent.errors import InvalidData
from arknights_agent.evaluation.contracts import (
    load_suite,
    validate_prediction,
    write_json,
    write_jsonl,
)


class AgentNormalizer:
    """Adapt declared claims, not gold, into the common prediction contract."""

    def __init__(self, session, release):
        self.release = release
        datasets = [entry["dataset_id"] for entry in release.manifest["datasets"].values()]
        rows = list(
            session.scalars(
                select(models.KnowledgeRecord).where(
                    models.KnowledgeRecord.dataset_id.in_(datasets)
                )
            )
        )
        self.records = {row.id: row for row in rows}
        self.by_key = {(row.module, row.kind, row.native_id): row for row in rows}
        self.operators = {row.native_id: row for row in rows if row.kind == "operator"}
        self.sources = {
            row.id: row
            for row in session.scalars(
                select(models.SourceSnapshot).where(
                    models.SourceSnapshot.id.in_(
                        {
                            sid
                            for entry in release.manifest["datasets"].values()
                            for sid in entry["source_snapshot_ids"]
                        }
                    )
                )
            )
        }

    def subject(self, record, selected, current_subject):
        if record.kind == "operator":
            return record.name
        parent = record.parent_native_id
        if record.kind == "module_phase":
            module = self.by_key.get(("operator", "module", parent))
            parent = module.parent_native_id if module else None
        if parent in self.operators:
            return self.operators[parent].name
        if record.kind == "skill":
            owners = [
                self.operators[ident].name
                for ident in selected
                if ident in self.operators
                and any(
                    ref.get("skillId") == record.native_id
                    for ref in self.operators[ident].payload.get("skills", [])
                )
            ]
            return (
                current_subject
                if current_subject in owners
                else owners[0]
                if len(owners) == 1
                else None
            )
        return current_subject

    def normalize(self, case, response, elapsed):
        plan = next(
            (
                row.get("plan", {})
                for row in response.get("tool_trace", [])
                if row.get("tool") == "plan_question"
            ),
            {},
        )
        selected = plan.get("operator_ids", [])
        conditions = {
            key: plan[key]
            for key in (
                "skill_index",
                "rank",
                "elite_phase",
                "level",
                "potential_rank",
                "module_query",
                "module_level",
                "compare",
            )
            if key in plan and plan[key] is not None
        }
        if selected:
            conditions["operators"] = [
                self.operators[ident].name for ident in selected if ident in self.operators
            ]
        else:
            # Conditions retain a literal requested subject even when the pinned
            # source cannot resolve an identity. This is user input, not a game
            # fact, native ID, or a new scoring rule. Never read another release.
            context = response.get("request_context", {})
            context = context if isinstance(context, dict) else {}
            names = context.get("operator_names")
            spans = context.get("subject_spans")
            question = response.get("question", "")
            if (
                context.get("origin") == "user_question_literal"
                and context.get("identity_resolution") == "not_matched_in_pinned_catalog"
                and context.get("identity_verified") is False
                and context.get("release_id") == self.release.id
                and question == case.get("question")
                and isinstance(names, list)
                and len(names) == 1
                and isinstance(names[0], str)
                and isinstance(spans, list)
                and len(spans) == 1
                and isinstance(spans[0], list)
                and len(spans[0]) == 2
                and all(type(value) is int for value in spans[0])
                and 0 <= spans[0][0] < spans[0][1] <= len(question)
                and question[spans[0][0] : spans[0][1]] == names[0]
                and response.get("status") in {"unsupported", "needs_clarification"}
                and not response.get("claims")
                and not response.get("citations")
            ):
                conditions["operators"] = names
        module = case["module"]
        if module == "recruitment" and "input" in response:
            conditions.update(
                tags=response["input"]["normalized_tags"],
                duration_minutes=response["input"]["duration_minutes"],
            )
        if module == "garrison":
            scope = response.get("scope", {})
            if scope.get("activity_id"):
                conditions["activity_id"] = scope["activity_id"]
            for row in response.get("tool_trace", []):
                if row.get("phase_id"):
                    conditions["phase_id"] = row["phase_id"]
        citations, index = [], {}
        for position, original in enumerate(response.get("citations", [])):
            citation = dict(original)
            source = self.sources.get(citation.get("snapshot_id"))
            if source:
                citation["source_key"] = source.source_key
            citations.append(citation)
            if original.get("id"):
                index[original["id"]] = position
        groups = defaultdict(list)
        current_subject = None
        for claim in response.get("claims", []):
            record = self.records.get(claim.get("record_id"))
            if module == "garrison":
                label = claim["label"]
                subject = label.split(" ", 1)[0] if label.startswith("V2_") else "act2autochess"
            elif record:
                if record.kind == "operator":
                    current_subject = record.name
                subject = self.subject(record, selected, current_subject)
                label = claim["label"]
            else:
                continue
            if subject is None:
                continue
            groups[f"{subject}::{label}"].append(
                (
                    claim["value"],
                    [index[key] for key in claim.get("citation_ids", []) if key in index],
                )
            )
        facts = [
            {
                "key": key,
                "value": rows[0][0] if len(rows) == 1 else [value for value, _ in rows],
                "citation_indices": sorted({i for _, indices in rows for i in indices}),
            }
            for key, rows in groups.items()
        ]
        answer_version = response.get("version", {})
        answer_revision = answer_version.get("source_revision") or answer_version.get(
            "source_revisions", {}
        ).get(module)
        envelope_revision = answer_revision
        if module == "garrison" and answer_version.get("base_metadata_source_revision"):
            entry = self.release.manifest["datasets"][module]
            envelope_revision = answer_version["base_metadata_source_revision"]
            member_revisions = {self.sources[sid].revision for sid in entry["source_snapshot_ids"]}
            if (
                envelope_revision != entry["source_revision"]
                or answer_revision not in member_revisions
                or not any(c.get("source_revision") == answer_revision for c in citations)
            ):
                raise InvalidData("garrison answer provenance cannot be normalized to this release")
            phase = answer_version.get("phase_id")
            trace_phases = {
                row["phase_id"] for row in response.get("tool_trace", []) if row.get("phase_id")
            }
            if not phase or trace_phases != {phase}:
                raise InvalidData("garrison answer phase differs from its provenance trace")
        result = {
            "schema_version": 1,
            "case_id": case["id"],
            "release_id": response.get("release_id", case["release_id"]),
            # The eval envelope names the module's pinned metadata revision;
            # individual historical assertions retain their actual revisions in
            # citations, raw responses and the adapter trace below.
            "source_revision": envelope_revision,
            "status": response["status"],
            "conditions": conditions,
            "facts": facts,
            "combinations": [
                {
                    "tags": combo["selected_tags"],
                    "candidate_names": [row["name"] for row in combo["candidates"]],
                    "minimum_rarity": combo["minimum_rarity_if_tags_retained"],
                }
                for combo in response.get("combinations", [])
            ],
            "citations": citations,
            "answer": response.get("answer", ""),
            "trace": {
                "provider": "agent",
                "simulation": False,
                "run_id": response.get("run_id"),
                "manifest_hash": response.get("manifest_hash"),
                "answer_source_revision": answer_revision,
            },
            "latency_ms": float(elapsed),
        }
        return validate_prediction(result)


def error_prediction(case, status, detail, elapsed=0.0):
    return validate_prediction(
        {
            "case_id": case["id"],
            "release_id": case["release_id"],
            "source_revision": None,
            "status": status,
            "answer": detail,
            "trace": {"provider": "agent", "simulation": False},
            "latency_ms": float(elapsed),
        }
    )


def run_agent(cases, settings, releases):
    predictions, responses = [], []
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        with client.app.state.session_factory() as session:
            normalizers = {
                ident: AgentNormalizer(session, release) for ident, release in releases.items()
            }
            for index, case in enumerate(cases, start=1):
                body = {key: value for key, value in case["request"].items() if value is not None}
                body.update(
                    question=case["question"], release_id=case["release_id"], planner="local"
                )
                start = perf_counter()
                response = client.post("/v1/qa", json=body)
                elapsed = (perf_counter() - start) * 1000
                if response.status_code == 200:
                    raw = response.json()
                    prediction = normalizers[case["release_id"]].normalize(case, raw, elapsed)
                    audit = client.get(
                        f"/v1/qa/runs/{raw['run_id']}", params={"release_id": case["release_id"]}
                    )
                    if audit.status_code != 200 or audit.json().get("result") != raw:
                        prediction = error_prediction(
                            case, "error", "evaluation audit readback differs", elapsed
                        )
                elif response.status_code == 422:
                    raw = response.json()
                    prediction = error_prediction(
                        case, "invalid_input", "request validation rejected", elapsed
                    )
                    # The HTTP failure still carries a request-pinned evaluation envelope.
                    prediction["source_revision"] = case["source_revision"]
                else:
                    raw = {"http_status": response.status_code}
                    prediction = error_prediction(
                        case, "error", f"HTTP {response.status_code}", elapsed
                    )
                predictions.append(prediction)
                responses.append(
                    {"case_id": case["id"], "http_status": response.status_code, "response": raw}
                )
                if index % 10 == 0 or index == len(cases):
                    print(f"Evaluated {index}/{len(cases)} Agent questions...", flush=True)
    return predictions, responses


def select_releases(session, cases, current_id=None, previous_id=None):
    current = services.resolve_release(session, current_id)
    mapping, releases = {"current": current}, {current.id: current}
    if any(case["release_ref"] == "previous" for case in cases):
        revision = next(
            case["source_revision"] for case in cases if case["release_ref"] == "previous"
        )
        if previous_id:
            previous = services.resolve_release(session, previous_id)
        else:
            previous = next(
                (
                    row
                    for row in session.scalars(
                        select(models.Release).order_by(models.Release.created_at.desc())
                    )
                    if row.manifest["datasets"]["operator"]["source_revision"] == revision
                    and row.manifest["datasets"]["operator"]["coverage"].get("status")
                    == "source_complete"
                ),
                None,
            )
            if previous is None:
                raise InvalidData(
                    "evaluation needs a full previous source revision; publish ingest-stage2 --commit PREVIOUS_SHA first"
                )
            previous = services.resolve_release(session, previous.id)
        mapping["previous"] = previous
        releases[previous.id] = previous
    bound = []
    for case in cases:
        release = mapping[case["release_ref"]]
        if (
            release.manifest["datasets"][case["module"]]["source_revision"]
            != case["source_revision"]
        ):
            raise InvalidData("evaluation case source revision differs from its bound release")
        bound.append({**case, "release_id": release.id})
    return bound, releases


def prepare_baseline_inputs(bound, sources, root, *, with_sources=True):
    """Separate hidden expectations before selecting any inference sources."""
    from arknights_agent.evaluation.packets import SourceReader, build_source_packet

    public = [deepcopy({key: value for key, value in case.items() if key != "expected"}) for case in bound]
    packets = deepcopy(public)
    if with_sources:
        reader = SourceReader(sources, Path(root))
        packets = []
        for case in deepcopy(public):
            try:
                packet = build_source_packet(case, reader)
            except InvalidData:
                # An invalid packet is retained as an unexecuted case, never
                # silently replaced by an empty, apparently source-backed arm.
                packet = None
            packets.append({**case, "source_packet": packet})
    return {"closed_book": public, "source_packet": packets}


def run_evaluation(
    settings,
    *,
    suite="evals/arknights_cn_v1",
    split="all",
    current_id=None,
    previous_id=None,
    output_dir=None,
    provider="none",
    external_paths=None,
    baseline_model=None,
    workspace=None,
    baseline_collector=None,
):
    from arknights_agent.db import make_engine, session_factory
    from arknights_agent.evaluation.baselines import import_predictions, run_baseline
    from arknights_agent.evaluation.comparison import compare_arm
    from arknights_agent.evaluation.evidence import EvidenceContext
    from arknights_agent.evaluation.scoring import aggregate_scores, score_case
    from arknights_agent.storage import SnapshotStore

    root = Path(workspace or ".").resolve()
    suite_path = Path(suite)
    if not suite_path.is_absolute():
        suite_path = root / suite_path
    manifest, cases, sources = load_suite(suite_path)
    if split not in ("all", "dev", "holdout"):
        raise InvalidData("evaluation split must be all, dev or holdout")
    cases = [case for case in cases if split == "all" or case["split"] == split]
    if not cases:
        raise InvalidData("selected evaluation split has no cases")
    started, runtime = datetime.now(UTC), services.implementation_hash()
    output = (
        Path(output_dir)
        if output_dir
        else root / ".local/evaluations" / started.strftime("%Y%m%dT%H%M%S.%fZ")
    )
    engine = make_engine(settings.database_url)
    store = SnapshotStore(settings.snapshot_root)
    try:
        with session_factory(engine)() as session:
            bound, releases = select_releases(session, cases, current_id, previous_id)
            agent_predictions, responses = run_agent(bound, settings, releases)
            write_jsonl(output / "agent-responses.jsonl", responses)
            arms = {"agent": agent_predictions}
            public_inputs = prepare_baseline_inputs(
                bound, sources, root,
                with_sources=baseline_collector is not None or (
                    provider == "deepseek" and settings.model_live_enabled
                    and bool(settings.deepseek_api_key) and bool(settings.deepseek_model)
                ),
            )
            if baseline_collector is not None:
                if not callable(baseline_collector) or external_paths:
                    raise InvalidData("internal collector must be callable and cannot mix imports")
                collected = baseline_collector(public_inputs, settings)
                if not isinstance(collected, dict) or set(collected) != {"closed_book", "source_packet"}:
                    raise InvalidData("internal collector must return both baseline arms")
                arms.update(collected)
            for arm in ("closed_book", "source_packet") if baseline_collector is None else ():
                if external_paths and arm in external_paths:
                    arms[arm] = import_predictions(
                        external_paths[arm],
                        bound,
                        arm=arm,
                        model=baseline_model or settings.llm_model or "external_declared",
                    )
                else:
                    arms[arm] = run_baseline(
                        public_inputs[arm], settings, provider=provider, arm=arm,
                    )
            contexts = {
                ident: EvidenceContext(session, release, store)
                for ident, release in releases.items()
            }
            all_scores, aggregates = {}, {}
            for arm, predictions in arms.items():
                indexed = {prediction["case_id"]: prediction for prediction in predictions}
                if len(indexed) != len(bound) or set(indexed) != {case["id"] for case in bound}:
                    raise InvalidData("evaluation arm omitted or duplicated cases")
                rows = [
                    score_case(
                        case,
                        validate_prediction(indexed[case["id"]]),
                        evidence_context=contexts[case["release_id"]],
                    )
                    for case in bound
                ]
                write_jsonl(output / f"{arm}-predictions.jsonl", predictions)
                write_jsonl(output / f"{arm}-scores.jsonl", rows)
                all_scores[arm], aggregates[arm] = rows, aggregate_scores(rows)
            unchanged = services.implementation_hash() == runtime
            report = {
                "schema_version": 1,
                "status": "completed" if unchanged else "invalid_mixed_implementation",
                "started_at": started.isoformat(),
                "finished_at": datetime.now(UTC).isoformat(),
                "runtime_implementation_hash": runtime,
                "implementation_unchanged": unchanged,
                "suite": manifest,
                "suite_directory": str(suite_path),
                "split": split,
                "case_count": len(bound),
                "case_categories": dict(Counter(case["category"] for case in bound)),
                "releases": {
                    ident: {
                        "manifest_hash": release.manifest_hash,
                        "source_revisions": {
                            module: entry["source_revision"]
                            for module, entry in release.manifest["datasets"].items()
                        },
                    }
                    for ident, release in releases.items()
                },
                "arms": aggregates,
                "by_split": {
                    arm: {
                        group: aggregate_scores(
                            [
                                row
                                for case, row in zip(bound, rows, strict=True)
                                if case["split"] == group
                            ]
                        )
                        for group in sorted({case["split"] for case in bound})
                    }
                    for arm, rows in all_scores.items()
                },
                "by_module": {
                    arm: {
                        module: aggregate_scores(
                            [
                                row
                                for case, row in zip(bound, rows, strict=True)
                                if case["module"] == module
                            ]
                        )
                        for module in sorted({case["module"] for case in bound})
                    }
                    for arm, rows in all_scores.items()
                },
                "comparison": {
                    "conclusion": "not_established",
                    "reason": "source-derived gold requires independent human review; all comparable model predictions must be measured and complete",
                    "live_baseline_predictions": sum(
                        bool(prediction.get("trace", {}).get("measured_model_run"))
                        for arm, predictions in arms.items()
                        if arm != "agent"
                        for prediction in predictions
                    ),
                    "gold_review_status_counts": dict(
                        Counter(case["review_status"] for case in bound)
                    ),
                    "paired": {
                        arm: compare_arm(bound, all_scores["agent"], all_scores[arm], predictions)
                        for arm, predictions in arms.items()
                        if arm != "agent"
                    },
                },
                "limitations": [
                    "source-derived, Agent-authored gold is independent of production answers, not independently human reviewed",
                    "typed required-field agreement does not prove every sentence or extra unscored fact is correct",
                    "closed_book and source_packet are distinct comparison arms; source_packet is bounded retrieval, not unrestricted PRTS browsing",
                ],
            }
            write_json(output / "report.json", report)
            write_jsonl(
                output / "bound-cases.jsonl",
                [
                    {key: value for key, value in case.items() if key != "expected"}
                    for case in bound
                ],
            )
            failures = [row for row in all_scores["agent"] if not row.get("joint_pass")]
            write_json(output / "agent-failures.json", failures)
            if not unchanged:
                raise InvalidData(
                    "implementation changed during evaluation; run is retained but invalid"
                )
            return {
                "output_dir": str(output.resolve()),
                "report_path": str((output / "report.json").resolve()),
                "case_count": len(bound),
                "status": report["status"],
                "agent": aggregates["agent"],
                "comparison": report["comparison"],
            }
    finally:
        engine.dispose()
