"""Dimension-separated scoring of typed predictions against source-derived gold.

This module does not import the question planner, answer generator, or calculator.
It never guesses factual correctness from a response status, prose fluency, or a
valid source hash. Pending/manual and unexecuted runs retain their denominators
in coverage; they are not counted as evaluated answers.
"""

from __future__ import annotations

import math
from collections import Counter

from arknights_agent.evaluation.evidence import json_equal

SUCCESS_STATUSES = {"answered", "computed"}
UNEVALUATED_STATUSES = {"pending_manual", "not_run", "error"}
REFUSAL_STATUSES = {"unsupported", "needs_verification", "out_of_scope"}


def _expected_response_class(statuses):
    statuses = set(statuses)
    if statuses & SUCCESS_STATUSES:
        return "answerable"
    if statuses and statuses <= REFUSAL_STATUSES:
        return "refusal"
    if statuses == {"needs_clarification"}:
        return "clarification"
    if statuses == {"invalid_input"}:
        return "invalid_input"
    return "mixed_or_unknown_boundary"


def _dimension(checks=(), *, applicable=True, issues=()):
    checks = list(checks)
    return {
        "applicable": applicable,
        "passed": all(checks) and not issues if applicable else None,
        "pass": sum(bool(item) for item in checks),
        "total": len(checks),
        "issues": list(issues),
    }


def _facts(expected, prediction):
    supplied, duplicates, invalid = {}, set(), []
    facts = prediction.get("facts", [])
    if not isinstance(facts, list):
        facts, invalid = [], [{"code": "facts_not_a_list"}]
    for fact in facts:
        if (
            not isinstance(fact, dict)
            or not isinstance(fact.get("key"), str)
            or "value" not in fact
        ):
            invalid.append({"code": "malformed_fact"})
            continue
        key = fact["key"]
        if key in supplied:
            duplicates.add(key)
        supplied[key] = fact
    issues, checks = list(invalid), []
    keys = {fact["key"] for fact in expected}
    for fact in expected:
        key = fact["key"]
        actual = supplied.get(key)
        valid = (
            actual is not None
            and key not in duplicates
            and json_equal(actual["value"], fact["value"])
        )
        checks.append(valid)
        if not valid:
            issues.append(
                {
                    "code": "fact_mismatch" if actual is not None else "missing_fact",
                    "key": key,
                    "expected": fact["value"],
                    "actual": actual.get("value") if actual is not None else None,
                    "duplicate": key in duplicates,
                }
            )
    for key in duplicates - keys:
        issues.append({"code": "duplicate_extra_fact", "key": key})
    return (
        _dimension(checks, applicable=bool(expected) or bool(issues), issues=issues),
        supplied,
        len([fact for fact in facts if isinstance(fact, dict) and fact.get("key") not in keys]),
    )


def _conditions(expected, prediction):
    actual = prediction.get("conditions", {})
    if not isinstance(actual, dict):
        return _dimension([False] * len(expected), issues=[{"code": "conditions_not_an_object"}])
    checks, issues = [], []
    for key, value in expected.items():
        valid = key in actual and json_equal(actual[key], value)
        checks.append(valid)
        if not valid:
            issues.append(
                {
                    "code": "condition_mismatch",
                    "key": key,
                    "expected": value,
                    "actual": actual.get(key),
                }
            )
    return _dimension(checks, applicable=bool(expected), issues=issues)


def _combination_key(tags):
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        return None
    if len(set(tags)) != len(tags):
        return None
    return tuple(sorted(tags))


