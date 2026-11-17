"""Apply explicit model links between a prior unknown and a user's explanation.

This module checks audited parent identity, literal provenance and typed slots.
It never interprets the words of an explanation or infers a withdrawal.
"""

from __future__ import annotations

import copy

from arknights_agent import services
from arknights_agent.errors import Conflict, DomainError, InvalidData
from arknights_agent.roguelike_sources import THEME_NAMES
from arknights_agent.runtime.dialogue_context import load_previous, previous_questions

_FIELDS = {"source_run_id", "unknown_quote", "explanation_quote", "disposition", "replacement"}
_KNOWN_TYPES = {"relic_possession", "floor", "difficulty", "knight_alive"}


class _InvalidUnresolvedChain(Exception):
    pass


def condition_slot_key(condition):
    """A typed scalar has one slot; optional labels are not extra conditions.

    Unknown quotation identities and distinct named relic possessions remain
    separate. This examines typed fields only, never the user's words.
    """
    kind = condition["type"]
    if kind in {"difficulty", "floor", "knight_alive"}:
        return kind, None
    return kind, condition["raw_text"] if kind == "unknown" else condition.get("name")


def _key(condition):
    return condition_slot_key(condition)


def _merge(old, current):
    old, current = list(old), list(current)
    if any(not isinstance(c, dict) or not isinstance(c.get("type"), str)
           or c["type"] not in {*_KNOWN_TYPES, "unknown"} or not _literal(c.get("raw_text"))
           for c in [*old, *current]):
        raise InvalidData("invalid typed condition merge state")
    # Within one proposal, two conflicting values are ambiguous. Between
    # rounds the explicitly proposed current value replaces the prior slot.
    current_slots = {}
    for condition in current:
        slot = _key(condition)
        if (slot in current_slots and condition["type"] != "unknown"
                and not services.same_json_value(
                    current_slots[slot].get("value"), condition.get("value"))):
            raise InvalidData("conflicting current values for one typed condition slot")
        current_slots[slot] = copy.deepcopy(condition)
    merged = {}
    for condition in old:
        slot = _key(condition)
        if (slot in merged and slot not in current_slots and condition["type"] != "unknown"
                and not services.same_json_value(merged[slot].get("value"), condition.get("value"))):
            raise InvalidData("conflicting inherited values need an explicit current typed condition")
        merged[slot] = copy.deepcopy(condition)
    merged.update(current_slots)
    return list(merged.values())


