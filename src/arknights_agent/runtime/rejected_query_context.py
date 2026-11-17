"""Revalidate quote-location feedback without treating it as source identity."""

from arknights_agent import services
from arknights_agent.errors import Conflict, InvalidData
from arknights_agent.runtime.dialogue_context import load_previous, previous_questions


def verify_history_quote_lookup(session, release, failed_answer, attempt):
    """Replay the literal lookup against the rejected turn's actual parent.

    This is a separate protocol from source_candidates. A matching user quote
    locates text only; it never selects an object, condition, or source fact.
    """
    repair = attempt["repair"]
    lookup = repair["source_lookup"]
    proposal = attempt["submitted_arguments"].get("interpretation")
    if (repair.get("code") != "unbound_relic_history_evidence"
            or not isinstance(lookup, dict)
            or set(lookup) != {
                "source_run_id", "source_answer_response_hash", "parent_status",
                "matched_history_indices", "quote_locations_only",
                "selector_or_semantic_binding_verified", "proposal_mutated",
            }
            or lookup.get("quote_locations_only") is not True
            or lookup.get("selector_or_semantic_binding_verified") is not False
            or lookup.get("proposal_mutated") is not False
            or not isinstance(proposal, dict)
            or proposal.get("intent") != "relic_effect"
            or not isinstance(lookup.get("matched_history_indices"), list)
            or any(type(i) is not int for i in lookup["matched_history_indices"])):
        raise InvalidData("invalid rejected query history quote lookup")
    state = failed_answer.get("dialogue_state")
    parent_id = state.get("previous_run_id") if isinstance(state, dict) else None
    if not isinstance(parent_id, str) or lookup["source_run_id"] != parent_id:
        raise Conflict("rejected query quote lookup differs from actual parent chain")
    parent = load_previous(session, parent_id, release.id, allow_general=True)
    history = previous_questions(parent)
    if previous_questions(failed_answer) != [*history, failed_answer["question"]]:
        raise Conflict("rejected query quote lookup history differs from actual parent")
    quote = proposal.get("entity_evidence")
    expected = {
        "source_run_id": parent["run_id"],
        "source_answer_response_hash": services.content_digest(parent),
        "parent_status": parent.get("status"),
        "matched_history_indices": [i for i, question in enumerate(history)
                                    if isinstance(quote, str) and quote and quote in question],
        "quote_locations_only": True,
        "selector_or_semantic_binding_verified": False,
        "proposal_mutated": False,
    }
    if not services.same_json_value(lookup, expected):
        raise Conflict("rejected query quote locations differ from audited user history")

