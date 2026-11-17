"""Validate a model's query proposal; all game facts come from pinned sources.

This is an identity/provenance boundary, not a colloquial-language parser. The
model proposes intent and user-state interpretation. It cannot supply answers,
source fields, releases, citations, menu membership, or verified game state.
"""

from __future__ import annotations

import copy

from arknights_agent import services
from arknights_agent.errors import Conflict, DomainError, InvalidData
from arknights_agent.roguelike import _Answer, answer_roguelike
from arknights_agent.roguelike_dialogue import (
    _canonical,
    _conditional_route,
    _decorate,
    _merge_conditions,
    _name_forms,
    _prior,
    _source_ending_ordinal,
    _state_intro,
)
from arknights_agent.roguelike_guides import render_selected_option
from arknights_agent.roguelike_sources import THEME_NAMES

_INTENTS = {"relic_effect", "relic_acquisition", "ending_route", "event_menu", "document",
            "unknown_probability", "clarify"}
_REFERENCES = {"current", "source_menu", "previous_entity", "previous_candidate", "previous_menu"}
_FIELDS = {"intent", "theme_id", "entity_name", "ending_number", "selection_number",
           "selection_evidence", "entity_evidence", "theme_evidence", "ending_evidence",
           "reference", "conditions", "unresolved", "theme_scope",
           "entity_evidence_question_index", "condition_resolutions", "allow_partial_source_answer"}
_CONDITION_TYPES = {"relic_possession", "floor", "difficulty", "knight_alive", "unknown"}
_SOURCE_INTENTS = {
    "roguelike_ending": ["ending_route", "document", "clarify"],
    "roguelike_relic": ["relic_effect", "relic_acquisition", "unknown_probability", "document", "clarify"],
    "roguelike_event": ["event_menu", "document", "clarify"],
}