def _combinations(expected, prediction, applicable):
    actual = prediction.get("combinations", [])
    if not isinstance(actual, list):
        actual = []
        issues = [{"code": "combinations_not_a_list"}]
    else:
        issues = []
    expected_map = {_combination_key(combo["tags"]): combo for combo in expected}
    actual_map, duplicates = {}, set()
    for combo in actual:
        key = _combination_key(combo.get("tags")) if isinstance(combo, dict) else None
        if key is None:
            issues.append({"code": "malformed_combination"})
            continue
        if key in actual_map:
            duplicates.add(key)
        actual_map[key] = combo
    checks, true_positive, false_positive, false_negative = [], 0, 0, 0
    for key, gold in expected_map.items():
        predicted = actual_map.get(key)
        wanted = gold["candidate_names"]
        wanted_set = set(wanted)
        names = predicted.get("candidate_names", []) if predicted else []
        names_valid = isinstance(names, list) and all(isinstance(name, str) for name in names)
        supplied = set(names) if names_valid else set()
        names_valid = names_valid and len(names) == len(supplied)
        min_valid = (
            predicted is not None
            and "minimum_rarity" in predicted
            and json_equal(predicted.get("minimum_rarity"), gold["minimum_rarity"])
        )
        valid = (
            predicted is not None
            and key not in duplicates
            and names_valid
            and supplied == wanted_set
            and min_valid
        )
        checks.append(valid)
        true_positive += len(supplied & wanted_set)
        false_positive += len(supplied - wanted_set)
        false_negative += len(wanted_set - supplied)
        if not valid:
            issues.append(
                {
                    "code": "combination_mismatch"
                    if predicted is not None
                    else "missing_combination",
                    "tags": list(key),
                    "missing_candidates": sorted(wanted_set - supplied),
                    "extra_candidates": sorted(supplied - wanted_set),
                    "candidate_list_valid": names_valid,
                    "minimum_rarity_expected": gold["minimum_rarity"],
                    "minimum_rarity_actual": predicted.get("minimum_rarity") if predicted else None,
                    "duplicate": key in duplicates,
                }
            )
    for key in actual_map.keys() - expected_map.keys():
        combo = actual_map[key]
        issues.append({"code": "extra_combination", "tags": list(key)})
        names = combo.get("candidate_names", [])
        if isinstance(names, list) and all(isinstance(name, str) for name in names):
            false_positive += len(set(names))
    result = _dimension(checks, applicable=applicable, issues=issues)
    result["candidate_set_counts"] = {
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
    }
    return result


def _anchor_match(anchor, verified):
    return all(
        anchor.get(key) == verified.get(key)
        for key in ("source_key", "source_revision", "snapshot_hash", "pointer")
    ) and anchor.get("value_hash") == verified.get("field_hash")


def _citation_at(prediction, audit, index):
    if not isinstance(index, int) or isinstance(index, bool):
        return None
    citations = prediction.get("citations", [])
    if not 0 <= index < len(citations):
        return None
    citation = citations[index]
    if not isinstance(citation, dict):
        return None
    ident = citation.get("id", f"citation:{index}")
    return audit.verified.get(ident) if isinstance(ident, str) else None


def _evidence(case, prediction, supplied_facts, context):
    expected = case["expected"]
    claims = prediction.get("facts", [])
    needed = bool(
        expected.get("facts") or expected.get("combinations") or expected.get("required_anchors")
    )
    needed = needed or prediction.get("status") in SUCCESS_STATUSES or bool(claims)
    if not needed:
        return _dimension(applicable=False)
    if context is None:
        return _dimension(
            [False],
            issues=[
                {"code": "evidence_not_verified", "detail": "raw source context is unavailable"}
            ],
        )
    audit = context.audit(prediction.get("citations", []), case["module"])
    checks, issues = [audit.passed], list(audit.errors)
    citations = list(audit.verified.values())
    for anchor in expected.get("required_anchors", []):
        valid = any(_anchor_match(anchor, citation) for citation in citations)
        checks.append(valid)
        if not valid:
            issues.append(
                {
                    "code": "required_anchor_not_cited",
                    "source_key": anchor["source_key"],
                    "pointer": anchor["pointer"],
                }
            )
    for fact in expected.get("facts", []):
        actual = supplied_facts.get(fact["key"], {})
        indices = actual.get("citation_indices", [])
        if not isinstance(indices, list):
            indices = []
        linked = [_citation_at(prediction, audit, index) for index in indices]
        linked = [citation for citation in linked if citation is not None]
        anchors = fact.get("anchors", [])
        valid = bool(linked) and all(
            any(_anchor_match(anchor, citation) for citation in linked) for anchor in anchors
        )
        checks.append(valid)
        if not valid:
            issues.append({"code": "fact_evidence_missing_or_wrong_field", "key": fact["key"]})
    result = _dimension(checks, issues=issues)
    result["audit"] = audit.summary()
    return result


