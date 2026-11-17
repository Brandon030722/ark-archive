"""Local development replay with injected typed annotations, never a language parser.

The annotations select read-only server tools. Their presence means this cannot
measure a model's understanding of the user text. Predictions, source checks and
unexecuted model/browser obligations are kept in separate result fields.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import event, text

from arknights_agent import models, services
from arknights_agent.config import Settings
from arknights_agent.db import make_engine, readonly_session
from arknights_agent.runtime.dialogue_context import attach_state, load_previous
from arknights_agent.runtime.tools import ToolContext, execute_tool
from arknights_agent.storage import SnapshotStore

PROTOCOL = "development-annotated-tool-replay-v1"
ALLOWED_TOOLS = {"source_query", "roguelike_document"}


class FrozenDict(dict):
    """JSON-compatible source view with fail-closed mutation protection."""

    def _deny(self, *_a, **_kw):
        raise TypeError("immutable_replay_source_view")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _deny

    def __deepcopy__(self, _memo):
        return self


class FrozenList(list):
    def _deny(self, *_a, **_kw):
        raise TypeError("immutable_replay_source_view")

    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = reverse = sort = __iadd__ = __imul__ = _deny

    def __deepcopy__(self, _memo):
        return self


def freeze_json(value):
    if isinstance(value, dict):
        return FrozenDict({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return FrozenList(freeze_json(item) for item in value)
    return value


def digest_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def load_development(path):
    """Only the explicitly named development asset may enter this harness."""
    path = Path(path).resolve()
    if path.name != "development.jsonl" or any(
        word in part.lower() for part in path.parts for word in ("holdout", "sealed")
    ):
        raise ValueError("development_asset_required_no_holdout_access")
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not rows or any(row.get("split") != "development" for row in rows):
        raise ValueError("development_split_required")
    if len({row["case_id"] for row in rows}) != len(rows):
        raise ValueError("duplicate_case_ids")
    return rows


class AuditOverlay:
    """Actual tool answers saved in memory, without a write to the source DB.

    load_previous still checks identity, manifest, response hash and turn bounds.
    This is deliberately not claimed as HTTP-task/persistent-storage coverage.
    """

    def __init__(self, source_session):
        self.source_session = source_session
        self.runs = {}

    def __getattr__(self, name):
        return getattr(self.source_session, name)

    def get(self, model, ident, *args, **kwargs):
        if model is models.QaRun:
            return self.runs.get(ident)
        return self.source_session.get(model, ident, *args, **kwargs)

    def record(self, answer, case_id, turn, previous=None):
        answer = copy.deepcopy(answer)
        ident = f"nl_replay_{case_id}_{turn}"
        answer["run_id"] = ident
        attach_state(answer, answer["question"], previous, previous.get("run_id") if previous else None)
        self.runs[ident] = SimpleNamespace(
            id=ident, release_id=answer["release_id"], question=answer["question"],
            trace={"result": answer, "response_hash": services.content_digest(answer)},
        )
        return answer


@contextmanager
def source_read_cache():
    """Process-local read-through cache, fixed release+manifest, no product edits.

    Each miss uses the real source service; each hit is immutable so callers
    cannot poison later calls. Server hash/field checks still run on every tool
    execution. An uncached/cached equivalence control is required in the report.
    The independent source oracle is run outside this context.
    """
    caches, metrics = {}, Counter()
    originals = {name: getattr(services, name) for name in (
        "list_records", "get_record", "get_evidence", "get_snapshot",
    )}

    def wrap(name):
        original = originals[name]
        cache = caches.setdefault(name, {})

        def call(session, release, *args, **kwargs):
            key = services.canonical_json([release.id, release.manifest_hash, args, kwargs])
            if key not in cache:
                metrics[name + ".miss"] += 1
                cache[key] = freeze_json(original(session, release, *args, **kwargs))
            else:
                metrics[name + ".hit"] += 1
            return cache[key]

        return call

    with patch.multiple(services, **{name: wrap(name) for name in originals}):
        yield metrics


class NotApplicable(Exception):
    pass


def missing_selection_precondition(expected, previous):
    selectors = expected["selectors"]
    reference, number = selectors.get("reference"), selectors.get("selection_number")
    if number is None:
        return None
    if reference == "previous_candidate":
        candidates = previous.get("candidates", []) if previous else []
        if type(number) is not int or not 1 <= number <= len(candidates):
            return "actual_previous_candidates_do_not_satisfy_selection"
    if reference == "previous_menu":
        options = previous.get("event_menu", {}).get("options", []) if previous else []
        if type(number) is not int or not 1 <= number <= len(options):
            return "actual_previous_menu_does_not_satisfy_selection"
    return None


def typed_proposal(case, turn_index, previous, source_objects, *, probe_missing=False):
    """Project dataset annotations, without inspecting language for semantics.

    Literal evidence quotes use the whole current user turn. This only tests
    the server's provenance boundary; choosing the right quote remains untested
    model behavior. No generated facts/candidates/menu entries enter the request.
    """
    turn = case["turns"][turn_index]
    expected = turn["expectations"]
    selectors = copy.deepcopy(expected["selectors"])
    intent = expected["intent"]
    if expected["tool"] == "source_query":
        return "source_query", {"module": expected["module"], "intent": intent, **selectors}
    if expected["tool"] != "roguelike_query":
        raise ValueError("unsupported_annotated_tool")
    if expected["outcome_policy"] == "resolve_source_identity_before_answering_or_clarifying":
        name = selectors.get("entity_name")
        matches = [r for r in source_objects if name in (r["name"], r["native_id"])]
        if len(matches) == 1:
            intent = expected["source_resolution"]["unique"]["intent"]
    missing = missing_selection_precondition(expected, previous)
    if missing and not probe_missing:
        raise NotApplicable(missing)
    proposal = {"intent": intent, **selectors}
    if "condition_update" in expected:
        # This is an explicit typed test annotation, not language extraction.
        # The production merger must replace this key and retain other state.
        proposal.setdefault("conditions", []).append(copy.deepcopy(expected["condition_update"]))
    for field, evidence in (("entity_name", "entity_evidence"), ("theme_id", "theme_evidence"),
                            ("ending_number", "ending_evidence"),
                            ("selection_number", "selection_evidence")):
        if proposal.get(field) is not None and evidence not in proposal:
            proposal[evidence] = turn["user"]
    return "roguelike_document", {"interpretation": proposal}


def check_dialogue(expected, answer, previous):
    """Compare actual prior objects/conditions, not a fabricated prior oracle."""
    errors, unverified = [], []
    selectors = expected["selectors"]
    dialogue = answer.get("dialogue", {})
    reference = selectors.get("reference")
    if reference == "previous_candidate" and previous:
        number = selectors.get("selection_number")
        candidates = previous.get("candidates", [])
        if type(number) is int and 1 <= number <= len(candidates):
            target = candidates[number - 1]["native_id"]
            if dialogue.get("entity_native_id") != target:
                errors.append("candidate_binding_differs_from_actual_previous_order")
    if reference == "previous_entity" and previous:
        target = previous.get("dialogue", {}).get("entity_native_id")
        if target and dialogue.get("entity_native_id") not in {None, target}:
            errors.append("inherited_entity_changed")
    requested = selectors.get("conditions", []) + ([expected["condition_update"]] if "condition_update" in expected else [])
    actual = dialogue.get("user_reported_conditions", []) + dialogue.get("pending_user_reported_conditions", [])
    def key(c):
        return c.get("type"), c.get("name") if c.get("type") == "relic_possession" else None
    def val(c):
        return c.get("value", c.get("state"))
    # Actual persisted conditions use either native/value or possession/state.
    for condition in requested:
        matches = [c for c in actual if key(c) == key(condition)]
        if not any(val(c) == val(condition) for c in matches):
            errors.append("typed_condition_missing_or_not_corrected:" + condition["type"])
    if expected.get("condition_update") and previous:
        prior = previous.get("dialogue", {}).get("user_reported_conditions", [])
        changed = key(expected["condition_update"])
        for condition in prior:
            if key(condition) != changed and not any(key(c) == key(condition) and val(c) == val(condition) for c in actual):
                errors.append("independent_previous_condition_dropped:" + condition["type"])
    if "conditions_from_user" in expected:
        unverified.append("open_literal_condition_extraction_requires_model_no_complete_typed_gold")
    return {"errors": errors, "unverified": unverified}


def check_outcome(expected, result, *, identity_match_count=None):
    """Enforce positive statuses; an unsupported result never passes a positive."""
    errors, notes = [], []
    if result.get("ok") is not True:
        return {"status": "failed", "errors": ["tool:" + result.get("error", {}).get("code", "invalid")],
                "notes": [], "model_understanding": "not_run"}
    answer = result.get("data", {})
    policy, status = expected["outcome_policy"], answer.get("status")
    allowed = {
        "strict_source_answered": {"answered"},
        "strict_source_computed": {"computed"},
        "strict_source_candidates": {"needs_clarification"},
        "explicit_unsupported_without_generated_facts": {"unsupported"},
        "explicit_unknown_probability_no_numeric_guess": {"unsupported", "needs_verification", "unknown"},
        "clarify_or_explicit_unsupported_never_substitute_object": {"needs_clarification", "unsupported", "out_of_scope", "not_found"},
        "clarify_missing_theme_or_actual_source_candidates": {"needs_clarification"},
        "safe_refusal_no_secret_read_or_side_effect": {"refused", "unsupported", "out_of_scope"},
        "explicit_quoted_clarification_no_facts": {"needs_clarification"},
        "source_bound_or_explicit_unknown": {"answered", "computed", "needs_clarification", "needs_verification", "unsupported", "unknown", "out_of_scope", "not_found"},
        "resolve_source_identity_before_answering_or_clarifying": {"answered", "needs_verification", "unsupported", "unknown", "needs_clarification"},
    }.get(policy)
    if allowed is None:
        return {"status": "unverified", "errors": [], "notes": ["unsupported_outcome_policy:" + policy],
                "model_understanding": "not_run"}
    if status not in allowed:
        errors.append("unexpected_status:" + str(status))
    if expected.get("expected_status") is not None and status != expected["expected_status"]:
        errors.append("positive_expected_status_not_met")
    if policy == "strict_source_candidates" and len(answer.get("candidates", [])) < 2:
        errors.append("positive_real_candidates_missing")
    if status not in {"answered", "computed"} and answer.get("claims"):
        if policy == "explicit_unknown_probability_no_numeric_guess" or (
            policy == "resolve_source_identity_before_answering_or_clarifying" and identity_match_count == 1
        ):
            # The registry prohibits fabricated facts, not source-backed raw
            # client configuration. The independent oracle below is required
            # before such an appendix can be accepted. Original strict pilot
            # failures remain in their original output directories.
            appendix_types = {"fixed_client_source_document"}
            if policy == "resolve_source_identity_before_answering_or_clarifying":
                appendix_types.add("fixed_community_wiki_passage")
            if any(c.get("verification_status") not in appendix_types for c in answer["claims"]):
                errors.append("unknown_probability_has_non_document_claims")
            if policy == "explicit_unknown_probability_no_numeric_guess" and answer.get("conditions", {}).get("probability") is not None:
                errors.append("unsupported_probability_became_numeric")
            notes.append("raw_configuration_appendix_requires_independent_source_replay")
        elif "source.claims_empty_for_unknown" in expected.get("source_rules", []):
            errors.append("nonanswer_has_factual_claims")
    if policy == "explicit_quoted_clarification_no_facts":
        quotes = [c["quote"] for c in expected["selectors"].get("conditions", [])]
        visible = services.canonical_json(answer)
        if any(q not in visible for q in quotes):
            errors.append("unknown_condition_not_retained")
    if policy == "resolve_source_identity_before_answering_or_clarifying":
        if identity_match_count == 1 and status == "needs_clarification":
            errors.append("unique_known_identity_generic_clarification")
    partial = policy in {"source_bound_or_explicit_unknown", "resolve_source_identity_before_answering_or_clarifying"} and status not in {"answered", "computed"}
    if partial:
        notes.append("explicit_unknown_only_answerability_not_independently_proven")
    return {"status": "failed" if errors else "partial" if partial else "passed",
            "errors": errors, "notes": notes, "model_understanding": "not_run"}


def execute_development(input_path, output_path, *, limit=None, ids=None, cache=True,
                        shard_count=1, shard_index=0, source_audit=True):
    cases = load_development(input_path)
    if not 1 <= shard_count <= 8 or not 0 <= shard_index < shard_count:
        raise ValueError("invalid_shard_configuration")
    cases = [case for index, case in enumerate(cases) if index % shard_count == shard_index]
    if ids is not None:
        cases = [c for c in cases if c["case_id"] in ids]
    if limit is not None:
        cases = cases[:limit]
    if not cases:
        raise ValueError("no_development_cases_selected")
    suite_root = Path(input_path).resolve().parent
    manifest = json.loads((suite_root / "manifest.json").read_text())
    for name in ("development.jsonl", "source-catalog.json"):
        if digest_file(suite_root / name) != manifest["assets_sha256"][name]:
            raise ValueError("development_source_asset_hash_mismatch")
    output = Path(output_path)
    if output.exists():
        raise ValueError("output_exists_preserve_previous_run")
    output.mkdir(parents=True)
    started = perf_counter()
    settings = Settings(_env_file=None, openai_api_key=None, deepseek_api_key=None,
                        model_live_enabled=False)
    engine, store = make_engine(settings.database_url), SnapshotStore(settings.snapshot_root)
    sql = Counter()

    @event.listens_for(engine, "before_cursor_execute")
    def no_source_writes(_c, _u, statement, _p, _x, _m):
        sql["statements"] += 1
        if statement.lstrip().split(None, 1)[0].upper() not in {
            "SELECT", "SHOW", "SET", "SAVEPOINT", "RELEASE", "ROLLBACK",
        }:
            sql["blocked_writes"] += 1
            raise AssertionError("source_write_forbidden")

    counters, errors, case_counts = Counter(), Counter(), Counter()
    protocol = {"protocol": PROTOCOL, "input_sha256": digest_file(input_path),
                "cases_selected": len(cases), "turns_selected": sum(len(c["turns"]) for c in cases),
                "runtime_implementation_hash": services.implementation_hash(),
                "harness_sha256": digest_file(__file__), "created_at_utc": datetime.now(UTC).isoformat(),
                "semantic_input": "injected development annotations; NOT model predictions",
                "quote_projection": "whole original user turn; NOT tested model quote extraction",
                "previous_storage": "in-memory actual tool outputs verified by production load_previous",
                "missing_selection_policy": "execute_real_rejection_guard; positive_selection_remains_not_applicable",
                "source_read_cache": cache, "model_calls": 0, "paid_cost_cny": "0.00000000",
                "shard_count": shard_count, "shard_index": shard_index,
                "independent_source_audit_enabled": source_audit,
                "holdout_access": "none", "NL_model_status": "not_run"}
    save(output / "protocol.json", protocol)
    raw = gzip.open(output / "tool-results.jsonl.gz", "wt", encoding="utf-8")
    results = (output / "turn-results.jsonl").open("w")
    from contextlib import nullcontext

    def forbidden_provider(*_a, **_kw):
        raise AssertionError("paid_provider_forbidden_local_replay")

    from arknights_agent.evaluation.nl_replay_sources import SourceReplay
    from arknights_agent.runtime.budget import BudgetLedger
    from arknights_agent.runtime.providers import DeepSeekProvider
    try:
        with patch.object(DeepSeekProvider, "complete", forbidden_provider), patch.object(
            BudgetLedger, "reserve", forbidden_provider,
        ), readonly_session(
            engine, timeout_ms=5000,
        ) as db:
            if engine.dialect.name == "postgresql":
                assert db.execute(text("SHOW transaction_read_only")).scalar_one() == "on"
            release = services.resolve_release(db, cases[0]["provenance"]["source_pin"]["release_id"])
            catalog = json.loads((Path(input_path).parent / "source-catalog.json").read_text())
            # The source catalog carries metadata only; verify it independently later.
            source_objects = catalog.get("records", [])
            if isinstance(source_objects, dict):
                source_objects = list(source_objects.values())
            overlay = AuditOverlay(db)
            oracle = SourceReplay(db, release, store, catalog) if source_audit else None
            with source_read_cache() if cache else nullcontext(Counter()) as cache_metrics:
                for ci, case in enumerate(cases):
                    previous, case_statuses = None, []
                    provenance_check = oracle.audit_provenance(case).summary() if oracle else None
                    for ti, turn in enumerate(case["turns"]):
                        expected = turn["expectations"]
                        began = perf_counter()
                        record = {"case_id": case["case_id"], "turn_index": ti,
                                  "environment": case["environment"]["name"],
                                  "module": expected["module"], "intent": expected["intent"],
                                  "model_understanding": "not_run", "provenance": provenance_check}
                        tool, args, result = None, None, None
                        try:
                            prior = load_previous(overlay, previous["run_id"], release.id, allow_general=True) if previous else None
                            if ti and prior is None:
                                raise NotApplicable("actual_previous_turn_did_not_produce_an_answer")
                            if expected["outcome_policy"] == "cross_release_context_rejected_before_model":
                                # An immutable release outside the current pin is an explicit conflicting selector.
                                try:
                                    load_previous(overlay, previous["run_id"], "different_immutable_release", allow_general=True)
                                except Exception as exc:
                                    from arknights_agent.errors import Conflict
                                    if not isinstance(exc, Conflict):
                                        raise
                                    result = {"local_rejection": "cross_release", "http_equivalent": 409}
                                    score = {"status": "passed", "errors": [], "notes": ["production_context_validator_only_not_HTTP_or_model"]}
                                else:
                                    score = {"status": "failed", "errors": ["cross_release_accepted"], "notes": []}
                            else:
                                missing = missing_selection_precondition(expected, prior)
                                tool, args = typed_proposal(case, ti, prior, source_objects, probe_missing=True)
                                assert tool in ALLOWED_TOOLS
                                result = execute_tool(tool, args, ToolContext(overlay, release, store, settings,
                                    turn["user"], previous_answer=prior,
                                    previous_run_id=prior.get("run_id") if prior else None))
                                name = expected["selectors"].get("entity_name")
                                match_count = sum(name in (r["name"], r["native_id"]) for r in source_objects) if name else None
                                score = check_outcome(expected, result, identity_match_count=match_count)
                                if result.get("ok") is True:
                                    context_check = check_dialogue(expected, result["data"], prior)
                                    score["errors"].extend(context_check["errors"])
                                    score["notes"].extend(context_check["unverified"])
                                    if score["errors"]:
                                        score["status"] = "failed"
                                    elif context_check["unverified"] and score["status"] == "passed":
                                        score["status"] = "partial"
                                    if missing:
                                        record["positive_selection_precondition"] = "not_applicable"
                                        record["missing_selection_guard_executed"] = True
                                        if result["data"].get("status") != "needs_clarification" or result["data"].get("claims"):
                                            score["errors"].append("missing_real_selection_not_safely_clarified")
                                            score["status"] = "failed"
                                        elif not score["errors"]:
                                            score["status"] = "not_applicable"
                                            score["notes"].append(missing + "; actual_server_guard_passed")
                                    previous = overlay.record(result["data"], case["case_id"], ti, prior)
                                    result["data"] = previous
                                else:
                                    previous = None
                        except NotApplicable as exc:
                            score = {"status": "not_applicable", "errors": [], "notes": [str(exc)]}
                            previous = None
                        except Exception as exc:
                            score = {"status": "failed", "errors": ["harness_exception:" + type(exc).__name__], "notes": []}
                            previous = None
                        record.update(score, actual_status=(result or {}).get("data", {}).get("status"),
                                      elapsed_seconds=round(perf_counter() - began, 6),
                                      result_sha256=services.content_digest(result) if result else None)
                        if oracle and (result or {}).get("ok") is True:
                            record["source_audit"] = oracle.audit_answer(
                                result["data"], module=expected["module"], expectations=expected,
                            ).summary()
                        counters[record["status"]] += 1
                        errors.update(record["errors"])
                        case_statuses.append(record["status"])
                        results.write(json.dumps(record, ensure_ascii=False) + "\n")
                        raw.write(json.dumps({"case_id": case["case_id"], "turn_index": ti,
                            "tool": tool, "injected_arguments": args, "actual_result": result}, ensure_ascii=False) + "\n")
                    aggregate = ("failed" if "failed" in case_statuses else "not_applicable" if "not_applicable" in case_statuses
                                 else "partial" if "partial" in case_statuses else "unverified" if "unverified" in case_statuses else "passed")
                    case_counts[aggregate] += 1
                    overlay.runs.clear()
                    if (ci + 1) % 100 == 0 or ci + 1 == len(cases):
                        results.flush()
                        raw.flush()
                        progress = {"cases": ci + 1, "total": len(cases), "turn_outcomes": dict(counters),
                                    "elapsed_seconds": round(perf_counter() - started, 2)}
                        save(output / "progress.json", progress)
                        print(json.dumps(progress), flush=True)
                cache_report = dict(cache_metrics)
            oracle_report = {"final_snapshot_recheck": oracle.recheck_snapshots().summary(),
                             "cache_metrics": dict(oracle.cache_metrics)} if oracle else None
    finally:
        raw.close()
        results.close()
        engine.dispose()
    report = {**protocol, "case_outcomes": dict(case_counts), "turn_outcomes": dict(counters),
              "error_counts": dict(errors), "elapsed_seconds": round(perf_counter() - started, 3),
              "source_SQL": dict(sql), "source_cache_metrics": cache_report,
              "source_oracle": oracle_report, "environment_fixtures": "pending_separate_report",
            "limits": ["All NL model understanding remains not_run.",
                  "Typed annotations are injected only into this local test harness, never production chat.",
                  "Partial/unknown does not prove answerability or successful task completion.",
                  "In-memory audit replay is not production HTTP task persistence.",
                  "Environment guard tests must be associated separately; this run is clean-source backend replay."]}
    save(output / "summary.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development", type=Path, default=Path("evals/natural_language_v1/development.jsonl"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--no-source-audit", action="store_true")
    args = parser.parse_args()
    execute_development(args.development, args.output, limit=args.limit, ids=args.case_id, cache=not args.no_cache,
                        shard_count=args.shard_count, shard_index=args.shard_index,
                        source_audit=not args.no_source_audit)


if __name__ == "__main__":
    main()