def _proposal(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if (not isinstance(value, dict) or set(value) - _FIELDS
            or not isinstance(value.get("intent"), str) or value["intent"] not in _INTENTS):
        raise InvalidData("invalid roguelike query proposal")
    result = {"theme_id": None, "entity_name": None, "ending_number": None,
              "selection_number": None, "selection_evidence": None,
              "entity_evidence": None, "theme_evidence": None, "ending_evidence": None,
              "reference": "current", "conditions": [],
              "unresolved": [], **copy.deepcopy(value)}
    scope = result.pop("theme_scope", None)
    if scope is not None and (not isinstance(scope, str) or scope not in {"single", "all"}):
        raise InvalidData("invalid theme scope")
    if result["intent"] == "relic_effect":
        result["theme_scope"] = scope or "single"
    elif scope == "all":
        raise InvalidData("all-theme scope requires the relic_effect intent")
    partial = result.pop("allow_partial_source_answer", False)
    if type(partial) is not bool:
        raise InvalidData("partial source answer selector must be a boolean")
    if partial:
        if result["intent"] != "relic_effect":
            raise InvalidData("partial source answers require the relic_effect intent")
        result["allow_partial_source_answer"] = True
    index = result.pop("entity_evidence_question_index", None)
    if index is not None:
        if type(index) is not int or not 0 <= index < 8:
            raise InvalidData("invalid entity evidence question index")
        result["entity_evidence_question_index"] = index
    resolutions = result.pop("condition_resolutions", [])
    if not isinstance(resolutions, list) or len(resolutions) > 12:
        raise InvalidData("invalid condition resolutions")
    if resolutions:
        result["condition_resolutions"] = resolutions
    if result["theme_id"] is not None and (not isinstance(result["theme_id"], str) or result["theme_id"] not in THEME_NAMES):
        raise InvalidData("invalid proposal theme")
    if not isinstance(result["reference"], str) or result["reference"] not in _REFERENCES:
        raise InvalidData("invalid proposal reference")
    if result["entity_name"] is not None and (not isinstance(result["entity_name"], str)
                                              or not result["entity_name"].strip()):
        raise InvalidData("invalid proposal entity")
    for key, maximum in (("ending_number", 6), ("selection_number", 50)):
        value = result[key]
        if value is not None and (type(value) is not int or not 1 <= value <= maximum):
            raise InvalidData("invalid proposal number")
    for key in ("selection_evidence", "entity_evidence", "theme_evidence", "ending_evidence"):
        evidence = result[key]
        if evidence is not None and (not isinstance(evidence, str) or not evidence.strip()
                                     or len(evidence) > 2000):
            raise InvalidData("invalid literal evidence")
    for key in ("conditions", "unresolved"):
        if not isinstance(result[key], list) or len(result[key]) > 12:
            raise InvalidData("invalid proposal list")
    for c in result["conditions"]:
        if (not isinstance(c, dict) or set(c) - {"type", "name", "value", "quote", "reference", "subject_quote"}
                or not {"type", "name", "value", "quote"}.issubset(c)
                or c["type"] not in _CONDITION_TYPES or not isinstance(c["quote"], str)
                or not c["quote"].strip() or len(c["quote"]) > 2000
                or c["name"] is not None and not isinstance(c["name"], str)
                or c["value"] is not None and type(c["value"]) not in {str, int, bool}):
            raise InvalidData("invalid proposal condition")
        if (not isinstance(c.get("reference", "current"), str)
                or c.get("reference", "current") not in {"current", "previous_entity"}):
            raise InvalidData("invalid condition reference")
        subject = c.get("subject_quote")
        if subject is not None and (not isinstance(subject, str) or not subject.strip() or len(subject) > 120):
            raise InvalidData("invalid condition subject quote")
        if c.get("reference", "current") == "current" and subject is not None:
            raise InvalidData("current condition cannot assert a previous subject quote")
        if c.get("reference") == "previous_entity" and (c["type"] != "relic_possession" or subject is None):
            raise InvalidData("previous condition requires a relic possession and subject_quote")
    if any(not isinstance(q, str) or not q.strip() for q in result["unresolved"]):
        raise InvalidData("invalid unresolved quote")
    return result


def _questions(question, previous):
    if previous is None:
        return [question]
    state = previous.get("dialogue_state")
    history = state.get("questions") if isinstance(state, dict) else [previous["question"]]
    if (not isinstance(history, list) or not history or history[-1] != previous["question"]
            or any(not isinstance(q, str) or not q.strip() for q in history)):
        raise InvalidData("invalid authoritative question history")
    return [*history, question]


def _literal(name, question):
    return any(form and form in question for form in _name_forms(name))


def _identity_rows(rows, name):
    """Accept complete source identities, never an ID prefix or guessed nickname."""
    return [r for r in rows if name in {r["native_id"], r.get("source_id"), *_name_forms(r["name"])}]


def _explicit_evidence_rows(rows, evidence):
    """Match complete published IDs in the model's evidence, never user prose.

    Source IDs are opaque identifiers. Boundary checks keep a source ID from
    matching a prefix of another ID or an invented extension. Multiple IDs do
    not identify a selected object and retain the ordinary ambiguity path.
    """
    if evidence is None:
        return []
    found = []
    for row in rows:
        native = row["native_id"]
        if not native.startswith(str(row["parent_native_id"]) + ":/"):
            continue
        start = evidence.find(native)
        while start >= 0:
            end = start + len(native)
            before = evidence[start - 1] if start else ""
            after = evidence[end] if end < len(evidence) else ""
            left_continues = bool(before and (before.isalnum() or before in "_:/~.-%+"))
            right_continues = bool(after and (after.isalnum() or after in "_:/~.-%+"))
            if not left_continues and not right_continues:
                found.append(row)
                break
            start = evidence.find(native, start + 1)
    if len({r["native_id"] for r in found}) == 1 and len(found) != 1:
        raise Conflict("explicit source identity has differing catalog members")
    return found


def _replay_explicit_identity(session, release, question, store, row, rows):
    """Verify identity feedback against complete original source bytes."""
    probe = _Answer(session, release, question, store)
    probe.catalog_rows = {r["id"]: r for r in rows}
    record = probe.record(row["id"])
    if any(record[key] != row[key] for key in (
            "id", "kind", "native_id", "name", "parent_native_id")):
        raise Conflict("explicit identity differs from the complete source record")
    # This private proof is deliberately not an answer or a selected object.
    # Its citations are discarded; only verified identity metadata may repair
    # the model's proposal, which remains unchanged.
    probe.cite(record, "", record["data"])


def _literal_source_candidates(session, release, question, store, rows, selector, theme, kind):
    """Retrieve source names using a model slot, without interpreting user text.

    Results are correction observations, not selected objects. No classifier
    words are stripped and no candidate becomes a parameter automatically.
    """
    candidates = []
    for row in rows:
        if theme is not None and row["parent_native_id"] != theme:
            continue
        if kind is not None and row["kind"] != kind:
            continue
        forms = [f for f in _name_forms(row["name"]) if f]
        literal = [f for f in forms if f in selector or selector in f]
        if literal:
            candidates.append((max(map(len, literal)), row))
    candidates.sort(key=lambda pair: (-pair[0], pair[1]["native_id"]))
    verified = []
    for _, row in candidates[:5]:
        _replay_explicit_identity(session, release, question, store, row, rows)
        verified.append(_source_target(row))
    return {
        "method": "literal_source_name_lookup_from_model_selector",
        "selector": selector,
        "theme_id": theme,
        "record_kind": kind,
        "total_count": len(candidates),
        "truncated": len(candidates) > len(verified),
        "source_candidates": verified,
        "selected_automatically": False,
    }


def _conditions(proposal, questions, rows, theme, *, pending=False, bindings=()):
    if pending and theme is not None:
        raise InvalidData("pending conditions cannot assert a theme")
    results = []
    for c in proposal["conditions"]:
        quoted_users = [q for q in questions if c["quote"] in q]
        if not quoted_users:
            raise InvalidData("condition quote is not a literal user statement")
        name, value, kind = c["name"], c["value"], c["type"]
        if kind == "relic_possession":
            binding = next((b for b in bindings if b["name"] == name and b["quote"] == c["quote"]), None)
            if not isinstance(name, str):
                raise InvalidData("condition relic needs a complete source identity or true previous-object binding")
            if binding is not None and not pending and binding["theme_id"] != theme:
                raise InvalidData("condition reference cannot cross source themes")
            if not pending:
                matches = [r for r in rows if r["kind"] == "roguelike_relic"
                           and name in _name_forms(r["name"])
                           and r["parent_native_id"] == theme]
                names = {r["name"] for r in matches}
                if len(names) != 1 or len(matches) != 1:
                    raise InvalidData("condition relic is not uniquely source-owned")
                name = next(iter(names))
            if value is True or value in ("owned", "held", "持有", "已有"):
                value = "owned"
            elif value is False or value in ("not_owned", "missing", "未持有", "没有"):
                value = "not_owned"
            else:
                raise InvalidData("invalid reported relic state")
        elif kind in {"floor", "difficulty"}:
            if type(value) is not int or value < 0 or kind == "floor" and value == 0:
                raise InvalidData("invalid reported numeric state")
        elif kind == "knight_alive":
            if type(value) is not bool:
                raise InvalidData("invalid reported knight state")
        result = {"type": kind, "value": value, "raw_text": c["quote"],
                  "verification_status": "user_reported", "interpretation_status": "model_proposed",
                  "citation_ids": []}
        if name is not None:
            result["name"] = name
        if pending:
            result.update(verification_status="pending_theme_validation",
                          validation_scope="literal_user_quote_and_typed_value_only")
        results.append(result)
    grouped = {}
    for c in results:
        grouped.setdefault((c["type"], c.get("name")), set()).add(str(c["value"]))
    if any(len(values) > 1 for values in grouped.values()):
        raise InvalidData("conflicting user condition interpretations")
    return results


def _pending_proposal(conditions):
    """Replay only audited user state through the same typed/quote validator."""
    if not isinstance(conditions, list):
        raise InvalidData("invalid pending user conditions")
    fields = {"type", "name", "value", "raw_text", "verification_status",
              "interpretation_status", "citation_ids", "validation_scope"}
    typed = []
    for c in conditions:
        if (not isinstance(c, dict) or set(c) - fields
                or not {"type", "value", "raw_text"}.issubset(c)
                or c.get("verification_status") != "pending_theme_validation"
                or c.get("validation_scope") != "literal_user_quote_and_typed_value_only"
                or c.get("interpretation_status") != "model_proposed"
                or c.get("citation_ids") != []):
            raise InvalidData("invalid pending user condition provenance")
        validated = _proposal({"intent": "clarify", "conditions": [{
            "type": c["type"], "name": c.get("name"), "value": c["value"], "quote": c["raw_text"]}]})
        typed.extend(validated["conditions"])
    # The per-model-turn limit stays twelve; audited state can accumulate over
    # the existing bounded dialogue, so validate each persisted item separately.
    return {"conditions": typed}


def pending_theme_clarification_binding(session, release, question, proposal, previous, condition):
    """Reopen one audited numeric condition after the model supplies its theme.

    This proves only that the same typed user assertion was left pending by a
    real clarification. It neither interprets a comparison in the quote nor
    verifies player state. Named, unknown and character-specific conditions
    require their separate source/condition-resolution boundaries.
    """
    theme, evidence = proposal.get("theme_id"), proposal.get("theme_evidence")
    if (session is None or not isinstance(previous, dict)
            or not isinstance(previous.get("run_id"), str)
            or theme not in THEME_NAMES or proposal.get("theme_scope") not in {None, "single"}
            or not isinstance(evidence, str) or not evidence.strip() or evidence not in question
            or not isinstance(condition, dict) or condition.get("type") not in {"floor", "difficulty"}
            or condition.get("name") is not None or condition.get("reference", "current") != "current"
            or condition.get("subject_quote") is not None):
        return None
    from arknights_agent.runtime.dialogue_context import load_previous

    audited = load_previous(session, previous["run_id"], release.id)
    if not services.same_json_value(audited, previous):
        return None
    state = audited.get("dialogue", {})
    if (audited.get("status") != "needs_clarification"
            or audited.get("manifest_hash") != release.manifest_hash
            or audited.get("claims") or audited.get("citations") or audited.get("records")
            or audited.get("candidates") or audited.get("named_relic_context")
            or state.get("theme_id") is not None or state.get("pending_conditions_theme_id") is not None
            or state.get("entity_native_id") is not None
            or state.get("interpretation_origin") != "model_query_proposal"
            or state.get("user_reported_conditions") != [] or state.get("unresolved_text")
            or state.get("unresolved_quotes")):
        return None
    quote = condition.get("quote")
    if not isinstance(quote, str) or not quote or quote not in audited["question"]:
        return None
    try:
        wanted = _proposal({"intent": "clarify", "conditions": [condition]})["conditions"][0]
        original = _proposal(state.get("model_interpretation"))
        pending = _pending_proposal(state.get("pending_user_reported_conditions"))
        submitted = _proposal({"intent": "clarify", "conditions": proposal.get("conditions", [])})
        # Recheck the original immediate question, not arbitrary old history.
        _conditions(original, [audited["question"]], [], None, pending=True)
        _conditions(pending, [audited["question"]], [], None, pending=True)
    except InvalidData:
        return None

    def identity(item):
        return {key: item.get(key, default) for key, default in (
            ("type", None), ("name", None), ("value", None), ("quote", None),
            ("reference", "current"), ("subject_quote", None))}

    def inventory(items):
        # A valid audit hash proves saved bytes, not that a legacy producer
        # retained every proposed restriction. Require complete typed inventory
        # equality, including multiplicity, before granting this new binding.
        return sorted(services.canonical_json(identity(c)) for c in items)

    wanted = identity(wanted)
    if (original["theme_id"] is not None or original["theme_evidence"] is not None
            or original["unresolved"]
            or inventory(original["conditions"]) != inventory(pending["conditions"])
            or any(c["type"] not in {"floor", "difficulty"} or c["name"] is not None
                   for c in pending["conditions"])
            or any(not any(services.same_json_value(identity(c), identity(new))
                           for new in submitted["conditions"]) for c in pending["conditions"])
            or not any(services.same_json_value(identity(c), wanted) for c in original["conditions"])
            or not any(services.same_json_value(identity(c), wanted) for c in pending["conditions"])):
        return None
    return {
        "protocol": "same-parent-pending-numeric-theme-clarification-v1",
        "source_run_id": audited["run_id"],
        "source_answer_response_hash": services.content_digest(audited),
        "condition_sha256": services.content_digest(wanted), "quote": quote,
        "theme_id": theme, "theme_evidence": evidence,
        "release_id": release.id, "manifest_hash": release.manifest_hash,
        "state_verification": "user_reported_only", "source_answer_mutated": False,
        "numeric_comparison_interpreted": False, "automatic_condition_transfer": False,
    }


def _prior_entity(previous, rows):
    if previous is None:
        return None
    if previous.get("candidates"):
        return None
    native = previous.get("dialogue", {}).get("entity_native_id")
    selected = [r for r in rows if r["native_id"] == native] if native else []
    if not selected:
        records = [r for r in previous.get("records", [])
                   if r.get("kind") not in {"roguelike_buff", "roguelike_wiki"}]
        if len(records) == 1:
            selected = [r for r in rows if r["native_id"] == records[0]["native_id"]]
    return selected[0] if len(selected) == 1 else None


def _prior_theme(previous, entity):
    if previous is None:
        return None
    themes = {value for value in (previous.get("theme_id"),
                                 previous.get("dialogue", {}).get("theme_id"),
                                 previous.get("version", {}).get("theme_id"),
                                 entity["parent_native_id"] if entity else None)
              if value is not None}
    if not themes:
        return None
    if len(themes) != 1 or not themes.issubset(THEME_NAMES):
        raise Conflict("authoritative previous theme identities disagree")
    return next(iter(themes))


def _historical_entity_evidence(session, release, question, proposal, previous, rows, store):
    """Prove one model-supplied history quote for an already bound object.

    This never selects from user prose. The selector/reference is unchanged;
    only literal provenance may come from the audited user's earlier turn.
    Selection and ending evidence retain their current-turn contracts. A
    separate complete-menu replay can license a same-bound-theme history quote.
    """
    quote = proposal["entity_evidence"]
    if (session is None or proposal["reference"] not in {"previous_entity", "previous_menu"}
            or previous is None or previous.get("status") != "answered"
            or not previous.get("claims") or not previous.get("citations")
            or previous.get("candidates") or not isinstance(previous.get("run_id"), str)):
        return None
    from arknights_agent.runtime.dialogue_context import load_previous

    audited = load_previous(session, previous["run_id"], release.id)
    if not services.same_json_value(audited, previous):
        return None
    history = _questions(question, audited)[:-1]
    indices = [index for index, user in enumerate(history) if quote in user]
    target = _prior_entity(audited, rows)
    records = [r for r in audited.get("records", [])
               if r.get("kind") not in {"roguelike_buff", "roguelike_wiki"}]
    if (not indices or target is None or len(records) != 1
            or audited.get("dialogue", {}).get("entity_native_id") != target["native_id"]
            or records[0].get("native_id") != target["native_id"]
            or proposal["theme_id"] not in {None, target["parent_native_id"]}
            or proposal["entity_name"] is not None
            and target not in _identity_rows(rows, proposal["entity_name"])):
        return None
    identities = _explicit_evidence_rows(rows, quote)
    if identities:
        if {row["native_id"] for row in identities} != {target["native_id"]}:
            return None
    else:
        # An opaque ID with a fabricated prefix/suffix cannot fall back to a
        # coincidental name. Name matching checks the already bound scope;
        # it does not infer a selector, a theme or the meaning of the quote.
        names = {row["native_id"] for row in rows
                 if row["parent_native_id"] == target["parent_native_id"]
                 and row["kind"] == target["kind"] and _literal(row["name"], quote)}
        if target["native_id"] in quote or names != {target["native_id"]}:
            return None
    probe = _Answer(session, release, question, store)
    probe.catalog_rows = {row["id"]: row for row in rows}
    record = probe.record(target["id"])
    if not services.same_json_value(record, records[0]):
        raise Conflict("historical entity evidence source record differs")
    probe.cite(record, "", record["data"])
    action = audited["dialogue"].get("intent")
    if action not in {"event_menu", "document", "relic_acquisition", "ending_route"}:
        return None
    fresh = answer_roguelike(session, release, _canonical(
        target["parent_native_id"], target, action), store=store)
    fresh_entity = _prior_entity(fresh, rows)
    if (fresh.get("status") != "answered" or not fresh.get("claims")
            or not fresh.get("citations") or fresh.get("candidates")
            or fresh.get("release_id") != release.id
            or fresh.get("manifest_hash") != release.manifest_hash
            or fresh_entity is None or fresh_entity["native_id"] != target["native_id"]):
        raise Conflict("historical entity evidence cannot replay its unique source")
    old_options = audited.get("event_menu", {}).get("options", [])
    if proposal["reference"] == "previous_menu" or old_options:
        options = fresh.get("event_menu", {}).get("options", [])
        if (target["kind"] != "roguelike_event" or not old_options or not options
                or any(not isinstance(option, dict) or "raw" not in option
                       for option in [*old_options, *options])
                or not services.same_json_value(old_options, options)):
            raise Conflict("historical entity evidence menu differs from its complete source")
    return {"protocol": "same-release-bound-entity-user-history-v1",
            "quote": quote, "source_question_indices": indices,
            "source_run_id": audited["run_id"],
            "source_answer_response_hash": services.content_digest(audited),
            "source_record_hash": services.content_digest(record),
            "entity_native_id": target["native_id"], "theme_id": target["parent_native_id"],
            "release_id": release.id, "manifest_hash": release.manifest_hash,
            "verification": "literal_user_history_and_same_bound_source",
            "proposal_mutated": False, "source_answer_mutated": False}


def _replayed_bound_menu_history(session, release, question, proposal, previous, rows, store):
    """Reopen an actual delivered menu before accepting redundant history quotes.

    A quote/index never creates this binding. The actual audited parent and
    its complete menu must independently match the fixed source first.
    """
    if (session is None or store is None or proposal["reference"] != "previous_menu"
            or proposal["intent"] != "event_menu" or not isinstance(previous, dict)
            or previous.get("status") != "answered" or not previous.get("claims")
            or not previous.get("citations") or previous.get("candidates")
            or not isinstance(previous.get("run_id"), str)):
        return None
    from arknights_agent.runtime.dialogue_context import load_previous

    audited = load_previous(session, previous["run_id"], release.id)
    if not services.same_json_value(audited, previous):
        raise Conflict("bound menu history parent differs from the server audit")
    target = _prior_entity(audited, rows)
    records = [r for r in audited.get("records", [])
               if r.get("kind") not in {"roguelike_buff", "roguelike_wiki"}]
    old_options = audited.get("event_menu", {}).get("options", [])
    if (target is None or target["kind"] != "roguelike_event" or len(records) != 1
            or audited.get("dialogue", {}).get("entity_native_id") != target["native_id"]
            or records[0].get("native_id") != target["native_id"]
            or proposal["theme_id"] not in {None, target["parent_native_id"]}
            or proposal["entity_name"] is not None
            and target not in _identity_rows(rows, proposal["entity_name"])
            or not isinstance(old_options, list) or not old_options):
        return None
    probe = _Answer(session, release, question, store)
    probe.catalog_rows = {row["id"]: row for row in rows}
    record = probe.record(target["id"])
    if not services.same_json_value(record, records[0]):
        raise Conflict("bound menu history source record differs")
    probe.cite(record, "", record["data"])
    fresh = answer_roguelike(session, release, _canonical(
        target["parent_native_id"], target, "event_menu"), store=store)
    options = fresh.get("event_menu", {}).get("options", [])
    fresh_entity = _prior_entity(fresh, rows)
    if (fresh.get("status") != "answered" or not fresh.get("claims")
            or not fresh.get("citations") or fresh.get("candidates")
            or fresh.get("release_id") != release.id
            or fresh.get("manifest_hash") != release.manifest_hash
            or fresh_entity is None or fresh_entity["native_id"] != target["native_id"]
            or not options or any(not isinstance(o, dict) or "raw" not in o
                                   for o in [*old_options, *options])
            or not services.same_json_value(old_options, options)):
        raise Conflict("bound menu history differs from its complete fixed source")
    return ({"protocol": "same-release-replayed-previous-menu-history-v1",
             "source_run_id": audited["run_id"],
             "source_answer_response_hash": services.content_digest(audited),
             "source_record_hash": services.content_digest(record),
             "source_menu_sha256": services.content_digest(options),
             "entity_native_id": target["native_id"], "theme_id": target["parent_native_id"],
             "release_id": release.id, "manifest_hash": release.manifest_hash,
             "source_record_replayed": True, "source_menu_replayed": True,
             "proposal_mutated": False, "source_answer_mutated": False},
            _questions(question, audited)[:-1])


def _indexed_entity_evidence(session, release, question, proposal, previous, *, bound_menu=None):
    """Check an explicit model-selected user quote, without binding its meaning.

    A failed task can still have authentic user history. Its attempted selector
    is not a source identity; the new model must choose and replay one below.
    """
    index = proposal.get("entity_evidence_question_index")
    quote = proposal["entity_evidence"]
    if index is None:
        return None
    previous_menu = proposal["reference"] == "previous_menu" and bound_menu is not None
    if ((proposal["reference"] not in {"current", "source_menu"} and not previous_menu)
            or quote is None or proposal["entity_name"] is None and not previous_menu):
        raise InvalidData("indexed entity evidence requires an explicit current source selector")
    if previous is None:
        if index != 0 or quote not in question:
            raise InvalidData("indexed entity quote is absent from the actual user question")
        return {"protocol": "indexed-user-source-selector-v1", "question_index": 0,
                "quote": quote, "source_run_id": None, "historical_quote": False,
                "release_id": release.id, "manifest_hash": release.manifest_hash,
                "verification": "literal_current_user_quote_only", "source_bound": False}
    if session is None or not isinstance(previous.get("run_id"), str):
        raise InvalidData("indexed history evidence requires the actual audited parent")
    from arknights_agent.runtime.dialogue_context import load_previous

    audited = load_previous(session, previous["run_id"], release.id, allow_general=True)
    if not services.same_json_value(audited, previous):
        raise Conflict("indexed entity evidence parent differs from the server audit")
    questions = _questions(question, audited)
    if not 0 <= index < len(questions) or quote not in questions[index]:
        raise InvalidData("indexed entity quote is absent from the actual indexed user turn")
    return {**(bound_menu[0] if previous_menu else {}),
            "protocol": ("indexed-bound-menu-user-history-v1" if previous_menu
                         else "indexed-user-source-selector-v1"), "question_index": index,
            "quote": quote, "source_run_id": audited["run_id"],
            "source_answer_response_hash": services.content_digest(audited),
            "historical_quote": index < len(questions) - 1,
            "release_id": release.id, "manifest_hash": release.manifest_hash,
            "verification": ("literal_indexed_history_and_replayed_bound_menu" if previous_menu
                             else "literal_indexed_user_history_only"),
            "source_bound": previous_menu}


def _historical_relic_evidence(session, release, question, proposal, previous, store):
    """Revalidate a model-selected earlier name against a real relic group.

    No words are interpreted here: the quote, selector and reference are all
    supplied by the model. A rejected parent supplies history, never a binding.
    """
    if (session is None or store is None or not isinstance(previous, dict)
            or not isinstance(previous.get("run_id"), str)):
        return None
    from arknights_agent.roguelike_relic_effects import _previous_group, _source_group
    from arknights_agent.runtime.dialogue_context import load_previous

    actual = load_previous(session, previous["run_id"], release.id, allow_general=True)
    if not services.same_json_value(actual, previous):
        raise Conflict("historical relic evidence differs from its audited parent")
    quote = proposal.get("entity_evidence")
    index = proposal.get("entity_evidence_question_index")
    history = _questions(question, actual)[:-1]
    if not isinstance(quote, str) or not quote:
        return None
    indices = [i for i, user in enumerate(history) if quote in user]
    if index is not None:
        if type(index) is not int or index not in indices:
            return None
        indices = [index]
    if not indices:
        return None
    source = _Answer(session, release, question, store)
    rows = source.catalog()
    recovered = False
    delivered_current = (
        proposal["reference"] == "current" and index is not None
        and isinstance(proposal.get("entity_name"), str)
        and actual.get("status") in {"answered", "needs_verification", "needs_clarification"}
    )
    if proposal["reference"] == "previous_entity" or delivered_current:
        name, group, targets = _previous_group(source, rows, actual)
        selector = proposal.get("entity_name")
        if selector is not None and not _identity_rows(group, selector):
            return None
    elif (proposal["reference"] == "current" and index is not None
          and actual.get("status") == "refused"):
        if not isinstance(proposal.get("entity_name"), str):
            return None
        selected = _identity_rows([r for r in rows if r["kind"] == "roguelike_relic"],
                                  proposal.get("entity_name"))
        names = {r["name"] for r in selected}
        if len(names) != 1:
            return None
        name = next(iter(names))
        group, targets = _source_group(source, rows, name)
        recovered = True
    else:
        return None
    # Reference and selector meaning belong to the model. The real audited
    # group (or explicit freshly replayed selector) supplies identity; do not
    # demand a canonical source name inside an otherwise genuine user quote.
    return {"protocol": "audited-historical-relic-group-evidence-v1",
            "source_run_id": actual["run_id"],
            "source_answer_response_hash": services.content_digest(actual),
            "source_question_indices": indices, "quote": quote, "entity_name": name,
            "source_group_targets_sha256": services.content_digest(targets),
            "complete_source_group_replayed": True, "recovered_after_failed_parent": recovered,
            "release_id": release.id, "manifest_hash": release.manifest_hash,
            "proposal_mutated": False, "source_answer_mutated": False}


def _relic_history_repair_context(session, release, question, proposal, previous):
    """Expose literal audited quote locations, never choose an interpretation."""
    if session is None or not isinstance(previous, dict) or not previous.get("run_id"):
        return None
    from arknights_agent.runtime.dialogue_context import load_previous

    actual = load_previous(session, previous["run_id"], release.id, allow_general=True)
    if not services.same_json_value(actual, previous):
        raise Conflict("historical relic repair parent differs from its audited parent")
    quote = proposal.get("entity_evidence")
    history = _questions(question, actual)[:-1]
    return {
        "source_run_id": actual["run_id"],
        "source_answer_response_hash": services.content_digest(actual),
        "parent_status": actual.get("status"),
        "matched_history_indices": [i for i, user in enumerate(history)
                                    if isinstance(quote, str) and quote and quote in user],
        "quote_locations_only": True,
        "selector_or_semantic_binding_verified": False,
        "proposal_mutated": False,
    }


def revalidate_failed_query_context(session, release, previous, store=None):
    """Expose genuine rejected attempts as data, never an answered antecedent."""
    if previous is None or previous.get("status") != "refused":
        return None
    from arknights_agent.runtime.dialogue_context import load_previous

    audited = load_previous(session, previous.get("run_id"), release.id, allow_general=True)
    if not services.same_json_value(audited, previous):
        raise Conflict("failed query context differs from the actual audited parent")
    runtime = previous.get("agent_runtime", {})
    attempts = runtime.get("rejected_query_attempts", [])
    if (not isinstance(attempts, list) or not attempts or len(attempts) > 8
            or runtime.get("server_committed") is not False
            or previous.get("claims") or previous.get("citations")):
        return None
    retained = copy.deepcopy(attempts)
    rows = None
    for attempt in retained:
        if (not isinstance(attempt, dict)
                or set(attempt) != {"tool", "submitted_arguments", "repair", "observation_only"}
                or attempt["tool"] not in {"roguelike_query", "roguelike_document", "source_query"}
                or attempt["observation_only"] is not True
                or not isinstance(attempt["submitted_arguments"], dict)
                or not isinstance(attempt["repair"], dict)):
            raise InvalidData("invalid rejected query observation")
        repair = attempt["repair"]
        targets = []
        if isinstance(repair.get("source_target"), dict):
            targets.append(repair["source_target"])
        lookup = repair.get("source_lookup")
        if lookup is not None:
            if (repair.get("code") == "unbound_relic_history_evidence"
                    or isinstance(lookup, dict) and "quote_locations_only" in lookup):
                from arknights_agent.runtime.rejected_query_context import (
                    verify_history_quote_lookup,
                )

                verify_history_quote_lookup(session, release, previous, attempt)
            elif (not isinstance(lookup, dict)
                    or not isinstance(lookup.get("source_candidates"), list)
                    or len(lookup["source_candidates"]) > 5
                    or lookup.get("selected_automatically") is not False):
                raise InvalidData("invalid rejected query source lookup")
            else:
                targets.extend(lookup["source_candidates"])
        for target in targets:
            if rows is None:
                rows = _Answer(session, release, previous["question"], store).catalog()
            matching = [r for r in rows if r["native_id"] == target.get("native_id")]
            if len(matching) != 1 or not services.same_json_value(_source_target(matching[0]), target):
                raise Conflict("rejected query source identity differs from current catalog")
            _replay_explicit_identity(session, release, previous["question"], store, matching[0], rows)
    return {"protocol": "same-release-rejected-query-context-v1",
            "parent_run_id": previous["run_id"],
            "parent_response_sha256": services.content_digest(previous),
            "release_id": release.id, "manifest_hash": release.manifest_hash,
            "original_question": previous["question"], "attempts": retained,
            "bound_source_object": False, "delivered_candidate_count": 0,
            "delivered_menu_option_count": 0, "observation_only": True,
            "game_facts_authorized": False, "source_candidates_revalidated": True}


def _source_target(row):
    """Source identity/type feedback only; never a proposed game answer."""
    return {"theme_id": row["parent_native_id"], "name": row["name"],
            "native_id": row["native_id"], "kind": row["kind"],
            "compatible_intents": list(_SOURCE_INTENTS.get(row["kind"], ["document", "clarify"]))}


def _candidate_rows(answer, previous, rows):
    """Revalidate the complete authoritative set before repeating or choosing."""
    candidates = previous.get("candidates", [])
    if (not isinstance(candidates, list) or not candidates
            or any(not isinstance(c, dict) or not isinstance(c.get("native_id"), str)
                   or not c["native_id"] for c in candidates)):
        raise InvalidData("invalid authoritative candidate identities")
    numbers = [c.get("number") for c in candidates]
    natives = [c.get("native_id") for c in candidates]
    if (not candidates or any(type(n) is not int for n in numbers)
            or sorted(numbers) != list(range(1, len(candidates) + 1))
            or len(set(natives)) != len(natives)):
        raise InvalidData("authoritative candidate set is not uniquely numbered")
    selected = [r for r in rows if r["native_id"] in natives]
    groups = {(r["parent_native_id"], r["kind"], r["name"]) for r in selected}
    if len(selected) != len(candidates) or len(groups) != 1:
        raise InvalidData("authoritative candidates differ from current source identities")
    theme, kind, name = next(iter(groups))
    complete = [r for r in rows if (r["parent_native_id"], r["kind"], r["name"]) == (theme, kind, name)]
    if kind != "roguelike_event" or {r["native_id"] for r in complete} != set(natives):
        raise InvalidData("authoritative candidate set is no longer complete")
    answer.verify_catalog(theme, "event", rows)
    return candidates, selected, theme, name


def _candidate_intent(proposal, previous):
    """Use typed actions, never words in a generated candidate question.

    The current model may change an event from menu to document or vice versa.
    Clarification alone does not change the prior action. New responses persist
    that action separately so several unselected clarifications cannot erase it.
    Older model responses can supply their original typed intent directly.
    """
    if proposal["intent"] in {"event_menu", "document"}:
        return proposal["intent"]
    if proposal["intent"] != "clarify":
        raise InvalidData("candidate query intent is incompatible with source events")
    state = previous.get("dialogue", {})
    if not isinstance(state, dict):
        raise InvalidData("invalid authoritative candidate action state")
    original = state.get("model_interpretation", {})
    original_action = original.get("intent") if isinstance(original, dict) else None
    action = state.get("candidate_followup_intent")
    if action is not None:
        if not isinstance(action, str) or action not in {"event_menu", "document"}:
            raise InvalidData("invalid authoritative candidate action")
        if (isinstance(original_action, str) and original_action in {"event_menu", "document"}
                and original_action != action):
            raise InvalidData("authoritative candidate actions disagree")
        return action
    action = original_action
    if not isinstance(action, str) or action not in {"event_menu", "document"}:
        raise InvalidData("authoritative candidates have no typed follow-up action")
    return action


def _repeat_candidates(answer, previous, rows, proposal):
    """Reissue the audited unselected set with its typed follow-up action.

    Repeating a question does not pick an item. The display numbers belong to
    the audited set, while names and descriptions are rebuilt from source rows.
    This function interprets no words from current or historical questions.
    """
    candidates, selected, theme, name = _candidate_rows(answer, previous, rows)
    action = _candidate_intent(proposal, previous)
    if proposal["theme_id"] not in {None, theme}:
        raise InvalidData("candidate query theme differs from authoritative source set")
    result = answer.refuse("尚未选择唯一场景，请从以下真实候选中明确选择。")
    result["theme_id"] = theme
    result["candidates"] = []
    lines = [f"找到 {len(candidates)} 个同名场景，请按描述选择："]
    for candidate in sorted(candidates, key=lambda c: c["number"]):
        row = next(r for r in selected if r["native_id"] == candidate["native_id"])
        record = answer.record(row["id"])
        citation = answer.cite(record, "", record["data"])
        description = record["data"].get("description") or ""
        question = _canonical(theme, row, action)
        result["candidates"].append({"number": candidate["number"], "name": row["name"],
                                     "native_id": row["native_id"], "description": description,
                                     "question": question, "citation_ids": [citation]})
        lines.extend([f"{candidate['number']}. {description}", "   查询：" + question])
    result["answer"] = "\n".join(lines)
    result["conditions"].update(candidate_count=len(candidates), candidate_list_complete=True,
                                guide_kind="disambiguation", route_readiness_verified=False)
    return result, theme, name, action


def _condition_references(answer, release, question, proposal, previous, rows):
    """Bind explicit typed condition subjects separately from the query target."""
    bindings = []
    # Only server-authored provenance from a hash-checked previous result may
    # continue pending conditions. It is not part of the model's input schema.
    prior_state = previous.get("dialogue", {}) if previous else {}
    prior_conditions = [*prior_state.get("user_reported_conditions", []),
                        *prior_state.get("pending_user_reported_conditions", [])]
    for binding in prior_state.get("condition_reference_bindings", []):
        if not isinstance(binding, dict):
            raise InvalidData("invalid authoritative condition reference")
        matches = [r for r in rows if r["native_id"] == binding.get("native_id")
                   and r["kind"] == "roguelike_relic"
                   and r["name"] == binding.get("name")
                   and r["parent_native_id"] == binding.get("theme_id")]
        if (binding.get("release_id") != release.id
                or binding.get("manifest_hash") != release.manifest_hash
                or binding.get("verification") != "same-release-unique-previous-relic-v1"
                or len(matches) != 1 or not binding.get("source_run_id")
                or not binding.get("source_response_hash")
                or not any(c.get("name") == binding.get("name")
                           and c.get("raw_text") == binding.get("quote") for c in prior_conditions)):
            raise InvalidData("condition reference provenance differs from authoritative state")
        fresh = answer.record(matches[0]["id"])
        if services.content_digest(fresh) != binding.get("source_record_hash"):
            raise InvalidData("condition reference source record changed")
        bindings.append(copy.deepcopy(binding))
    validated = copy.deepcopy(proposal)
    for c in validated["conditions"]:
        if c.get("reference", "current") != "previous_entity":
            continue
        subject = c["subject_quote"]
        if (c["quote"] not in question or c["quote"].count(subject) != 1
                or subject == c["quote"]):
            raise InvalidData("condition reference needs a unique literal subject span in the current quote")
        entity = _prior_entity(previous, rows)
        records = previous.get("records", []) if previous else []
        records = [r for r in records if r.get("kind") not in {"roguelike_buff", "roguelike_wiki"}]
        if (previous is None or previous.get("status") != "answered"
                or not previous.get("run_id") or previous.get("candidates")
                or not previous.get("claims") or not previous.get("citations")
                or entity is None or entity["kind"] != "roguelike_relic"
                or len(records) != 1 or records[0].get("native_id") != entity["native_id"]
                or previous.get("dialogue", {}).get("entity_native_id") != entity["native_id"]
                or not any(cite.get("record_id") == entity["id"] for cite in previous["citations"])):
            raise InvalidData("condition reference requires a real uniquely source-verified previous relic")
        if (c["name"] is not None and c["name"] not in _name_forms(entity["name"])
                or proposal["theme_id"] not in {None, entity["parent_native_id"]}):
            raise InvalidData("condition name or theme differs from the verified previous relic")
        fresh = answer.record(entity["id"])
        if services.content_digest(fresh) != services.content_digest(records[0]):
            raise InvalidData("previous condition source identity differs from the current pinned record")
        c["name"] = entity["name"]
        binding = {
            "name": entity["name"], "native_id": entity["native_id"],
            "theme_id": entity["parent_native_id"], "quote": c["quote"],
            "subject_quote": subject, "source_run_id": previous["run_id"],
            "source_response_hash": services.content_digest(previous),
            "source_record_hash": services.content_digest(fresh),
            "release_id": release.id, "manifest_hash": release.manifest_hash,
            "verification": "same-release-unique-previous-relic-v1",
            "state_verification": "user_reported_only",
        }
        bindings = [b for b in bindings if (b["name"], b["quote"]) != (binding["name"], binding["quote"])]
        bindings.append(binding)
    return validated, bindings


def _revalidate_unbound_target(session, release, previous, rows, store):
    """Recover a source-unique literal goal from an audited rejected turn.

    A failed prior query is not an already bound object. Replaying its complete
    original proposal without prior context must independently reach the new
    source-kind gate. Candidate menus, ambiguous names, unknown goals and
    conditions cannot supply the target. No previous answer is mutated.
    """
    if previous is None or previous.get("status") != "needs_clarification":
        return None
    state = previous.get("dialogue", {})
    if (previous.get("candidates") or previous.get("records") or previous.get("claims")
            or previous.get("citations") or state.get("entity_native_id")
            or state.get("interpretation_origin") != "model_query_proposal"
            or not isinstance(previous.get("run_id"), str)
            or not isinstance(state.get("model_interpretation"), dict)):
        return None
    old = state["model_interpretation"]
    if old.get("reference", "current") != "current" or not old.get("entity_name"):
        return None
    replay = answer_roguelike_interpretation(
        session, release, previous["question"], old, store=store)
    replay_state = replay.get("dialogue", {})
    conflict = replay_state.get("proposal_repair_request", {})
    if conflict.get("code") == "source_kind_mismatch" and not replay.get("claims") and not replay.get("citations"):
        identity = conflict.get("source_target", {})
        protocol = "same-release-source-kind-conflict-target-replay-v1"
    else:
        # A removed language guard is not itself evidence. The original literal
        # target must now independently resolve, cite its complete pinned record
        # and pass the ordinary source verifier without prior context.
        entity = _prior_entity(replay, rows)
        primary = [r for r in replay.get("records", [])
                   if r.get("kind") not in {"roguelike_buff", "roguelike_wiki"}]
        if (entity is None or replay.get("status") not in {"answered", "unsupported"}
                or replay.get("candidates") or not replay.get("claims") or not replay.get("citations")
                or len(primary) != 1 or primary[0].get("native_id") != entity["native_id"]
                or replay_state.get("entity_native_id") != entity["native_id"]):
            return None
        identity = _source_target(entity)
        protocol = "same-release-source-unique-unbound-target-replay-v2"
    found = [row for row in rows if row["native_id"] == identity.get("native_id")]
    if len(found) != 1 or _source_target(found[0]) != identity:
        return None
    return found[0], {
        "protocol": protocol,
        "source_run_id": previous["run_id"],
        "source_answer_response_hash": services.content_digest(previous),
        "release_id": release.id, "manifest_hash": release.manifest_hash,
        "source_target": identity, "replay_depth": 1,
        "source_answer_mutated": False,
    }


def revalidate_legacy_pending_conditions(session, release, previous_answer, store=None):
    """Derive pending state once from an audited old result, without editing it.

    Runtime callers must obtain previous_answer through load_previous. Legacy
    proposals get no special trust: replay the complete old question with no
    previous context, then retain only newly validated pending user state.
    """
    previous = _prior(previous_answer, release)
    if previous is None:
        return None
    dialogue = previous.get("dialogue", {})
    if (not isinstance(dialogue, dict) or "pending_user_reported_conditions" in dialogue
            or dialogue.get("user_reported_conditions") != []
            or dialogue.get("interpretation_origin") != "model_query_proposal"
            or previous.get("status") != "needs_clarification"
            or not isinstance(previous.get("run_id"), str)
            or not isinstance(dialogue.get("model_interpretation"), dict)):
        return None
    replayed = answer_roguelike_interpretation(
        session, release, previous["question"], dialogue["model_interpretation"], store=store)
    state = replayed.get("dialogue", {})
    pending = state.get("pending_user_reported_conditions", [])
    if (not pending or replayed.get("status") != "needs_clarification"
            or replayed.get("claims") or replayed.get("citations")):
        return None
    return {
        "pending_user_reported_conditions": copy.deepcopy(pending),
        "pending_conditions_theme_id": state.get("pending_conditions_theme_id"),
        "pending_state_derivation": {
            "protocol": "same-release-legacy-user-state-replay-v1",
            "source_run_id": previous["run_id"],
            "source_answer_response_hash": services.content_digest(previous),
            "release_id": release.id,
            "manifest_hash": release.manifest_hash,
            "replay_depth": 1,
            "scope": "pending_user_state_only",
            "source_answer_mutated": False,
        },
    }


def answer_roguelike_interpretation(session, release, original_question, interpretation,
                                  store=None, previous_answer=None):
    """Validate model-owned selectors and literal provenance against pinned sources."""
    if not isinstance(original_question, str) or not original_question.strip() or len(original_question) > 2000:
        raise InvalidData("question must contain 1 to 2000 characters")
    if isinstance(previous_answer, dict) and previous_answer.get("module") in {
        "operator", "recruitment", "garrison", "auto",
    }:
        # A verified general conversation may switch modules. Preserve its
        # actual question history, but none of its module-specific objects,
        # menus or conditions can become a roguelike antecedent.
        if (previous_answer.get("release_id") != release.id
                or previous_answer.get("manifest_hash") != release.manifest_hash):
            raise InvalidData("previous answer must belong to the same pinned release")
        previous = {key: copy.deepcopy(previous_answer[key]) for key in (
            "question", "release_id", "manifest_hash", "module", "dialogue_state",
        ) if key in previous_answer}
    else:
        previous = _prior(previous_answer, release)
    answer = _Answer(session, release, original_question, store)
    parsed = {"intent": None, "theme_id": None, "entity_name": None, "entity_native_id": None,
              "canonical_question": None, "context_used": False, "user_reported_conditions": [],
              "pending_user_reported_conditions": [], "pending_conditions_theme_id": None,
              "unresolved_text": "", "state_observation": "user_reported_only",
              "interpretation_origin": "model_query_proposal"}

    def decorate(result):
        # A source-verified candidate list has no selected object. The first
        # catalog match is only a lookup seed, never an authoritative target.
        if result.get("candidates"):
            parsed["entity_native_id"] = None
            if parsed["intent"] in {"event_menu", "document"}:
                parsed["candidate_followup_intent"] = parsed["intent"]
        if "condition_reference_bindings" in parsed:
            retained = [*parsed["user_reported_conditions"], *parsed["pending_user_reported_conditions"]]
            parsed["condition_reference_bindings"] = [
                b for b in parsed["condition_reference_bindings"]
                if any(c.get("name") == b["name"] and c.get("raw_text") == b["quote"] for c in retained)]
            if not parsed["condition_reference_bindings"]:
                parsed.pop("condition_reference_bindings")
        result = _decorate(result, original_question, parsed)
        result["conditions"]["pending_user_reported_conditions"] = copy.deepcopy(
            parsed["pending_user_reported_conditions"])
        result["conditions"]["pending_conditions_theme_id"] = parsed["pending_conditions_theme_id"]
        return result

    def refuse(message, status="needs_clarification"):
        return decorate(answer.refuse(message, status))

    def repair(code, detail, source_target=None, *, fields=None, source_lookup=None):
        parsed["proposal_repair_request"] = {"code": code, "detail": detail}
        if fields is not None:
            parsed["proposal_repair_request"]["fields"] = fields
        if source_target is not None:
            parsed["proposal_repair_request"]["source_target"] = _source_target(source_target)
        if source_lookup is not None:
            parsed["proposal_repair_request"]["source_lookup"] = source_lookup
        return refuse(detail)

    try:
        proposal = _proposal(interpretation)
        parsed.update(intent=proposal["intent"], model_interpretation=proposal,
                      original_user_questions=_questions(original_question, previous))
        if not answer.entry or store is None:
            return refuse("本次发布或快照存储不能核验来源，不能根据模型提议补写事实。", "needs_verification")
        # Language meaning and module/intent selection belong to the model.
        # This boundary validates structured parameters and their provenance.
        questions = parsed["original_user_questions"]
        if any(not any(q in user for user in questions) for q in proposal["unresolved"]):
            return repair("nonliteral_unresolved", "unresolved 只能使用原问或用户历史的逐字引文，不能填入解释、追问措辞或模型的备注。")
        if proposal["reference"] == "source_menu" and proposal["intent"] != "event_menu":
            return repair("source_menu_intent_mismatch", "source_menu仅用于重新核验单个明确事件的来源菜单；请按本轮所需信息自行修正intent或reference。", fields=["intent", "reference"])
        if proposal["intent"] == "relic_effect":
            fields = [key for key in ("selection_number", "selection_evidence", "ending_number")
                      if proposal[key] is not None]
            if fields:
                return repair("inapplicable_effect_fields", "藏品效果不接受按钮或结局序号；请自行复核并更正冲突字段，保留对象和真实条件。", fields=fields)
            historical = None
            current_evidence = None
            index = proposal.get("entity_evidence_question_index")
            if (index == len(questions) - 1 and proposal["reference"] == "current"
                    and proposal["entity_evidence"] is not None
                    and proposal["entity_evidence"] in original_question):
                current_evidence = _indexed_entity_evidence(
                    session, release, original_question, proposal, previous_answer)
            elif (index is not None
                    or proposal["entity_evidence"] is not None
                    and proposal["entity_evidence"] not in original_question):
                historical = _historical_relic_evidence(
                    session, release, original_question, proposal, previous_answer, store)
                if historical is None:
                    return repair(
                        "unbound_relic_history_evidence",
                        "历史对象引文须属于真实父链。已交付前轮可用previous_entity，或current配合"
                        "同组来源名和明确历史引文索引；失败前轮重查须用current、来源名及索引。"
                        "source_lookup只给出逐字引文所在轮次，不证明对象含义；请模型自行修正提案。",
                        fields=["reference", "entity_name", "entity_evidence", "entity_evidence_question_index"],
                        source_lookup=_relic_history_repair_context(
                            session, release, original_question, proposal, previous_answer),
                    )
            for field in ("entity_evidence", "theme_evidence", "ending_evidence"):
                if proposal[field] is not None and proposal[field] not in original_question:
                    if field == "entity_evidence" and historical is not None:
                        continue
                    return repair("nonliteral_" + field, field + " 必须是本轮用户逐字引文。")
            from arknights_agent.roguelike_relic_effects import answer_relic_effects

            result = answer_relic_effects(
                session, release, original_question, proposal, store=store,
                previous_answer=(previous_answer if historical is not None else previous),
                provenance_parent=previous_answer,
            )
            if historical is not None:
                result.setdefault("dialogue", {})["entity_evidence_binding"] = historical
            if current_evidence is not None:
                result.setdefault("dialogue", {})["indexed_current_entity_evidence"] = current_evidence
            return result
        parsed["unresolved_text"] = "；".join(proposal["unresolved"])
        parsed["unresolved_quotes"] = list(proposal["unresolved"])
        rows = answer.catalog()
        objects = [r for r in rows if r["kind"] not in {"roguelike_theme", "roguelike_wiki", "roguelike_buff"}]
        condition_proposal, bindings = _condition_references(
            answer, release, original_question, proposal, previous, objects)
        if bindings:
            parsed["condition_reference_bindings"] = bindings
            parsed["context_used"] = True
        kind = {"relic_acquisition": "roguelike_relic", "unknown_probability": "roguelike_relic",
                "ending_route": "roguelike_ending", "event_menu": "roguelike_event"}.get(proposal["intent"])
        theme, name = proposal["theme_id"], proposal["entity_name"]
        reference = proposal["reference"]
        bound_menu = None
        if reference == "previous_menu" and (
                proposal.get("entity_evidence_question_index") is not None
                or proposal["theme_evidence"] is not None
                and proposal["theme_evidence"] not in original_question):
            bound_menu = _replayed_bound_menu_history(
                session, release, original_question, proposal, previous_answer, objects, store)
        indexed_evidence = _indexed_entity_evidence(
            session, release, original_question, proposal, previous_answer,
            bound_menu=bound_menu,
        )
        prior_entity = _prior_entity(previous, objects)
        if prior_entity is None and reference == "previous_entity":
            recovered = _revalidate_unbound_target(session, release, previous, objects, store)
            if recovered is not None:
                prior_entity, parsed["previous_target_derivation"] = recovered
        prior_theme = _prior_theme(previous, prior_entity)
        prior_state = previous.get("dialogue", {}) if previous else {}
        legacy_pending = (revalidate_legacy_pending_conditions(session, release, previous, store)
                          if previous is not None and previous.get("module") == "roguelike"
                          else None)
        if legacy_pending is not None:
            prior_state = {**prior_state, **legacy_pending}
            parsed["previous_pending_state_derivation"] = legacy_pending["pending_state_derivation"]
        target, matches, global_matches = None, [], []
        condition_anchor = None
        # Preserve the model's typed user state before resolving an object or
        # theme. Pending values prove only literal user quotes, never ownership,
        # source facts, route readiness, or a theme to use for answering.
        scope_matches = _identity_rows(objects, name) if reference in {"current", "source_menu"} and name else []
        old_scope = prior_state.get("pending_conditions_theme_id")
        if old_scope is not None and (not isinstance(old_scope, str) or old_scope not in THEME_NAMES):
            raise InvalidData("invalid authoritative pending theme scope")
        known_scope = prior_theme or old_scope
        scope_owners = {r["parent_native_id"] for r in scope_matches}
        for field in ("entity_evidence", "theme_evidence", "ending_evidence"):
            quote = proposal[field]
            if quote is not None and quote not in original_question:
                if (field == "theme_evidence" and bound_menu is not None
                        and theme == bound_menu[0]["theme_id"]):
                    indices = [i for i, user in enumerate(bound_menu[1]) if quote in user]
                    if indices:
                        parsed["theme_evidence_binding"] = {**bound_menu[0], "quote": quote,
                            "source_question_indices": indices,
                            "verification": "literal_user_history_and_same_replayed_menu_theme"}
                        parsed["context_used"] = True
                        continue
                if field == "entity_evidence":
                    if indexed_evidence is not None:
                        continue
                    binding = _historical_entity_evidence(
                        session, release, original_question, proposal, previous, objects, store)
                    if binding is not None:
                        parsed["entity_evidence_binding"] = binding
                        parsed["context_used"] = True
                        continue
                return repair("nonliteral_" + field, field + " 必须是本轮用户逐字引文。")
        source_scope = (next(iter(scope_owners))
                        if len(scope_owners) == 1 and (name and _literal(name, original_question)
                                                       or proposal["entity_evidence"] is not None) else None)
        evidenced_theme = theme if proposal["theme_evidence"] is not None else None
        effective_scope = source_scope or evidenced_theme
        changing_scope = known_scope is not None and (
            effective_scope not in {None, known_scope}
            or theme not in {None, known_scope})
        current_pending = _conditions(condition_proposal, [original_question] if changing_scope else questions,
                                      objects, None, pending=True, bindings=bindings)
        # An ungrounded model theme cannot erase an already audited scope.
        # Otherwise a rejected proposal could turn current state into unscoped
        # pending and carry it into another theme on the following turn.
        pending_scope = (effective_scope if effective_scope is not None else
                         known_scope)
        old_pending = []
        if previous is not None:
            if ((effective_scope is not None and old_scope in {None, effective_scope}
                 or effective_scope is None and old_scope in {None, pending_scope})
                    and (old_scope is None or theme in {None, old_scope})):
                old_pending = _conditions(_pending_proposal(prior_state.get(
                    "pending_user_reported_conditions", [])), questions[:-1], objects, None, pending=True,
                    bindings=bindings)
                if pending_scope is None:
                    pending_scope = old_scope
        parsed["pending_user_reported_conditions"] = _merge_conditions(old_pending, current_pending)
        parsed["pending_conditions_theme_id"] = (pending_scope
                                                 if parsed["pending_user_reported_conditions"] else None)
        if reference in {"current", "source_menu"} and proposal["intent"] == "event_menu":
            explicit = _explicit_evidence_rows(objects, proposal["entity_evidence"])
            if len(explicit) == 1:
                identity = explicit[0]
                _replay_explicit_identity(session, release, original_question, store, identity, rows)
                selected_scope = [r for r in scope_matches
                                  if theme is None or r["parent_native_id"] == theme]
                # This feedback repairs menu object selectors only. Other
                # intents retain their existing source and ordinal binding.
                kind_conflict = identity["kind"] != "roguelike_event"
                theme_conflict = theme is not None and theme != identity["parent_native_id"]
                if (len(selected_scope) != 1
                        or selected_scope[0]["native_id"] != identity["native_id"]
                        or kind_conflict or theme_conflict):
                    fields = ["entity_name", "entity_evidence"]
                    if theme_conflict:
                        fields.append("theme_id")
                    if kind_conflict:
                        fields.append("intent")
                    return repair(
                        "explicit_identity_selector_mismatch",
                        "已提交的逐字entity_evidence含一个完整来源ID，但当前entity_name/theme/intent范围"
                        "未唯一对应该身份。请复核原问并显式更正selector或定位引文；不要只把ID留在引文中"
                        "却查询通名，也不能删除其他真实条件。服务器不会替你改ID、选择候选或生成事实。",
                        identity, fields=fields,
                    )
        if reference in {"current", "source_menu"}:
            if name is not None:
                if not _literal(name, original_question) and proposal["entity_evidence"] is None:
                    return repair("missing_entity_evidence", "entity_name 应为完整来源名称或 ID；原问使用别称时须提供本轮逐字 entity_evidence，前轮对象使用真实 reference。")
                global_matches = _identity_rows(objects, name)
                matches = [r for r in global_matches if not kind or r["kind"] == kind]
                if (proposal["intent"] == "ending_route" and proposal["ending_number"] is not None
                        and len(global_matches) == 1 and global_matches[0]["kind"] == "roguelike_relic"):
                    condition_anchor = global_matches[0]
                elif not global_matches:
                    lookup = _literal_source_candidates(
                        session, release, original_question, store, objects, name, theme, kind,
                    )
                    return repair(
                        "unknown_source_identity",
                        "当前entity_name没有精确来源身份。source_lookup是按你已提交的字面选择器检索并重放原件的候选元数据，"
                        "不是已选对象或事实答案；请结合原问核对并由你重新提交真实名称及逐字定位依据，保留全部有效条件。"
                        "不能自动取首个或把类别词机械拼进名称。候选不符时用query_records继续定位，"
                        "不能把查库工作推给用户索要内部ID。结局编号和主题仍各填自己的字段。",
                        fields=["entity_name", "entity_evidence"], source_lookup=lookup,
                    )
                if theme:
                    matches = [r for r in matches if r["parent_native_id"] == theme]
        elif reference == "previous_entity":
            if prior_entity is None:
                if previous is not None and previous.get("candidates"):
                    repeated, repeated_theme, repeated_name, action = _repeat_candidates(
                        answer, previous, rows, proposal)
                    parsed.update(theme_id=repeated_theme, entity_name=repeated_name,
                                  context_used=True, candidate_followup_intent=action)
                    return decorate(repeated)
                return refuse("前轮未绑定唯一已核验对象，请明确完整名称。")
            target = prior_entity
            parsed["context_used"] = True
        else:
            number = proposal["selection_number"]
            # The model interprets the choice; the server binds its typed index
            # to the actual audited list and replays the source below.
            if previous is None or number is None:
                # A null typed selection is still unselected even when the
                # model retains the requested document/menu intent. Requiring
                # a special intent label here loses the real list on an
                # otherwise valid ambiguous follow-up.
                if previous is not None and previous.get("candidates"):
                    repeated, repeated_theme, repeated_name, action = _repeat_candidates(
                        answer, previous, rows, proposal)
                    parsed.update(theme_id=repeated_theme, entity_name=repeated_name,
                                  context_used=True, candidate_followup_intent=action)
                    return decorate(repeated)
                old_menu = previous.get("event_menu", {}) if previous else {}
                old_options = old_menu.get("options", []) if isinstance(old_menu, dict) else []
                if (reference == "previous_menu" and number is None
                        and proposal["intent"] == "event_menu" and prior_entity is not None
                        and prior_entity["kind"] == "roguelike_event"
                        and previous.get("status") == "answered"
                        and previous.get("claims") and previous.get("citations")
                        and isinstance(old_options, list) and old_options):
                    # This typed reference asks to bind a menu choice but has
                    # no choice index. Recheck the actual menu before offering
                    # a repair; no words in the user's question select an item.
                    if theme not in {None, prior_entity["parent_native_id"]}:
                        return repair("reference_theme_mismatch", "提议主题与真实前轮对象不一致；请按当前原问重新提交引用范围。")
                    if name is not None and prior_entity not in _identity_rows(objects, name):
                        return repair("reference_entity_mismatch", "entity_name 与真实前轮事件不同；请重新核对对象引用。")
                    fresh = answer_roguelike(session, release, _canonical(
                        prior_entity["parent_native_id"], prior_entity, "event_menu"), store=store)
                    options = fresh.get("event_menu", {}).get("options", [])
                    if (not options or any(not isinstance(o, dict) or "raw" not in o
                                           for o in [*old_options, *options])
                            or [o["raw"] for o in old_options] != [o["raw"] for o in options]):
                        return refuse("前轮菜单和重新核验的原文不同，本次不借用旧按钮。", "needs_verification")
                    parsed.update(theme_id=prior_entity["parent_native_id"], entity_name=prior_entity["name"],
                                  entity_native_id=prior_entity["native_id"], context_used=True)
                    parsed["proposal_repair_request"] = {
                        "code": "missing_menu_selection",
                        "fields": ["selection_number", "selection_evidence", "reference"],
                        "detail": "previous_menu 只能选择真实前轮菜单成员，但本提案缺少 selection_number。"
                                  "请重新理解本轮原问：明确选择时提交编号及本轮逐字 selection_evidence；"
                                  "查看整个已绑定事件的菜单用 previous_entity，不选择按钮；"
                                  "尚未明确选择或存在未知条件时用 clarify 并保留原话。不能默认第一项或删除条件。",
                        "source_target": _source_target(prior_entity),
                    }
                    return refuse(parsed["proposal_repair_request"]["detail"])
                return refuse("选择序号必须绑定真实前轮候选或菜单。")
            if reference == "previous_candidate":
                if not previous.get("candidates"):
                    result = repair(
                        "reference_list_unavailable",
                        "真实前轮没有已交付的候选列表，不能将按钮编号当作候选编号。"
                        "请保留本轮合法selection_number、逐字selection_evidence和全部条件，"
                        "自主查明事件后使用source_menu核验当前来源菜单；历史对象原话可通过"
                        "entity_evidence_question_index明确引用。服务器不选择候选或替换对象。",
                        fields=["reference", "entity_name", "entity_evidence", "entity_evidence_question_index"],
                    )
                    result["dialogue"]["proposal_repair_request"]["actual_previous_state"] = {
                        "candidate_count": 0,
                        "menu_option_count": len(previous.get("event_menu", {}).get("options", [])),
                        "bound_source_object": prior_entity is not None,
                    }
                    return result
                candidates, _, _, _ = _candidate_rows(answer, previous, rows)
                actual = [c for c in candidates if c.get("number") == number]
                if len(actual) != 1:
                    return refuse("该序号不在前轮实际显示的候选中。")
            elif (prior_entity is None or not 1 <= number <= len(
                    previous.get("event_menu", {}).get("options", []))):
                if prior_entity is not None:
                    parsed.update(theme_id=prior_entity["parent_native_id"], entity_name=prior_entity["name"],
                                  entity_native_id=prior_entity["native_id"])
                return refuse("该序号不在前轮当前场景的实际菜单中，不能从后续分支借按钮。")
            evidence = proposal["selection_evidence"]
            if evidence is not None and evidence not in original_question:
                return repair("nonliteral_selection_evidence", "selection_evidence 必须是本轮用户逐字选择依据，不能引用历史、来源描述或模型解释。")
            if evidence is None:
                parsed["proposal_repair_request"] = {
                    "code": "missing_selection_evidence",
                    "detail": "选择编号缺少本轮依据。只有用户明确选择真实列表成员时才提交编号和逐字 selection_evidence；普通继续或无法区分多个候选时用 clarify，selection_number=null。不能默认第一个。",
                }
                if previous.get("candidates"):
                    repeated, repeated_theme, repeated_name, action = _repeat_candidates(
                        answer, previous, rows, proposal)
                    parsed.update(theme_id=repeated_theme, entity_name=repeated_name,
                                  context_used=True, candidate_followup_intent=action)
                    return decorate(repeated)
                return refuse(parsed["proposal_repair_request"]["detail"])
            parsed["selection_grounding"] = {
                "quote": evidence, "number": number, "reference": reference,
                "verification": "literal_current_user_quote",
                "interpretation_status": "model_proposed", "source_list": "actual_server_previous_answer",
            }
            parsed["context_used"] = True
            if reference == "previous_candidate":
                candidates = [c for c in previous.get("candidates", []) if c.get("number") == number]
                if len(candidates) != 1:
                    return refuse("该序号不在前轮实际显示的候选中。")
                found = [r for r in objects if r["native_id"] == candidates[0]["native_id"]]
                target = found[0] if len(found) == 1 else None
            else:
                target = prior_entity
                menu = previous.get("event_menu", {}).get("options", [])
                if target is None or not 1 <= number <= len(menu) or proposal["intent"] != "event_menu":
                    if target is not None:
                        parsed.update(theme_id=target["parent_native_id"], entity_name=target["name"],
                                      entity_native_id=target["native_id"])
                    return refuse("该序号不在前轮当前场景的实际菜单中，不能从后续分支借按钮。")
        if target is not None:
            if theme and theme != target["parent_native_id"]:
                return repair("reference_theme_mismatch", "提议主题与真实前轮对象不一致；更换主题时请使用 current 并保留当前原问的主题依据。")
            if name and target not in _identity_rows(objects, name):
                anchored = _identity_rows(objects, name)
                if (proposal["intent"] == "ending_route" and target["kind"] == "roguelike_ending"
                        and len(anchored) == 1 and anchored[0]["kind"] == "roguelike_relic"):
                    condition_anchor = anchored[0]
                else:
                    return repair("reference_entity_mismatch", "entity_name 与真实前轮对象不同；不要将条件藏品、主题或候选名称替换前轮查询对象。")
            if kind and target["kind"] != kind:
                return repair("reference_kind_mismatch", "查询意图与重新核验的前轮对象类型不一致，请按来源类型修正意图，保留真实目标和用户条件；不能将条件藏品替换为查询目标。", target)
            theme = target["parent_native_id"]
        if (proposal["intent"] == "event_menu" and reference == "previous_entity"
                and proposal["selection_number"] is not None and target is not None):
            old_options = previous.get("event_menu", {}).get("options", [])
            old_records = [r for r in previous.get("records", [])
                           if r.get("kind") not in {"roguelike_buff", "roguelike_wiki"}]
            if (previous.get("status") == "answered" and previous.get("claims")
                    and previous.get("citations") and not previous.get("candidates")
                    and len(old_records) == 1 and old_records[0].get("native_id") == target["native_id"]
                    and target["kind"] == "roguelike_event"
                    and isinstance(old_options, list) and old_options):
                number, quote = proposal["selection_number"], proposal["selection_evidence"]
                if not 1 <= number <= len(old_options):
                    return refuse("该序号不在前轮当前场景的实际菜单中，不能从后续分支借按钮。")
                if quote is None:
                    return repair("missing_selection_evidence", "选择编号缺少本轮逐字依据；请保留编号和真实条件，复核当前用户是否选择真实菜单成员。", fields=["selection_evidence"])
                if quote not in original_question:
                    return repair("nonliteral_selection_evidence", "selection_evidence 必须是本轮用户逐字选择依据，不能引用历史、来源描述或模型解释。")
                fresh_record = answer.record(target["id"])
                if not services.same_json_value(fresh_record, old_records[0]):
                    return refuse("前轮事件和当前固定来源不同，本次不借用旧菜单。", "needs_verification")
                fresh = answer_roguelike(session, release, _canonical(
                    target["parent_native_id"], target, "event_menu"), store=store)
                options = fresh.get("event_menu", {}).get("options", [])
                fresh_entity = _prior_entity(fresh, objects)
                if (fresh.get("status") != "answered" or not fresh.get("claims")
                        or not fresh.get("citations") or fresh.get("candidates")
                        or fresh_entity is None or fresh_entity["native_id"] != target["native_id"]
                        or fresh.get("release_id") != release.id or fresh.get("manifest_hash") != release.manifest_hash
                        or not options or any(not isinstance(o, dict) or "raw" not in o
                                               for o in [*old_options, *options])
                        or not services.same_json_value(old_options, options)):
                    return refuse("前轮菜单和重新核验的原文不同，本次不借用旧按钮。", "needs_verification")
                parsed.update(theme_id=target["parent_native_id"], entity_name=target["name"],
                              entity_native_id=target["native_id"], context_used=True)
                return repair(
                    "selection_reference_mismatch",
                    "已给出的编号和逐字依据指向真实前轮菜单，但reference=previous_entity只用于"
                    "重看整份对象。选择前轮按钮须由模型显式改为previous_menu；保留有效编号、"
                    "逐字依据和全部条件，不得改选第一项、删条件或由服务器代填引用。",
                    target, fields=["reference"],
                )
        if theme is None:
            inferred = {r["parent_native_id"] for r in global_matches}
            if len(inferred) == 1:
                theme = next(iter(inferred))
            elif previous is not None and proposal["ending_number"] is not None:
                theme = prior_theme
                parsed["context_used"] = bool(theme)
        # A source-owned object, actual previous context, or a model-owned
        # literal theme evidence span grounds scope. Do not reparse its meaning.
        owners = {r["parent_native_id"] for r in global_matches}
        if (theme is not None and theme != prior_theme and owners != {theme}
                and not (target and parsed["context_used"]) and proposal["theme_evidence"] is None):
            return repair("missing_theme_evidence", "theme_id 缺少当前来源唯一对象、真实前轮或本轮逐字 theme_evidence；请补充参数，不能猜测主题。")
        parsed["theme_id"] = theme
        if theme is None:
            return refuse("请明确肉鸽主题，不能靠模型猜测同名条目或结局序号。")
        # Recheck pending state against this grounded theme. A current typed
        # correction replaces the old value before source validation; changing
        # an already scoped theme never carries its old pending state across.
        if pending_scope not in {None, theme} or prior_theme not in {None, theme}:
            old_pending = []
        if prior_theme not in {None, theme}:
            # Historical quotes describe the previous run/theme. A new theme
            # requires a fresh user assertion, even if the model re-proposes an
            # old quoted value instead of relying on automatic inheritance.
            current_pending = _conditions(condition_proposal, [original_question], objects, None, pending=True,
                                          bindings=bindings)
        pending = _merge_conditions(old_pending, current_pending)
        conditions = _conditions(_pending_proposal(pending), questions, objects, theme, bindings=bindings)
        if previous is not None and theme == prior_theme:
            old = previous.get("dialogue", {}).get("user_reported_conditions", [])
            conditions = _merge_conditions(old, conditions)
        parsed["user_reported_conditions"] = conditions
        parsed["pending_user_reported_conditions"] = []
        parsed["pending_conditions_theme_id"] = None
        parsed["context_used"] = parsed["context_used"] or bool(old_pending)
        if (reference in {"current", "source_menu"} and name is None and proposal["ending_number"] is None
                and proposal["intent"] != "clarify"):
            return repair(
                "missing_query_target",
                "查询提议没有对象或结局编号，请重新检查原问中的明确目标；若原问确实缺少目标，"
                "请使用 clarify 意图。保留已核验的用户条件，不能猜测对象。",
            )
        if proposal["ending_number"] is not None:
            if proposal["intent"] != "ending_route" or not theme:
                return refuse("结局序号未由原问及明确主题支持。")
            ordinal = _source_ending_ordinal(answer, rows, theme, proposal["ending_number"])
            if ordinal is None:
                return refuse("该发布不能唯一核验此正常结局编号及排序。", "needs_verification")
            bound_repeat = (reference == "previous_entity" and target is not None
                            and target["id"] == ordinal["id"] and target["kind"] == "roguelike_ending")
            bound_named = (reference == "current" and len(matches) == 1
                           and matches[0]["id"] == ordinal["id"])
            if proposal["ending_evidence"] is None and not (bound_repeat or bound_named):
                return repair("missing_ending_evidence", "ending_number 缺少本轮逐字 ending_evidence；沿用真实前轮同一结局或完整来源结局名称时可为 null。编号含义由模型解释，服务器只核验引用归属和来源排序。")
            if target is not None and target["id"] != ordinal["id"]:
                return repair("reference_ending_mismatch", "ending_number 与真实前轮对象不一致；查询新结局时请使用 current。")
            if matches and any(r["id"] != ordinal["id"] for r in matches):
                return repair("ending_identity_mismatch", "结局名称与 ending_number 对应的来源结局不同，请按用户原问修正提议。")
            target, matches = ordinal, []
            parsed["ending_ordinal"] = {"number": proposal["ending_number"], "source_id": ordinal["source_id"],
                                        "verification": "exact_normal_ending_id_and_priority"}
        elif target is None:
            matches = [r for r in matches if r["parent_native_id"] == theme]
            source_matches = [r for r in global_matches if r["parent_native_id"] == theme]
            if (kind and not matches and len(source_matches) == 1
                    and source_matches[0]["kind"] in _SOURCE_INTENTS):
                return repair(
                    "source_kind_mismatch",
                    "完整名称在固定发布中唯一对应的对象类型与查询意图冲突。请按 source_target 的来源类型重新提交意图，保留目标名称和逐字用户条件；不要将已知对象改成不存在，也不要自行生成事实。",
                    source_matches[0],
                )
            names = {r["name"] for r in matches}
            # Document queries have no preselected kind. Resolve duplicate event
            # scenes through the existing source candidate flow, by actual kinds.
            event_candidates = {r["kind"] for r in matches} == {"roguelike_event"}
            if not matches or len(names) != 1 or len(matches) > 1 and not event_candidates:
                return refuse("固定来源没有唯一对应此对象，请核对名称、主题或原始ID。")
            target = matches[0]
        parsed.update(entity_name=target["name"], entity_native_id=target["native_id"])
        if condition_anchor is not None and not any(
                c["type"] == "relic_possession" and c.get("name") == condition_anchor["name"]
                for c in conditions):
            return repair("missing_user_condition", "entity_name 指向条件藏品，查询目标是结局。请将该藏品的用户原话解释保留在 conditions；不能在查询结局时丢弃它。")
        same_target = previous is not None and prior_entity is not None and target["native_id"] == prior_entity["native_id"]
        opaque_unresolved = ""
        old_quotes = []
        if same_target:
            old_state = previous.get("dialogue", {})
            inherited = old_state.get("unresolved_text", "")
            from arknights_agent.roguelike_condition_resolution import recover_unresolved_quotes

            old_quotes = recover_unresolved_quotes(session, release, previous)
            if old_quotes is None:
                old_quotes = []
            if (not isinstance(old_quotes, list) or any(not isinstance(q, str) or not q
                    or not any(q in user for user in questions[:-1]) for q in old_quotes)):
                raise InvalidData("invalid authoritative unresolved quote history")
            # Never split an old generated string to guess which uncertainty
            # the user meant. Unrecoverable legacy display state stays pending.
            if inherited and inherited != "；".join(old_quotes):
                opaque_unresolved = inherited
            parsed["unresolved_quotes"] = list(dict.fromkeys([
                *old_quotes, *parsed["unresolved_quotes"]]))
        if (proposal["unresolved"] and reference != "current"
                and proposal["intent"] != "clarify"
                and not any(c["type"] == "unknown" for c in conditions)
                and not (same_target and proposal.get("condition_resolutions")
                         and all(quote in old_quotes for quote in proposal["unresolved"]))):
            return repair(
                "unresolved_reference_needs_classification",
                "前轮引用已绑定唯一对象，请重新区分查询请求与实际未知条件：请求本身不放入 unresolved；"
                "实际未知条件须保留逐字引文并用 unknown 条件或 clarify 意图表达。不能删除真实未知条件。",
            )
        for quote in parsed["unresolved_quotes"]:
            if not any(c["type"] == "unknown" and c["raw_text"] == quote for c in conditions):
                conditions.append({
                    "type": "unknown", "value": None, "raw_text": quote,
                    "verification_status": "user_reported", "interpretation_status": "model_proposed",
                    "citation_ids": [],
                })
        if proposal.get("condition_resolutions"):
            from arknights_agent.roguelike_condition_resolution import apply_condition_resolutions

            def validate_replacement(raw):
                typed = _proposal({"intent": "clarify", "conditions": [raw]})
                return _conditions(typed, [original_question], objects, theme, bindings=bindings)[0]

            resolved = apply_condition_resolutions(
                session, release, original_question, previous, proposal["condition_resolutions"],
                inherited_conditions=conditions,
                current_conditions=_conditions(condition_proposal, questions, objects, theme, bindings=bindings),
                scope_binding={"theme_id": theme, "theme_scope": "single", "entity_name": target["name"]},
                validate_replacement=validate_replacement,
                acknowledged_unresolved=proposal["unresolved"],
            )
            if resolved.get("proposal_repair_request"):
                parsed["proposal_repair_request"] = resolved["proposal_repair_request"]
                return refuse(resolved["proposal_repair_request"]["detail"])
            conditions = resolved["conditions"]
            parsed["condition_resolution_provenance"] = resolved["provenance"]
            parsed["unresolved_quotes"] = [q for q in parsed["unresolved_quotes"]
                                            if q not in resolved["resolved_unknown_quotes"]]
        parsed["user_reported_conditions"] = conditions
        parsed["unresolved_text"] = "；".join(filter(None, [
            opaque_unresolved, "；".join(parsed["unresolved_quotes"])]))
        if proposal["intent"] == "clarify" or parsed["unresolved_text"] or any(c["type"] == "unknown" for c in conditions):
            unknown = list(dict.fromkeys([*parsed["unresolved_quotes"],
                *(c["raw_text"] for c in conditions if c["type"] == "unknown")]))
            if unknown:
                return refuse("你提到的" + "、".join(f"「{q}」" for q in unknown)
                              + "具体指什么？补充后我继续查。")
            return refuse("还需要补充所问对象或条件，才能继续查这条资料。")
        if proposal["selection_number"] is not None and reference not in {"previous_candidate", "previous_menu", "source_menu"}:
            return refuse("菜单或候选序号必须绑定真实前轮列表。")
        if reference == "source_menu":
            number, quote = proposal["selection_number"], proposal["selection_evidence"]
            if target["kind"] != "roguelike_event" or len(matches) != 1:
                return repair("source_menu_requires_unique_event", "source_menu必须由模型明确定位唯一来源事件，不能从同名候选或后续场景取首项。", fields=["entity_name", "entity_evidence"])
            if number is None or quote is None:
                return repair("missing_source_menu_selection", "source_menu需要本轮明确选择的编号及本轮逐字selection_evidence；查看整份菜单使用current，不默认第一个。", fields=["selection_number", "selection_evidence", "reference"])
            if quote not in original_question:
                return repair("nonliteral_selection_evidence", "selection_evidence必须来自本轮用户原话，不能将历史或来源文字冒充本轮选择。")
            _replay_explicit_identity(session, release, original_question, store, target, rows)
        if indexed_evidence is not None:
            same_bound_menu = (reference == "previous_menu" and bound_menu is not None
                               and bound_menu[0]["entity_native_id"] == target["native_id"])
            if not same_bound_menu and (len(matches) != 1 or matches[0]["native_id"] != target["native_id"]):
                return repair("indexed_selector_not_unique", "历史逐字引文只证明来源，不替模型选择对象；请显式选择唯一来源身份。", fields=["entity_name", "entity_evidence"])
            _replay_explicit_identity(session, release, original_question, store, target, rows)
            parsed["entity_evidence_binding"] = {**indexed_evidence,
                "entity_native_id": target["native_id"], "theme_id": target["parent_native_id"],
                "source_record_replayed": True, "proposal_mutated": False}
        canonical = _canonical(theme, target, proposal["intent"])
        if len(matches) > 1:
            request = "所有可选按钮和完整奖励是什么" if proposal["intent"] == "event_menu" else "完整原始配置是什么"
            canonical = f"肉鸽{theme}，事件「{target['name']}」的{request}？"
        parsed["canonical_question"] = canonical
        result = answer_roguelike(session, release, canonical, store=store)
        if reference == "source_menu":
            options = result.get("event_menu", {}).get("options", [])
            number = proposal["selection_number"]
            records = [r for r in result.get("records", [])
                       if r.get("kind") not in {"roguelike_buff", "roguelike_wiki"}]
            if (result.get("status") != "answered" or not result.get("claims")
                    or not result.get("citations") or result.get("candidates")
                    or len(records) != 1 or records[0].get("native_id") != target["native_id"]
                    or not isinstance(options, list) or not options
                    or any(not isinstance(o, dict) or not isinstance(o.get("raw"), str) for o in options)):
                return refuse("当前来源菜单不能完整核验，本次不选择按钮。", "needs_verification")
            if number > len(options):
                return repair("source_menu_number_out_of_range", "该编号不在当前来源场景的主菜单中，不能借用后续场景或子按钮。", fields=["selection_number", "selection_evidence"])
            chosen = copy.deepcopy(options[number - 1])
            result["selected_option"] = {"number": number, **chosen,
                                         "citation_ids": result["event_menu"]["citation_ids"]}
            parsed["selected_option_number"] = number
            parsed["selection_grounding"] = {
                "quote": proposal["selection_evidence"], "number": number, "reference": reference,
                "verification": "literal_current_user_quote", "interpretation_status": "model_proposed",
                "source_list": "current_verified_source_menu",
                "source_menu_sha256": services.content_digest(options),
                "not_a_previous_delivered_menu": True,
            }
            result["answer"] = render_selected_option(result, number)
        if reference == "previous_menu" and result.get("event_menu"):
            number = proposal["selection_number"]
            old_options = previous["event_menu"]["options"]
            options = result["event_menu"]["options"]
            if (number > len(options) or any("raw" not in o for o in [*old_options, *options])
                    or [o["raw"] for o in old_options] != [o["raw"] for o in options]):
                return refuse("前轮菜单和重新核验的原文不同，本次不借用旧按钮。", "needs_verification")
            chosen = copy.deepcopy(options[number - 1])
            parsed["selected_option_number"] = number
            result["selected_option"] = {"number": number, **chosen,
                                         "citation_ids": result["event_menu"]["citation_ids"]}
            result["answer"] = render_selected_option(result, number)
        if proposal["intent"] == "ending_route":
            _conditional_route(result, parsed, answer, rows, session, release, store)
        elif conditions:
            result["answer"] = _state_intro(conditions) + result["answer"]
        result.setdefault("conditions", {})["route_readiness_verified"] = False
        return decorate(result)
    except InvalidData as exc:
        parsed["proposal_validation_error"] = str(exc)
        return repair("invalid_query_proposal", "模型提出的查询或用户条件未通过原话、对象及指代核验：" + str(exc))
    except DomainError:
        return refuse("固定来源、版本或对象校验失败，不能采用模型提议补写事实。", "needs_verification")