def _version(case, prediction, context):
    checks = [prediction.get("source_revision") == case["source_revision"]]
    issues = []
    if not checks[0]:
        issues.append(
            {
                "code": "source_revision_mismatch",
                "expected": case["source_revision"],
                "actual": prediction.get("source_revision"),
            }
        )
    expected_release = context.release.id if context is not None else case.get("release_id")
    if expected_release is None:
        checks.append(False)
        issues.append({"code": "release_not_verified"})
    else:
        checks.append(prediction.get("release_id") == expected_release)
        if not checks[-1]:
            issues.append(
                {
                    "code": "release_mismatch",
                    "expected": expected_release,
                    "actual": prediction.get("release_id"),
                }
            )
    return _dimension(checks, issues=issues)


def _answer_text(expected, prediction):
    answer = prediction.get("answer", "")
    answer = answer if isinstance(answer, str) else ""
    checks, issues = [], []
    for text in expected.get("answer_contains", []):
        valid = text in answer
        checks.append(valid)
        if not valid:
            issues.append({"code": "required_answer_text_missing", "text": text})
    for text in expected.get("answer_not_contains", []):
        valid = text not in answer
        checks.append(valid)
        if not valid:
            issues.append({"code": "forbidden_answer_text_present", "text": text})
    return _dimension(checks, applicable=bool(checks), issues=issues)


def score_case(case: dict, prediction: dict, *, evidence_context=None):
    """Score required facts, explicit conditions, exact sets and source evidence.

    `expected` is never given to a baseline provider. A textual-only baseline must
    be normalized by an explicit reviewer into this typed format or remain pending.
    Extra facts are counted as unscored rather than silently awarded correctness.
    """
    expected = case["expected"]
    answerable = bool(SUCCESS_STATUSES & set(expected["statuses"]))
    status = prediction.get("status", "error")
    result = {
        "case_id": case["id"],
        "evaluation_status": "scored",
        "answerable": answerable,
        "expected_statuses": list(expected["statuses"]),
        "expected_response_class": _expected_response_class(expected["statuses"]),
        "answered": status in SUCCESS_STATUSES,
        "has_factual_output": bool(prediction.get("facts") or prediction.get("combinations")),
        "prediction_status": status,
        "dimensions": {},
        "joint_pass": None,
        "task_pass_without_evidence": None,
        "required_content_applicable": bool(expected.get("facts") or expected.get("combinations")),
        "required_content_agreement": None,
        "unscored_extra_fact_count": 0,
    }
    if status in UNEVALUATED_STATUSES:
        result["evaluation_status"] = status
        return result
    fact_score, supplied, extras = _facts(expected.get("facts", []), prediction)
    result["unscored_extra_fact_count"] = extras
    refusal_checks = [status in expected["statuses"]]
    refusal_issues = []
    if not refusal_checks[0]:
        refusal_issues.append(
            {"code": "status_mismatch", "expected": expected["statuses"], "actual": status}
        )
    identity_matches = (
        prediction.get("case_id") == case["id"]
        and prediction.get("schema_version") == 1
        and not isinstance(prediction.get("schema_version"), bool)
    )
    refusal_checks.append(identity_matches)
    if not identity_matches:
        refusal_issues.append({"code": "prediction_case_or_schema_mismatch"})
    if not answerable:
        no_facts = not prediction.get("facts") and not prediction.get("combinations")
        refusal_checks.append(no_facts)
        if not no_facts:
            refusal_issues.append({"code": "refusal_contains_factual_output"})
    dimensions = {
        "facts": fact_score,
        "conditions": _conditions(expected.get("conditions", {}), prediction),
        "version": _version(case, prediction, evidence_context),
        "evidence": _evidence(case, prediction, supplied, evidence_context),
        "combinations": _combinations(
            expected.get("combinations", []),
            prediction,
            (case["module"] == "recruitment" and answerable)
            or bool(prediction.get("combinations")),
        ),
        "refusal": _dimension(refusal_checks, issues=refusal_issues),
        "answer_text": _answer_text(expected, prediction),
    }
    result["dimensions"] = dimensions
    applicable = [dimension for dimension in dimensions.values() if dimension["applicable"]]
    result["joint_pass"] = all(dimension["passed"] for dimension in applicable)
    result["task_pass_without_evidence"] = all(
        dimension["passed"]
        for key, dimension in dimensions.items()
        if key != "evidence" and dimension["applicable"]
    )
    if result["required_content_applicable"]:
        result["required_content_agreement"] = result["answered"] and all(
            dimension["passed"]
            for key, dimension in dimensions.items()
            if key in {"facts", "combinations"} and dimension["applicable"]
        )
    return result


