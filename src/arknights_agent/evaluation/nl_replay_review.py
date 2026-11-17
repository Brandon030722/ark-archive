"""Independent checks over saved actual development dialogue tool outputs.

This consumes predictions after execution. It never creates a model proposal,
selects a source candidate for the model, or rewrites a saved prediction.
"""

from __future__ import annotations

import copy

from arknights_agent import services


def same(left, right):
    return services.canonical_json(left) == services.canonical_json(right)


def review_dialogue(expected, answer, previous):
    errors, checks, unverified = [], [], []
    selectors = expected["selectors"]
    dialogue = answer.get("dialogue", {})
    reference = selectors.get("reference")
    number = selectors.get("selection_number")
    previous_dialogue = previous.get("dialogue", {}) if previous else {}
    if reference == "previous_candidate" and previous:
        candidates = previous.get("candidates", [])
        if number is None and candidates:
            if answer.get("status") != "needs_clarification" or not same(answer.get("candidates"), candidates):
                errors.append("unselected_actual_candidates_not_preserved")
            else:
                checks.append("unselected_actual_candidate_order")
        elif type(number) is int and 1 <= number <= len(candidates):
            # The protocol uses displayed numbers, not an invented catalog sort.
            actual = [c for c in candidates if c.get("number") == number]
            if len(actual) != 1 or dialogue.get("entity_native_id") != actual[0]["native_id"]:
                errors.append("selected_candidate_not_bound_to_actual_displayed_number")
            else:
                checks.append("actual_candidate_selection")
    if reference == "previous_menu" and previous and type(number) is int:
        options = previous.get("event_menu", {}).get("options", [])
        selected = answer.get("selected_option")
        if 1 <= number <= len(options):
            if not isinstance(selected, dict):
                errors.append("selected_menu_option_missing")
            else:
                value = copy.deepcopy(selected)
                selected_number = value.pop("number", None)
                value.pop("citation_ids", None)
                if selected_number != number or not same(value, options[number - 1]):
                    errors.append("selected_menu_option_differs_from_actual_previous_order")
                else:
                    checks.append("actual_menu_selection")
            if dialogue.get("entity_native_id") != previous_dialogue.get("entity_native_id"):
                errors.append("menu_selection_changed_event_identity")
    state = dialogue.get("user_reported_conditions", []) + dialogue.get("pending_user_reported_conditions", [])
    questions = dialogue.get("original_user_questions", [])
    for condition in state:
        if (condition.get("verification_status") != "user_reported"
                or condition.get("citation_ids")
                or not any(condition.get("raw_text", "\x00") in q for q in questions)):
            errors.append("user_condition_became_fact_or_lost_actual_quote")
        else:
            checks.append("actual_quoted_user_state")
    def condition_key(condition):
        kind = condition.get("type")
        if kind == "relic_possession":
            return kind, condition.get("name")
        if kind == "unknown":
            return kind, condition.get("quote", condition.get("raw_text"))
        return kind, None

    required = selectors.get("conditions", []) + ([expected["condition_update"]]
                if "condition_update" in expected else [])
    for condition in required:
        if not any(condition_key(c) == condition_key(condition)
                   and same(c.get("value"), condition.get("value"))
                   and c.get("raw_text") == condition.get("quote") for c in state):
            errors.append("typed_condition_value_or_quote_not_preserved:" + condition["type"])
        else:
            checks.append("strict_typed_condition_value")
    if "condition_update" in expected and previous:
        prior = previous_dialogue.get("user_reported_conditions", []) + previous_dialogue.get("pending_user_reported_conditions", [])
        old_theme, new_theme = previous_dialogue.get("theme_id"), dialogue.get("theme_id")
        changed = condition_key(expected["condition_update"])
        for condition in prior:
            if condition_key(condition) == changed or old_theme and new_theme and old_theme != new_theme:
                continue
            if not any(condition_key(c) == condition_key(condition)
                       and same(c.get("value"), condition.get("value")) for c in state):
                errors.append("independent_actual_condition_dropped:" + condition["type"])
            else:
                checks.append("independent_actual_condition_retained")
        if previous_dialogue.get("unresolved_text"):
            # Unknown phrases are tested individually through actual pending
            # state; unresolved text lacking typed state has no complete oracle.
            if not any(c.get("type") == "unknown" for c in prior):
                unverified.append("previous_unresolved_text_without_complete_typed_state")
    if "dialogue.no_revived_theme_conditions" in expected.get("context_rules", []):
        old_theme = previous_dialogue.get("theme_id") or previous_dialogue.get("pending_conditions_theme_id")
        new_theme = dialogue.get("theme_id")
        old = previous_dialogue.get("user_reported_conditions", []) + previous_dialogue.get("pending_user_reported_conditions", [])
        if old_theme and new_theme and old_theme != new_theme:
            current_quotes = {c.get("quote") for c in selectors.get("conditions", [])}
            inherited = [c for c in state if any(same(c, before) for before in old)
                         and c.get("raw_text") not in current_quotes]
            if inherited:
                errors.append("old_theme_conditions_revived")
            else:
                checks.append("theme_change_no_old_conditions")
    if expected["module"] == "roguelike" and expected["intent"] != "clarify":
        if dialogue.get("intent") != expected["intent"]:
            errors.append("actual_intent_differs_from_injected_task")
        else:
            checks.append("task_intent_retained")
    # These rules describe meanings absent from typed annotations. It would be
    # circular to claim that injecting those annotations tests their extraction.
    if "conditions_from_user" in expected:
        unverified.append("unannotated_literal_condition_extraction_requires_model")
    return {"passed": not errors, "errors": errors, "checks": checks, "unverified": unverified}
