"""Paired task results, keeping incomplete and simulated arms visible."""

from fractions import Fraction
from math import comb

from arknights_agent.errors import InvalidData


def _index(rows, ids):
    indexed = {row["case_id"]: row for row in rows}
    if len(indexed) != len(rows) or set(indexed) != ids:
        raise InvalidData("paired comparison requires identical, unique case IDs")
    return indexed


def _paired(ids, agent, baseline, *, metric="task_pass_without_evidence"):
    counts = dict(both_correct=0, agent_only_correct=0, baseline_only_correct=0, both_incorrect=0)
    for ident in ids:
        left, right = agent[ident], baseline[ident]
        if left["evaluation_status"] != "scored" or right["evaluation_status"] != "scored":
            continue
        a, b = bool(left[metric]), bool(right[metric])
        key = (
            "both_correct"
            if a and b
            else "agent_only_correct"
            if a
            else "baseline_only_correct"
            if b
            else "both_incorrect"
        )
        counts[key] += 1
    paired = sum(counts.values())
    wins, losses = counts["agent_only_correct"], counts["baseline_only_correct"]
    discordant = wins + losses
    # Two-sided exact binomial test conditional on discordant pairs. It does
    # not make an unreviewed benchmark representative of real player traffic.
    p_value = (
        float(
            min(
                Fraction(1),
                Fraction(
                    2 * sum(comb(discordant, k) for k in range(min(wins, losses) + 1)),
                    2**discordant,
                ),
            )
        )
        if paired and discordant
        else 1.0
        if paired
        else None
    )
    return {
        "metric": metric,
        "eligible_case_count": len(ids),
        "paired_case_count": paired,
        "unpaired_case_count": len(ids) - paired,
        **counts,
        "agent_pass_count": counts["both_correct"] + wins,
        "baseline_pass_count": counts["both_correct"] + losses,
        "agent_minus_baseline_rate": (wins - losses) / paired if paired else None,
        "exact_mcnemar_two_sided_p": p_value,
        "test": "two_sided_exact_binomial_on_discordant_pairs",
    }


def compare_arm(cases, agent_scores, baseline_scores, baseline_predictions):
    """Descriptive comparisons never turn missing predictions into wrong facts."""
    ids = {case["id"] for case in cases}
    if len(ids) != len(cases):
        raise InvalidData("paired comparison contains duplicate cases")
    agent = _index(agent_scores, ids)
    baseline = _index(baseline_scores, ids)
    predictions = _index(baseline_predictions, ids)
    if any(agent[ident]["answerable"] != baseline[ident]["answerable"] for ident in ids):
        raise InvalidData("paired comparison disagrees on case answerability")
    answerable = {ident for ident in ids if agent[ident]["answerable"]}
    holdout = {case["id"] for case in cases if case["split"] == "holdout"}
    scored_holdout = bool(holdout) and all(
        agent[ident]["evaluation_status"] == baseline[ident]["evaluation_status"] == "scored"
        for ident in holdout
    )
    real_holdout = bool(holdout) and all(
        predictions[ident].get("trace", {}).get("measured_model_run") is True
        and predictions[ident].get("trace", {}).get("execution_origin") == "live_api"
        and predictions[ident].get("trace", {}).get("simulation") is not True
        for ident in holdout
    )
    reviewed = bool(holdout) and all(
        case["review_status"] == "human_reviewed" for case in cases if case["id"] in holdout
    )
    reasons = []
    if not holdout:
        reasons.append("no_holdout_cases")
    if not scored_holdout:
        reasons.append("holdout_predictions_incomplete_or_unscorable")
    if not real_holdout:
        reasons.append("holdout_baseline_is_not_a_complete_live_model_run")
    if not reviewed:
        reasons.append("holdout_gold_not_marked_human_reviewed")
    return {
        "case_count": len(ids),
        "answerable": _paired(answerable, agent, baseline),
        "answerable_with_evidence": _paired(answerable, agent, baseline, metric="joint_pass"),
        "expected_refusal": _paired(ids - answerable, agent, baseline),
        "holdout_answerable": _paired(holdout & answerable, agent, baseline),
        "holdout_answerable_with_evidence": _paired(
            holdout & answerable, agent, baseline, metric="joint_pass"
        ),
        "holdout_expected_refusal": _paired(holdout - answerable, agent, baseline),
        "readiness": {
            "complete_scored_holdout": scored_holdout,
            "complete_live_baseline_holdout": real_holdout,
            "holdout_marked_human_reviewed": reviewed,
            "ready_for_human_interpretation": not reasons,
            "reasons": reasons,
        },
        "superiority_claim": "not_established",
        "limitations": [
            "paired rates use only jointly scored cases; report unpaired coverage alongside them",
            "human_reviewed is a recorded label, not independent verification of reviewer identity",
            "a discordance p-value does not establish real-world superiority or validate the source gold",
            "task comparisons exclude evidence compliance; closed_book deliberately receives no citations",
        ],
    }