def _ratio(numerator, denominator):
    return {
        "pass": numerator,
        "total": denominator,
        "rate": numerator / denominator if denominator else None,
    }


def _wilson(successes, total):
    if not total:
        return None
    z = 1.959963984540054
    p, z2 = successes / total, z * z
    denominator = 1 + z2 / total
    center = (p + z2 / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total)) / denominator
    return [max(0.0, center - half), min(1.0, center + half)]


def _all_case_accuracy(correct, rows):
    """A delivery denominator, with no fictional zero accuracy for an unrun arm.

    In a partial measured run unscored cases remain in the total; the rate is a
    conservative delivered-correct fraction, not an estimate of their answers.
    An all-not-run or all-pending arm has no measured correctness rate. A real
    execution error is an attempted but undelivered answer and earns no credit.
    """
    scored = sum(row["evaluation_status"] == "scored" for row in rows)
    errors = sum(row["evaluation_status"] == "error" for row in rows)
    measured = bool(scored or errors)
    result = _ratio(correct, len(rows))
    if not measured:
        result["rate"] = None
    result["measurement_status"] = (
        "complete" if rows and scored == len(rows) else "partial" if measured else "not_measured"
    )
    result["scored_count"] = scored
    result["unscored_count"] = len(rows) - scored
    return result


def _response_metrics(rows, scored, answerable, evaluated_answerable):
    all_answered = [row for row in scored if row["answered"]]
    content_cases = [row for row in answerable if row.get("required_content_applicable")]
    false_refusals = sum(
        row["prediction_status"] in REFUSAL_STATUSES for row in evaluated_answerable
    )
    boundaries = [row for row in scored if not row["answerable"]]
    classes = {
        row.get(
            "expected_response_class",
            "answerable" if row["answerable"] else "unclassified_boundary",
        )
        for row in rows
    }
    by_class = {}
    for name in sorted(classes):
        group = [
            row
            for row in rows
            if row.get(
                "expected_response_class",
                "answerable" if row["answerable"] else "unclassified_boundary",
            )
            == name
        ]
        evaluated = [row for row in group if row["evaluation_status"] == "scored"]
        by_class[name] = {
            "case_count": len(group),
            "scored_count": len(evaluated),
            "task_contract_correctness": _ratio(
                sum(row["task_pass_without_evidence"] is True for row in evaluated), len(evaluated)
            ),
            "prediction_status_counts": dict(
                sorted(Counter(row["prediction_status"] for row in group).items())
            ),
        }
    expected_labels = {"|".join(sorted(row.get("expected_statuses", ["unknown"]))) for row in rows}
    confusion = {
        expected: dict(
            sorted(
                Counter(
                    row["prediction_status"]
                    for row in rows
                    if "|".join(sorted(row.get("expected_statuses", ["unknown"]))) == expected
                ).items()
            )
        )
        for expected in sorted(expected_labels)
    }
    return {
        "answerable_task_accuracy_all_cases": _all_case_accuracy(
            sum(
                row["answered"] and row["task_pass_without_evidence"] is True
                for row in evaluated_answerable
            ),
            answerable,
        ),
        "answerable_joint_accuracy_all_cases": _all_case_accuracy(
            sum(row["answered"] and row["joint_pass"] is True for row in evaluated_answerable),
            answerable,
        ),
        "answerable_required_content_accuracy_all_cases": _all_case_accuracy(
            sum(row.get("required_content_agreement") is True for row in content_cases),
            content_cases,
        ),
        "task_precision_among_all_answered": _ratio(
            sum(
                row["answerable"] and row["task_pass_without_evidence"] is True
                for row in all_answered
            ),
            len(all_answered),
        ),
        "joint_precision_among_all_answered": _ratio(
            sum(row["answerable"] and row["joint_pass"] is True for row in all_answered),
            len(all_answered),
        ),
        "refusal_rate_among_scored": _ratio(
            sum(row["prediction_status"] in REFUSAL_STATUSES for row in scored), len(scored)
        ),
        "clarification_rate_among_scored": _ratio(
            sum(row["prediction_status"] == "needs_clarification" for row in scored), len(scored)
        ),
        "invalid_input_rate_among_scored": _ratio(
            sum(row["prediction_status"] == "invalid_input" for row in scored), len(scored)
        ),
        "answerable_false_refusal_rate": _ratio(false_refusals, len(evaluated_answerable)),
        "answerable_unnecessary_clarification_rate": _ratio(
            sum(row["prediction_status"] == "needs_clarification" for row in evaluated_answerable),
            len(evaluated_answerable),
        ),
        "answerable_nonanswer_rate": _ratio(
            sum(not row["answered"] for row in evaluated_answerable), len(evaluated_answerable)
        ),
        "boundary_false_answer_rate": _ratio(
            sum(row["answered"] for row in boundaries), len(boundaries)
        ),
        "boundary_factual_output_rate": _ratio(
            sum(row.get("has_factual_output", row["answered"]) for row in boundaries),
            len(boundaries),
        ),
        "by_expected_response_class": by_class,
        "status_confusion_matrix": confusion,
    }


