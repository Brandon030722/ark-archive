"""Export model-facing inputs without running a model or exporting gold."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from arknights_agent import services
from arknights_agent.db import make_engine, session_factory
from arknights_agent.errors import InvalidData
from arknights_agent.evaluation.baselines import (
    PROMPT_VERSION,
    _instructions,
    build_public_input,
    response_schema,
)
from arknights_agent.evaluation.contracts import digest, load_suite, write_json, write_jsonl
from arknights_agent.evaluation.packets import SourceReader, build_source_packet
from arknights_agent.evaluation.runner import select_releases


def export_inputs(
    settings,
    *,
    suite="evals/arknights_cn_v1",
    split="holdout",
    current_id=None,
    previous_id=None,
    output_dir=None,
    workspace=None,
):
    root = Path(workspace or ".").resolve()
    suite_path = Path(suite)
    if not suite_path.is_absolute():
        suite_path = root / suite_path
    manifest, cases, sources = load_suite(suite_path)
    if split not in {"dev", "holdout", "all"}:
        raise InvalidData("input export split must be dev, holdout or all")
    cases = [case for case in cases if split == "all" or case["split"] == split]
    if not cases:
        raise InvalidData("input export selected no cases")
    started = datetime.now(UTC)
    runtime = services.implementation_hash()
    output = (
        Path(output_dir)
        if output_dir
        else root / ".local/evaluation-inputs" / started.strftime("%Y%m%dT%H%M%S.%fZ")
    )
    engine = make_engine(settings.database_url)
    try:
        with session_factory(engine)() as session:
            bound, releases = select_releases(session, cases, current_id, previous_id)
            public_cases = [{k: v for k, v in case.items() if k != "expected"} for case in bound]
            reader = SourceReader(sources, root)
            files, counts = {}, {}
            for arm in ("closed_book", "source_packet"):
                rows = []
                for case in public_cases:
                    selected = case
                    if arm == "source_packet":
                        selected = {**case, "source_packet": build_source_packet(case, reader)}
                    public = build_public_input(selected, arm)
                    rows.append(
                        {
                            "schema_version": 1,
                            "case_id": case["id"],
                            "arm": arm,
                            "instructions": _instructions(arm),
                            "public_input": public,
                            "input_hash": digest(public),
                            "prompt_version": PROMPT_VERSION,
                        }
                    )
                file = output / f"{arm}-inputs.jsonl"
                write_jsonl(file, rows)
                files[file.name] = hashlib.sha256(file.read_bytes()).hexdigest()
                counts[arm] = len(rows)
            schema_file = output / "response-wire-schema.json"
            write_json(schema_file, response_schema())
            files[schema_file.name] = hashlib.sha256(schema_file.read_bytes()).hexdigest()
            unchanged = services.implementation_hash() == runtime
            result = {
                "schema_version": 1,
                "status": "completed" if unchanged else "invalid_mixed_implementation",
                "suite_id": manifest["suite_id"],
                "cases_sha256": manifest["cases_sha256"],
                "sources_sha256": manifest["sources_sha256"],
                "split": split,
                "case_count": len(bound),
                "arm_counts": counts,
                "files": files,
                "releases": {ident: release.manifest_hash for ident, release in releases.items()},
                "runtime_implementation_hash": runtime,
                "model_called": False,
                "gold_exported": False,
                "started_at": started.isoformat(),
                "finished_at": datetime.now(UTC).isoformat(),
                "output_dir": str(output.resolve()),
                "notes": [
                    "response-wire-schema uses conditions_json/value_json; external imports require normalized prediction JSONL",
                    "exports are inference inputs, not reviewer answer keys or measured model predictions",
                ],
            }
            write_json(output / "manifest.json", result)
            if not unchanged:
                raise InvalidData("implementation changed during input export; export is invalid")
            return result
    finally:
        engine.dispose()