def _literal(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 2000


def _user_only(condition, questions):
    if (not isinstance(condition, dict) or not _literal(condition.get("raw_text"))
            or not any(condition["raw_text"] in q for q in questions)
            or condition.get("verification_status") not in {
                "user_reported", "pending_theme_validation"}
            or condition.get("interpretation_status") != "model_proposed"
            or condition.get("citation_ids") != []):
        raise InvalidData("condition resolution requires literal user-only condition provenance")


def _same_unresolved_scope(older, newer):
    """Check source-owned state identity, without selecting from user text."""
    before, after = older.get("dialogue", {}), newer.get("dialogue", {})
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    def themes(answer, state):
        version = answer.get("version", {})
        if not isinstance(version, dict):
            return None
        raw = [value for value in (
            answer.get("theme_id"), version.get("theme_id"),
            state.get("theme_id"), state.get("pending_conditions_theme_id"),
        ) if value is not None]
        if any(not isinstance(value, str) for value in raw):
            return None
        values = set(raw)
        return values if values.issubset(THEME_NAMES) and len(values) <= 1 else None
    prior_themes, next_themes = themes(older, before), themes(newer, after)
    if prior_themes is None or next_themes is None:
        return False
    if prior_themes and prior_themes != next_themes:
        return False
    if (before.get("entity_name") and after.get("entity_name")
            and before["entity_name"] != after["entity_name"]):
        return False
    native = before.get("entity_native_id")
    if native and after.get("entity_native_id") and native != after["entity_native_id"]:
        return False
    if isinstance(native, str) and native and native == after.get("entity_native_id"):
        return True
    previous, current = older.get("named_relic_context"), newer.get("named_relic_context")
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return False
    return (
        previous.get("verification") == current.get("verification")
        == "complete_raw_relic_catalog_and_objects"
        and previous.get("release_id") == current.get("release_id") == older.get("release_id")
        and previous.get("manifest_hash") == current.get("manifest_hash") == older.get("manifest_hash")
        and bool(previous.get("targets"))
        and previous.get("name") == current.get("name") == before.get("entity_name")
        == after.get("entity_name")
        and services.same_json_value(previous.get("targets"), current.get("targets"))
    )


def recover_unresolved_quotes(session, release, parent):
    """Recover an old display only from complete, real, hash-checked parents.

    No display string is split. Every inherited segment is reconstructed from
    the original ordered quote arrays and the exact historical join operation.
    Missing evidence or a different source scope returns ``None``.
    """
    if session is None or not isinstance(parent, dict) or not isinstance(parent.get("run_id"), str):
        return None
    try:
        actual = load_previous(session, parent["run_id"], release.id)
        if (not services.same_json_value(actual, parent)
                or actual.get("manifest_hash") != release.manifest_hash):
            return None
        seen = set()

        def recover(answer, depth):
            ident = answer.get("run_id")
            if depth >= 8 or ident in seen:
                raise _InvalidUnresolvedChain
            seen.add(ident)
            state = answer.get("dialogue", {})
            dialogue_state = answer.get("dialogue_state", {})
            version = answer.get("version", {})
            if (not isinstance(state, dict) or not isinstance(dialogue_state, dict)
                    or not isinstance(version, dict)):
                raise _InvalidUnresolvedChain
            history = previous_questions(answer)
            source = state.get("model_interpretation", {})
            current = source.get("unresolved", []) if isinstance(source, dict) else []
            explicit = state.get("unresolved_quotes")
            display = state.get("unresolved_text", "")
            if (not isinstance(display, str) or not isinstance(current, list)
                    or any(not _literal(q) or not any(q in user for user in history) for q in current)
                    or explicit is not None and (not isinstance(explicit, list) or any(
                        not _literal(q) or not any(q in user for user in history) for q in explicit))):
                raise _InvalidUnresolvedChain
            parent_id = dialogue_state.get("previous_run_id")
            prior, prior_quotes = None, None
            if len(history) == 1:
                if parent_id is not None:
                    raise _InvalidUnresolvedChain
            else:
                if not isinstance(parent_id, str) or not parent_id:
                    raise _InvalidUnresolvedChain
                prior = load_previous(session, parent_id, release.id)
                if (prior.get("manifest_hash") != release.manifest_hash
                        or previous_questions(prior) != history[:-1]):
                    raise _InvalidUnresolvedChain
                prior_quotes = recover(prior, depth + 1)
            if explicit is not None and "；".join(explicit) == display:
                inherited = any(q not in current for q in explicit)
                if inherited and (prior is None or prior_quotes is None
                                  or not _same_unresolved_scope(prior, answer)
                                  or any(q not in current and q not in prior_quotes for q in explicit)):
                    return None
                return list(explicit)
            if "；".join(current) == display:
                return list(current)
            if (prior is None or prior_quotes is None
                    or not _same_unresolved_scope(prior, answer)):
                return None
            historical = "；".join(filter(None, [
                prior.get("dialogue", {}).get("unresolved_text", ""), "；".join(current)]))
            combined = [*prior_quotes, *current]
            if historical == display and "；".join(combined) == display:
                return combined
            return None

        return recover(actual, 0)
    except (DomainError, _InvalidUnresolvedChain):
        return None


def _unknowns(parent, session, release):
    """Deduplicate mirrors, but retain differing interpretations of one quote."""
    state = parent.get("dialogue", {})
    if not isinstance(state, dict):
        raise InvalidData("invalid authoritative condition state")
    history = previous_questions(parent)
    result = {}
    for field in ("user_reported_conditions", "pending_user_reported_conditions"):
        conditions = state.get(field, [])
        if not isinstance(conditions, list):
            raise InvalidData("invalid authoritative user condition list")
        for condition in conditions:
            if not isinstance(condition, dict) or condition.get("type") != "unknown":
                continue
            _user_only(condition, history)
            identity = {key: condition.get(key) for key in ("type", "name", "value", "raw_text")}
            result.setdefault(condition["raw_text"], {})[
                services.content_digest(identity)] = copy.deepcopy(condition)
    # Older generic answers retained unresolved literals separately. Use only
    # the actual original model list, never split a generated display string.
    original = state.get("model_interpretation", {})
    quotes = original.get("unresolved", []) if isinstance(original, dict) else []
    if not isinstance(quotes, list):
        raise InvalidData("invalid authoritative unresolved literal list")
    recovered = recover_unresolved_quotes(session, release, parent)
    if recovered is not None:
        quotes = list(dict.fromkeys([*recovered, *quotes]))
    for quote in quotes:
        if not _literal(quote) or not any(quote in q for q in history):
            raise InvalidData("authoritative unresolved quote lacks user provenance")
        if quote not in result:
            condition = {
                "type": "unknown", "name": None, "value": None, "raw_text": quote,
                "verification_status": "user_reported", "interpretation_status": "model_proposed",
                "citation_ids": [], "quote_source": "actual_user_question_history",
            }
            result[quote] = {services.content_digest(condition): condition}
    return result, list(dict.fromkeys(quotes))


def _scope(parent, binding):
    if (not isinstance(binding, dict) or set(binding) != {
            "theme_id", "theme_scope", "entity_name"}
            or not isinstance(binding["theme_scope"], str)
            or binding["theme_scope"] not in {"single", "all"}
            or binding["theme_id"] is not None and (
                not isinstance(binding["theme_id"], str) or binding["theme_id"] not in THEME_NAMES)
            or not isinstance(binding["entity_name"], str) or not binding["entity_name"].strip()
            or binding["theme_scope"] == "all" and binding["theme_id"] is not None):
        raise InvalidData("condition resolution requires a validated source scope")
    state = parent.get("dialogue", {})
    version = parent.get("version", {})
    if not isinstance(state, dict) or not isinstance(version, dict):
        raise InvalidData("invalid authoritative condition scope state")
    values = [value for value in (
        parent.get("theme_id"), version.get("theme_id"),
        state.get("theme_id"), state.get("pending_conditions_theme_id"),
    ) if value is not None]
    if any(not isinstance(value, str) for value in values):
        raise InvalidData("invalid authoritative condition theme identity")
    themes = set(values)
    if not themes.issubset(THEME_NAMES) or len(themes) > 1:
        raise Conflict("authoritative condition scopes disagree")
    if themes and (binding["theme_scope"] != "single" or binding["theme_id"] not in themes):
        raise InvalidData("condition resolution cannot cross an established source theme")
    if state.get("theme_scope") == "all":
        named = parent.get("named_relic_context", {})
        if (not isinstance(named, dict) or not named.get("targets")
                or named.get("release_id") != parent.get("release_id")
                or named.get("manifest_hash") != parent.get("manifest_hash")
                or named.get("verification") != "complete_raw_relic_catalog_and_objects"
                or named.get("name") != state.get("entity_name")
                or named.get("name") != binding["entity_name"]):
            raise InvalidData("all-theme unknown resolution requires the same verified named group")


def _replacement(raw, explanation, validate):
    if (not isinstance(raw, dict) or set(raw) - {
            "type", "name", "value", "quote", "reference", "subject_quote"}
            or not {"type", "name", "value", "quote"}.issubset(raw)
            or not isinstance(raw.get("type"), str) or raw["type"] not in _KNOWN_TYPES
            or not _literal(raw.get("quote")) or raw["quote"] not in explanation
            or raw.get("reference", "current") != "current"
            or raw.get("subject_quote") is not None):
        raise InvalidData("resolution replacement needs a known typed current explanation")
    kind, expected = raw["type"], raw["value"]
    if kind in {"floor", "difficulty"}:
        if type(expected) is not int or expected < (1 if kind == "floor" else 0):
            raise InvalidData("resolution replacement has an invalid numeric condition")
    elif kind == "knight_alive":
        if type(expected) is not bool:
            raise InvalidData("resolution replacement has an invalid boolean condition")
    else:
        if not isinstance(raw["name"], str) or not 1 <= len(raw["name"].strip()) <= 120:
            raise InvalidData("resolution replacement requires a source relic name")
        if type(expected) is bool:
            expected = "owned" if expected else "not_owned"
        elif not isinstance(expected, str) or expected not in {"owned", "not_owned"}:
            raise InvalidData("resolution replacement has an invalid possession condition")
    if kind != "relic_possession" and raw["name"] is not None:
        raise InvalidData("numeric and knight resolution slots cannot invent an object name")
    value = validate(copy.deepcopy(raw))
    if (not isinstance(value, dict) or set(value) - {
            "type", "name", "value", "raw_text", "verification_status", "interpretation_status",
            "citation_ids", "validation_scope"} or value.get("type") != raw["type"]):
        raise InvalidData("resolution replacement did not pass the typed condition validator")
    _user_only(value, [explanation])
    if (value["raw_text"] != raw["quote"] or value.get("type") not in _KNOWN_TYPES
            or not services.same_json_value(value.get("value"), expected)
            or value.get("name") != raw["name"]):
        raise InvalidData("validated replacement differs from the proposed explanation")
    return copy.deepcopy(value)


def apply_condition_resolutions(session, release, question, previous_answer, resolutions, *,
                               inherited_conditions, current_conditions, scope_binding,
                               validate_replacement, acknowledged_unresolved=None,
                               require_explicit_unknown_inventory=False):
    """Return copied user state after explicit, atomic model-owned resolutions.

    The caller first verifies the current source target/scope and validates its
    inherited/current conditions. ``validate_replacement`` must use that same
    typed/source validator and return exactly one user-only condition. The
    original parent is independently loaded here; callers must not edit it.
    """
    if type(require_explicit_unknown_inventory) is not bool:
        raise InvalidData("invalid explicit unknown inventory policy")
    if not isinstance(resolutions, list) or len(resolutions) > 12:
        raise InvalidData("invalid condition resolution list")
    if not isinstance(inherited_conditions, list) or not isinstance(current_conditions, list):
        raise InvalidData("invalid condition resolution state")
    acknowledged_unresolved = [] if acknowledged_unresolved is None else acknowledged_unresolved
    if (not isinstance(acknowledged_unresolved, list) or len(acknowledged_unresolved) > 12
            or any(not _literal(quote) for quote in acknowledged_unresolved)):
        raise InvalidData("invalid explicit remaining unknown acknowledgement")
    merged = _merge(inherited_conditions, current_conditions)
    if not resolutions and not require_explicit_unknown_inventory:
        return {"conditions": merged, "resolved_unknown_quotes": [], "provenance": []}
    if (session is None or not _literal(question) or not isinstance(previous_answer, dict)
            or not isinstance(previous_answer.get("run_id"), str)):
        raise InvalidData("condition resolution requires the real immediate parent")
    parent = load_previous(session, previous_answer["run_id"], release.id)
    if (not services.same_json_value(parent, previous_answer)
            or parent.get("manifest_hash") != release.manifest_hash):
        raise Conflict("condition resolution parent differs from its audited response")
    _scope(parent, scope_binding)
    unknowns, unresolved_quotes = _unknowns(parent, session, release)
    history = previous_questions(parent)
    if any(not any(quote in user for user in [*history, question])
           for quote in acknowledged_unresolved):
        raise InvalidData("remaining unknown acknowledgement requires a literal user quote")
    inherited_conditions = copy.deepcopy(inherited_conditions)
    for quote in unresolved_quotes:
        if (len(unknowns[quote]) == 1 and not any(
                c.get("type") == "unknown" and c.get("raw_text") == quote
                for c in inherited_conditions)):
            # Legacy generic answers stored this literal in unresolved only.
            # Reconstruct its user-only identity from the actual parent list,
            # without changing the old answer or interpreting any words.
            inherited_conditions.append(copy.deepcopy(next(iter(unknowns[quote].values()))))
    for condition in inherited_conditions:
        # Some callers already validated/merged current typed conditions. An
        # unknown selected for removal still has to match the old audit below.
        _user_only(condition, [*history, question])
    for condition in current_conditions:
        _user_only(condition, [*history, question])
    merged = _merge(inherited_conditions, current_conditions)
    parent_hash = services.content_digest(parent)
    resolved, provenance, replacements = [], [], {}
    current_slots = {_key(c): c for c in current_conditions}
    for resolution in resolutions:
        if not isinstance(resolution, dict) or set(resolution) != _FIELDS:
            raise InvalidData("invalid explicit condition resolution fields")
        old, explanation = resolution["unknown_quote"], resolution["explanation_quote"]
        if (resolution["source_run_id"] != parent["run_id"] or not _literal(old)
                or not _literal(explanation) or explanation not in question):
            raise InvalidData("condition resolution needs the actual parent and current literal")
        if old in resolved or len(unknowns.get(old, {})) != 1:
            raise InvalidData("condition resolution must identify one unique prior unknown")
        if old in acknowledged_unresolved:
            raise InvalidData(
                "condition resolution conflicts with explicit remaining unknown acknowledgement"
            )
        candidates = [c for c in inherited_conditions
                      if c.get("type") == "unknown" and c.get("raw_text") == old]
        if len(candidates) != 1 or ("unknown", old) in current_slots:
            raise InvalidData("resolved unknown is not uniquely inherited or is reintroduced")
        actual = next(iter(unknowns[old].values()))
        if any(candidates[0].get(k) != actual.get(k) for k in ("type", "name", "value", "raw_text")):
            raise Conflict("inherited unknown differs from the audited parent")
        disposition, raw = resolution["disposition"], resolution["replacement"]
        replacement = None
        if disposition == "replace":
            replacement = _replacement(raw, explanation, validate_replacement)
            slot = _key(replacement)
            for existing in (current_slots.get(slot), replacements.get(slot)):
                if existing is not None and not services.same_json_value(
                    existing.get("value"), replacement.get("value")
                ):
                    raise InvalidData("condition resolution conflicts with another current typed slot")
            replacements[slot] = replacement
        elif disposition != "retract" or raw is not None:
            raise InvalidData("explicit withdrawal requires retract and no replacement")
        resolved.append(old)
        provenance.append({
            "protocol": "audited-unknown-condition-resolution-v1",
            "source_run_id": parent["run_id"], "source_answer_response_hash": parent_hash,
            "source_unknown_sha256": services.content_digest(actual),
            "unknown_quote": old, "explanation_quote": explanation,
            "disposition": disposition, "replacement": copy.deepcopy(replacement),
            "release_id": release.id, "manifest_hash": release.manifest_hash,
            "scope_binding": copy.deepcopy(scope_binding),
            "interpretation_origin": "explicit_model_proposal",
            "state_verification": "user_reported_only", "source_answer_mutated": False,
        })
    retained = [c for c in merged if not (c["type"] == "unknown" and c["raw_text"] in resolved)]
    result = _merge(retained, replacements.values())
    remaining = list(dict.fromkeys(
        c["raw_text"] for c in result if c["type"] == "unknown" and c["raw_text"] in unknowns))
    unacknowledged = [quote for quote in remaining if quote not in acknowledged_unresolved]
    repair = None
    if unacknowledged:
        repair = {
            "code": "partial_condition_resolution",
            "fields": ["condition_resolutions", "unresolved"],
            "detail": ("本次尚未明确处置历史未知条件：" if not resolutions
                       else "本次只消解了部分历史未知条件，仍未处理：")
                      + "、".join(f"「{quote}」" for quote in unacknowledged)
                      + "。请由模型根据当前用户解释自主处理每条真实旧引文；若确为仍不明的独立条件，"
                      "须在本次unresolved逐字确认该完整引文。服务器不会合并同义或包含片段，"
                      "也不会自动清除任何未知。重试仍使用同一真实parent，必须完整重提本轮所有"
                      "condition_resolutions（包括刚才已提交的项），不能只增量提交剩余项。",
            "source_run_id": parent["run_id"],
            "source_answer_response_hash": parent_hash,
            "remaining_unknown_quotes": remaining,
            "unacknowledged_unknown_quotes": unacknowledged,
            "resolved_unknown_quotes": list(resolved),
            "submitted_condition_resolutions": copy.deepcopy(resolutions),
            "retry_uses_same_parent": True,
            "must_resubmit_all_resolutions": True,
            "proposal_mutated": False,
            "source_answer_mutated": False,
            "automatic_semantic_merge": False,
        }
    result = {
        "conditions": result, "resolved_unknown_quotes": resolved, "provenance": provenance,
        "remaining_unknown_quotes": remaining, "unacknowledged_unknown_quotes": unacknowledged,
        "proposal_repair_request": repair,
    }
    if require_explicit_unknown_inventory:
        result["inventory_verification"] = {
            "protocol": "audited-inherited-unknown-inventory-v1",
            "source_run_id": parent["run_id"], "source_answer_response_hash": parent_hash,
            "source_unknown_quotes": list(unknowns),
            "resolved_unknown_quotes": list(resolved),
            "acknowledged_remaining_unknown_quotes": [q for q in remaining if q in acknowledged_unresolved],
            "unacknowledged_unknown_quotes": unacknowledged,
            "all_inherited_unknowns_explicitly_handled": not unacknowledged,
            "automatic_semantic_merge": False, "source_answer_mutated": False,
        }
    return result