def aggregate_scores(rows):
    """Report case and field denominators without rewarding blanket refusal."""
    rows = list(rows)
    scored = [row for row in rows if row["evaluation_status"] == "scored"]
    answerable = [row for row in rows if row["answerable"]]
    evaluated_answerable = [row for row in scored if row["answerable"]]
    evaluated_refusals = [row for row in scored if not row["answerable"]]
    answered = [row for row in evaluated_answerable if row["answered"]]
    task_correct = sum(
        row["answered"] and row["task_pass_without_evidence"] is True
        for row in evaluated_answerable
    )
    joint_correct = sum(
        row["answered"] and row["joint_pass"] is True for row in evaluated_answerable
    )
    dimensions = {}
    for name in (
        "facts",
        "conditions",
        "version",
        "evidence",
        "combinations",
        "refusal",
        "answer_text",
    ):
        parts = [row["dimensions"][name] for row in scored if row["dimensions"][name]["applicable"]]
        dimensions[name] = {
            **_ratio(sum(part["pass"] for part in parts), sum(part["total"] for part in parts)),
            "case_pass": sum(part["passed"] is True for part in parts),
            "case_total": len(parts),
        }
    return {
        "case_count": len(rows),
        "scored_count": len(scored),
        "unevaluated_counts": {
            status: sum(row["evaluation_status"] == status for row in rows)
            for status in sorted(UNEVALUATED_STATUSES)
        },
        "answerable_case_count": len(answerable),
        "answerable_evaluation_coverage": _ratio(len(evaluated_answerable), len(answerable)),
        "answerable_answer_coverage": _ratio(
            sum(row["answered"] for row in answerable), len(answerable)
        ),
        "answerable_task_accuracy_without_evidence": {
            **_ratio(task_correct, len(evaluated_answerable)),
            "wilson_95_interval": _wilson(task_correct, len(evaluated_answerable)),
        },
        "answerable_joint_accuracy": {
            **_ratio(joint_correct, len(evaluated_answerable)),
            "wilson_95_interval": _wilson(joint_correct, len(evaluated_answerable)),
        },
        "task_precision_among_answered": _ratio(
            sum(row["task_pass_without_evidence"] is True for row in answered), len(answered)
        ),
        "expected_refusal_correctness": _ratio(
            sum(row["dimensions"]["refusal"]["passed"] is True for row in evaluated_refusals),
            len(evaluated_refusals),
        ),
        **_response_metrics(rows, scored, answerable, evaluated_answerable),
        "unscored_extra_fact_count": sum(row["unscored_extra_fact_count"] for row in scored),
        "dimensions": dimensions,
        "comparison_ready": bool(rows) and len(scored) == len(rows),
        "limitations": [
            "Required typed fields are scored; arbitrary additional prose is not semantically adjudicated.",
            "Source-derived gold is agent-authored and requires independent human review before an accuracy superiority claim.",
            "Field-weighted dimension rates are not per-question factual accuracy.",
            "Legacy expected_refusal_correctness pools all boundary statuses; by_expected_response_class separates refusals, clarification and invalid input.",
            "Legacy task_precision_among_answered is restricted to answerable cases; task_precision_among_all_answered also penalizes answers to boundary cases.",
            "All-case accuracy is delivered correctness over every answerable case; a partial run is a conservative fraction, and an entirely unmeasured arm stays null.",
            "Required content agreement compares requested typed facts/sets only; conditions, version, evidence and arbitrary additional prose require separate checks.",
        ],
    }
