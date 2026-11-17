"""Typed, source-pinned relic effects, including cross-theme coverage gaps.

Natural language interpretation belongs to the model. This module receives its
selectors, verifies the complete raw relic catalog, and presents original usage
text. It never calls the legacy question parser or chooses a candidate by order.
"""

from __future__ import annotations

import copy
import re

from arknights_agent import services
from arknights_agent.errors import Conflict, InvalidData
from arknights_agent.roguelike import _Answer
from arknights_agent.roguelike_condition_resolution import _merge, apply_condition_resolutions
from arknights_agent.roguelike_dialogue import _state_intro
from arknights_agent.roguelike_sources import THEME_NAMES


def _display(text):
    # Client display markup only; never infer a mechanic or substitute values.
    return re.sub(r"<(?:[@$][A-Za-z0-9_.]+|/)>", "", text).replace("\\n", "\n")


def _identity(row, record):
    return {
        "record_id": row["id"], "name": row["name"], "native_id": row["native_id"],
        "theme_id": row["parent_native_id"], "kind": row["kind"],
        "data_sha256": services.content_digest(record["data"]),
    }


def _source_group(answer, rows, name):
    """Recheck complete records, not a first match or a previous transcript."""
    group = [r for r in rows if r["kind"] == "roguelike_relic" and r["name"] == name]
    proof = _Answer(answer.session, answer.release, answer.result["question"], answer.store)
    proof.catalog_rows = answer.catalog_rows
    targets = []
    for row in group:
        record = proof.record(row["id"])
        proof.cite(record, "", record["data"])
        targets.append(_identity(row, record))
    return group, targets


def _previous_group(answer, rows, previous):
    if not isinstance(previous, dict) or (
        previous.get("release_id") != answer.release.id
        or previous.get("manifest_hash") != answer.release.manifest_hash
        or previous.get("module") != "roguelike"
    ):
        raise InvalidData("previous relic context must be a same-release server answer")
    context = previous.get("named_relic_context")
    dialogue = previous.get("dialogue", {})
    if (not isinstance(context, dict) or previous.get("candidates")
            or context.get("release_id") != answer.release.id
            or context.get("manifest_hash") != answer.release.manifest_hash
            or not isinstance(context.get("name"), str)
            or dialogue.get("intent") != "relic_effect"
            or dialogue.get("entity_name") != context["name"]):
        raise InvalidData("no source-verified previous named relic group")
    group, targets = _source_group(answer, rows, context["name"])
    if not targets or not services.same_json_value(targets, context.get("targets")):
        raise Conflict("previous named relic identities differ from complete current sources")
    actual = {r["id"]: r for r in group}
    records = previous.get("records", [])
    if not isinstance(records, list) or len({r.get("id") for r in records}) != len(records):
        raise Conflict("previous relic source records are invalid")
    for record in records:
        if record.get("id") not in actual:
            raise Conflict("previous relic record is outside its named group")
        if not services.same_json_value(record, answer.record(record["id"])):
            raise Conflict("previous relic record differs from original source")
    proof = _Answer(answer.session, answer.release, answer.result["question"], answer.store)
    proof.catalog_rows = answer.catalog_rows
    prior_citations = previous.get("citations", [])
    if not isinstance(prior_citations, list):
        raise Conflict("previous relic citations are not a list")
    valid_citations = {}
    for citation in prior_citations:
        if not isinstance(citation, dict) or citation.get("record_id") not in {r["id"] for r in records}:
            raise Conflict("previous relic citation refers outside its actual records")
        record = answer.record(citation["record_id"])
        usage = record["data"].get("usage")
        if not isinstance(usage, str) or not usage.strip():
            raise Conflict("previous relic usage is not in original source")
        cid = proof.cite(record, "/usage", usage)
        expected = next(c for c in proof.result["citations"] if c["id"] == cid)
        if not services.same_json_value(
            {k: v for k, v in citation.items() if k != "id"},
            {k: v for k, v in expected.items() if k != "id"},
        ) or not isinstance(citation.get("id"), str) or citation["id"] in valid_citations:
            raise Conflict("previous relic citation differs from original usage proof")
        valid_citations[citation["id"]] = expected
    claims = previous.get("claims", [])
    if not isinstance(claims, list) or len(claims) != len(records) or len(prior_citations) != len(records):
        raise Conflict("previous relic factual coverage differs from its source records")
    for claim in claims:
        refs = claim.get("citation_ids", []) if isinstance(claim, dict) else []
        if len(refs) != 1 or refs[0] not in valid_citations:
            raise Conflict("previous relic claim has no unique verified usage citation")
        verified = valid_citations[refs[0]]
        if claim.get("record_id") != verified["record_id"] or not services.same_json_value(claim.get("value"), verified["field_value"]):
            raise Conflict("previous relic effect claim differs from original usage")
    if previous.get("status") == "answered" and (
        not records or not previous.get("claims") or not previous.get("citations")
    ):
        raise Conflict("previous positive relic group has no factual source output")
    # A verified group can survive a single-theme source gap. It does not turn
    # the missing theme into an existing relic or a selected object.
    if previous.get("status") not in {"answered", "needs_verification", "needs_clarification"}:
        raise InvalidData("previous relic context was not an eligible server result")
    return context["name"], group, targets


def _retains_condition_scope(proposal, previous, theme, scope):
    if previous is None:
        return False
    state = previous.get("dialogue", {})
    named = previous.get("named_relic_context", {})
    same_group = (proposal.get("reference") == "previous_entity"
                  or proposal.get("entity_name") == named.get("name")
                  or any(proposal.get("entity_name") == t.get("native_id")
                         for t in named.get("targets", []) if isinstance(t, dict)))
    return ((scope == "single" and state.get("theme_scope") == "single"
             and (state.get("theme_id") is not None and state.get("theme_id") == theme
                  or state.get("theme_id") is None and same_group))
            or state.get("theme_scope") == "all" and same_group)


def _condition_identity(condition):
    if not isinstance(condition, dict) or set(condition) - {
            "type", "name", "value", "quote", "reference", "subject_quote"}:
        return None
    return {key: copy.deepcopy(condition.get(key, default)) for key, default in (
        ("type", None), ("name", None), ("value", None), ("quote", None),
        ("reference", "current"), ("subject_quote", None))}


def _historical_condition(answer, proposal, previous, condition, theme, scope):
    """Allow only an identical, real parent condition to be re-proposed."""
    if answer is not None:
        from arknights_agent.roguelike_interpretation import pending_theme_clarification_binding

        clarified = pending_theme_clarification_binding(
            answer.session, answer.release, answer.result["question"], proposal, previous, condition)
        if clarified is not None:
            return clarified
    if (answer is None or previous is None or not isinstance(previous.get("run_id"), str)
            or not _retains_condition_scope(proposal, previous, theme, scope)):
        return None
    from arknights_agent.runtime.dialogue_context import load_previous, previous_questions

    audited = load_previous(answer.session, previous["run_id"], answer.release.id)
    if not services.same_json_value(audited, previous):
        return None
    quote = condition.get("quote")
    if not isinstance(quote, str) or not any(quote in q for q in previous_questions(audited)):
        return None
    state = audited.get("dialogue", {})
    raw = state.get("model_interpretation", {})
    original = raw.get("conditions", []) if isinstance(raw, dict) else []
    wanted = _condition_identity(condition)
    if (wanted is None or not isinstance(original, list)
            or not any(services.same_json_value(_condition_identity(c), wanted) for c in original)):
        return None
    retained = [*state.get("user_reported_conditions", []),
                *state.get("pending_user_reported_conditions", [])]
    expected = {"type": wanted["type"], "name": wanted["name"], "value": wanted["value"],
                "raw_text": quote, "reference": wanted["reference"]}
    if not any(isinstance(c, dict) and services.same_json_value(
            {key: c.get(key, "current" if key == "reference" else None) for key in expected}, expected)
            and c.get("verification_status") == "user_reported"
            and c.get("interpretation_status") == "model_proposed" and c.get("citation_ids") == []
            for c in retained):
        return None
    _previous_group(answer, answer.catalog(), audited)
    return {"protocol": "same-parent-exact-user-condition-reproposal-v1",
            "source_run_id": audited["run_id"],
            "source_answer_response_hash": services.content_digest(audited),
            "condition_sha256": services.content_digest(wanted), "quote": quote,
            "release_id": answer.release.id, "manifest_hash": answer.release.manifest_hash,
            "state_verification": "user_reported_only", "source_answer_mutated": False}


def _condition_source(answer, rows, condition, theme, scope):
    """Check a model condition's source identity; never interpret its quote."""
    if condition.get("type") != "relic_possession":
        return None
    selector = condition.get("name")
    matches = [r for r in rows if r["kind"] == "roguelike_relic" and selector is not None
               and selector in {r["name"], r["native_id"], r.get("source_id")}
               and (scope == "all" or theme is None or r["parent_native_id"] == theme)]
    names = {r["name"] for r in matches}
    if not matches or len(names) != 1:
        return "condition_source_identity_missing"
    # Without a selected theme, this proves only the named source group. It
    # must not pick a period or turn missing scope into an absent relic.
    owners = [r["parent_native_id"] for r in matches]
    if any(owners.count(owner) > 1 for owner in set(owners)):
        return "condition_source_identity_ambiguous"
    proof = _Answer(answer.session, answer.release, answer.result["question"], answer.store)
    proof.catalog_rows = answer.catalog_rows
    for row in matches:
        record = proof.record(row["id"])
        proof.cite(record, "", record["data"])
    return None


def _source_identity_theme_conflict(rows, selector, theme, scope):
    if scope != "single" or theme is None or selector is None:
        return None
    selected = [r for r in rows if selector in {r["native_id"], r.get("source_id")}]
    if len(selected) == 1 and selected[0]["parent_native_id"] != theme:
        return selected[0]
    return None


class _UnboundHistoricalCondition(InvalidData):
    def __init__(self, index, condition):
        super().__init__("historical condition must exactly match the real inherited parent state")
        self.index = index
        self.condition = copy.deepcopy(condition)


def _parent_observation(answer, previous):
    """Only expose authenticated parent data; never promote failed state."""
    if previous is None or not isinstance(previous.get("run_id"), str):
        return {"source_run_id": None, "source_answer_response_hash": None,
                "parent_status": None, "parent_theme_id": None,
                "parent_theme_scope": None, "questions": []}
    from arknights_agent.runtime.dialogue_context import load_previous, previous_questions

    audited = load_previous(answer.session, previous["run_id"], answer.release.id, allow_general=True)
    if not services.same_json_value(audited, previous):
        raise Conflict("condition feedback parent differs from its audited response")
    state = audited.get("dialogue", {})
    return {"source_run_id": audited["run_id"],
            "source_answer_response_hash": services.content_digest(audited),
            "parent_status": audited.get("status"), "parent_theme_id": state.get("theme_id"),
            "parent_theme_scope": state.get("theme_scope"), "questions": previous_questions(audited)}


def _state(proposal, question, previous, theme, scope, *, return_parts=False,
           answer=None, historical_bindings=None):
    current = []
    for index, condition in enumerate(proposal.get("conditions", [])):
        quote = condition.get("quote")
        if not isinstance(quote, str) or not quote:
            raise InvalidData("relic effect condition requires a literal user quote")
        if quote not in question:
            binding = _historical_condition(answer, proposal, previous, condition, theme, scope)
            if binding is None:
                raise _UnboundHistoricalCondition(index, condition)
            if historical_bindings is not None:
                historical_bindings.append(binding)
        kind, value = condition.get("type"), condition.get("value")
        if kind in {"floor", "difficulty"} and (
            type(value) is not int or value < (1 if kind == "floor" else 0)
        ):
            raise InvalidData("invalid numeric user condition")
        if kind == "knight_alive" and type(value) is not bool:
            raise InvalidData("invalid boolean user condition")
        if kind == "relic_possession" and not (
                type(value) is bool or isinstance(value, str) and value in {"owned", "not_owned"}):
            raise InvalidData("invalid possession user condition")
        if kind not in {"floor", "difficulty", "knight_alive", "relic_possession", "unknown"}:
            raise InvalidData("unsupported user condition type")
        current.append({
            "type": kind, "name": condition.get("name"), "value": copy.deepcopy(value),
            "raw_text": quote, "reference": condition.get("reference", "current"),
            "verification_status": "user_reported", "interpretation_status": "model_proposed",
            "citation_ids": [],
        })
    old = []
    if _retains_condition_scope(proposal, previous, theme, scope):
        state = previous.get("dialogue", {})
        old = copy.deepcopy(state.get("pending_user_reported_conditions", []))
        history = previous.get("dialogue_state", {}).get("questions", [previous.get("question", "")])
        if any(not isinstance(c, dict) or not isinstance(c.get("raw_text"), str)
               or not any(c["raw_text"] in q for q in history)
               or c.get("citation_ids") != [] or c.get("verification_status") != "user_reported"
               for c in old):
            raise Conflict("previous user condition provenance differs")
    merged = _merge(old, current)
    if return_parts:
        return old, current, merged
    return merged


def answer_relic_effects(session, release, original_question, proposal, *, store=None,
                         previous_answer=None, provenance_parent=None):
    """Answer model-selected relic effects without interpreting user wording."""
    answer = _Answer(session, release, original_question, store)
    if not isinstance(proposal, dict) or proposal.get("intent") != "relic_effect":
        raise InvalidData("relic effects require the typed relic_effect intent")
    partial_allowed = proposal.get("allow_partial_source_answer", False)
    if type(partial_allowed) is not bool:
        raise InvalidData("partial source answer selector must be a boolean")
    if not partial_allowed and "allow_partial_source_answer" in proposal:
        # An omitted/default-false capability preserves the historical typed
        # proposal and answer hashes. Never mutate the model's original input.
        proposal = copy.deepcopy(proposal)
        proposal.pop("allow_partial_source_answer")
    if not isinstance(original_question, str) or not original_question.strip() or len(original_question) > 2000:
        raise InvalidData("question must contain 1 to 2000 characters")
    scope = proposal.get("theme_scope") or "single"
    theme, selector = proposal.get("theme_id"), proposal.get("entity_name")
    if scope not in {"single", "all"} or theme is not None and theme not in THEME_NAMES:
        raise InvalidData("invalid typed relic effect scope")
    if scope == "all" and theme is not None:
        raise InvalidData("all-theme effects cannot also restrict a single theme")
    if previous_answer is not None and (
        previous_answer.get("release_id") != release.id
        or previous_answer.get("manifest_hash") != release.manifest_hash
    ):
        raise InvalidData("relic effect dialogue cannot cross releases")
    previous = previous_answer
    dialogue = {
        "intent": "relic_effect", "theme_scope": scope, "theme_id": theme,
        "entity_name": None, "entity_native_id": None, "context_used": False,
        "interpretation_origin": "model_query_proposal", "model_interpretation": copy.deepcopy(proposal),
        "state_observation": "user_reported_only", "user_reported_conditions": [],
        "pending_user_reported_conditions": [], "pending_conditions_theme_id": None,
        "unresolved_text": "", "original_user_questions": [],
    }
    history = previous.get("dialogue_state", {}).get("questions", [previous.get("question", "")]) if previous else []
    dialogue["original_user_questions"] = [*history, original_question]

    def finish(message=None, status=None):
        if message is not None:
            answer.result["answer"] = message
        if status is not None:
            answer.result["status"] = status
        answer.result["dialogue"] = dialogue
        answer.result["theme_id"] = theme
        answer.result["version"]["theme_id"] = theme
        answer.result["conditions"].update({
            "user_reported_conditions": copy.deepcopy(dialogue["user_reported_conditions"]),
            "pending_user_reported_conditions": copy.deepcopy(dialogue["pending_user_reported_conditions"]),
            "pending_conditions_theme_id": dialogue["pending_conditions_theme_id"],
            "runtime_mechanics_verified": False,
        })
        return answer.result

    def reject_identity_theme(selected):
        from arknights_agent.roguelike_interpretation import _source_target

        proof = _Answer(session, release, original_question, store)
        proof.catalog_rows = answer.catalog_rows
        record = proof.record(selected["id"])
        proof.cite(record, "", record["data"])
        dialogue["proposal_repair_request"] = {
            "code": "reference_theme_mismatch", "fields": ["theme_id", "entity_name"],
            "detail": "entity_name精确指向的藏品来源ID属于另一个主题，与本次single/theme_id冲突。"
                      "请结合用户原话自行纠正entity_name或theme_id；按新主题查询同名藏品组时，"
                      "可由模型选择同名组名称或null，不能忽略完整来源ID后查询另一对象。",
            "source_target": _source_target(selected), "proposal_mutated": False,
        }
        return finish(dialogue["proposal_repair_request"]["detail"], "needs_clarification")

    if store is None or not answer.entry:
        return finish("当前无法核验藏品效果的原始资料，暂不能回答具体效果。", "needs_verification")
    for field in ("entity_evidence", "theme_evidence", "ending_evidence"):
        if proposal.get(field) is not None and proposal[field] not in original_question:
            if field == "entity_evidence":
                from arknights_agent.roguelike_interpretation import _historical_relic_evidence

                binding = _historical_relic_evidence(
                    session, release, original_question, proposal, previous, store)
                if binding is not None:
                    dialogue["entity_evidence_binding"] = binding
                    dialogue["context_used"] = True
                    continue
            raise InvalidData("relic effect evidence must be literal in the current question")
    unresolved = proposal.get("unresolved", [])
    if not isinstance(unresolved, list) or any(not isinstance(q, str) or not q
            or not any(q in user for user in dialogue["original_user_questions"]) for q in unresolved):
        raise InvalidData("relic effect unresolved text must be a literal user quote")
    rows = answer.catalog()
    relics = [r for r in rows if r["kind"] == "roguelike_relic"]
    proofs = []
    for ident in THEME_NAMES:
        answer.verify_catalog(ident, "relic", rows)
        table = answer.source_table(ident, "items")
        proofs.append({"theme_id": ident, "complete_raw_items_count": len(table),
                       "raw_items_sha256": services.content_digest(table),
                       "complete_relic_membership_verified": True})
    rejected = []
    for index, condition in enumerate(proposal.get("conditions", [])):
        error = _condition_source(answer, rows, condition, theme, scope)
        if error is not None:
            rejected.append({"index": index, "code": error, "selector": condition.get("name"),
                             "record_kind": "roguelike_relic", "theme_id": theme, "theme_scope": scope,
                             "complete_source_catalog_checked": True})
    if rejected:
        fields = [f"conditions[{c['index']}].{slot}" for c in rejected for slot in ("type", "name")]
        dialogue["proposal_repair_request"] = {
            "code": rejected[0]["code"], "fields": fields,
            "detail": "所提交的relic_possession条件没有在当前来源范围唯一对应真实藏品。"
                      "请由模型结合用户原话自行纠正所列条件的type/name；真正不明的限制须逐字保留，"
                      "不能把它当作已持有的藏品，也不能为通过校验删掉限制或其他真实条件。"
                      "服务器没有替你改成unknown，也没有接受这份提议为玩家状态。",
            "condition_source_observations": rejected, "preserve_other_user_conditions": True,
            "proposal_mutated": False, "game_facts_or_user_state_accepted": False,
        }
        return finish(dialogue["proposal_repair_request"]["detail"], "needs_clarification")
    historical_bindings = []
    try:
        old_conditions, current_conditions, pending = _state(
            proposal, original_question, previous, theme, scope, return_parts=True,
            answer=answer, historical_bindings=historical_bindings)
    except _UnboundHistoricalCondition as exc:
        parent = _parent_observation(answer, provenance_parent or previous)
        quote = exc.condition.get("quote")
        indices = [index for index, user in enumerate(parent.pop("questions")) if quote in user]
        repair = {
            "code": "unbound_historical_condition", "fields": [f"conditions[{exc.index}].quote"],
            "detail": "这条条件引文不在当前问句，且未绑定同范围内真实已交付的前轮条件。"
                      "请只复核所列条件及其适用范围，保留当前对象、主题和其它有效参数；"
                      "不要把条件错误改成对象指代错误。历史逐字位置只证明原话存在，不证明"
                      "条件可跨主题沿用或失败轮状态已生效。由模型结合完整请求决定是否重提、"
                      "说明范围或澄清；不能为通过校验擅自删除仍适用的条件。",
            "condition_index": exc.index, "submitted_condition": exc.condition,
            "matched_history_indices": indices, "current_theme_id": theme,
            "current_theme_scope": scope, "actual_parent": parent,
            "preserved_target": {key: copy.deepcopy(proposal.get(key)) for key in (
                "intent", "reference", "entity_name", "entity_evidence", "theme_id", "theme_scope")},
            "quote_locations_only": True, "historical_condition_accepted": False,
            "automatic_condition_transfer": False, "proposal_mutated": False,
        }
        dialogue["proposal_repair_request"] = repair
        return finish(repair["detail"], "needs_clarification")
    if historical_bindings:
        dialogue["condition_history_bindings"] = historical_bindings
    # An empty array in a later model proposal does not resolve a previously
    # delivered uncertainty. Keep unresolved literal user quotes as user-only
    # unknown state, with the same scope-inheritance rules as typed conditions.
    for quote in unresolved:
        item = {
            "type": "unknown", "name": None, "value": None, "raw_text": quote,
            "verification_status": "user_reported", "interpretation_status": "model_proposed",
            "citation_ids": [], "quote_source": "actual_user_question_history",
        }
        if partial_allowed:
            prior = [c for c in old_conditions if c.get("type") == "unknown" and c.get("raw_text") == quote]
            if len(prior) == 1:
                # A literal inventory acknowledgement is not a new semantic
                # interpretation of the old unknown or its model-given label.
                item = copy.deepcopy(prior[0])
        if not any(c["type"] == "unknown" and c["raw_text"] == quote for c in current_conditions):
            current_conditions.append(copy.deepcopy(item))
        if not any(c["type"] == "unknown" and c["raw_text"] == quote for c in pending):
            pending.append(item)
    dialogue["user_reported_conditions"] = copy.deepcopy(pending)
    dialogue["pending_user_reported_conditions"] = pending
    dialogue["pending_conditions_theme_id"] = theme if pending else None
    dialogue["unresolved_text"] = "；".join(unresolved)
    legacy_gaps = [{"name": c.get("name"), "quote": c["raw_text"], "code": code}
                   for c in old_conditions
                   if (code := _condition_source(answer, rows, c, theme, scope)) is not None]
    if legacy_gaps:
        dialogue["historical_condition_source_gaps"] = legacy_gaps
        dialogue["state_condition_source_validation"] = "historical_gap_not_reclassified_or_verified"
        return finish("先前记录中的一项条件未能对应当前固定资料中的真实藏品。"
                      "这项条件仍保留为待核验状态；不能当作已持有，也不能自行猜成其他条件。",
                      "needs_verification")
    reference = proposal.get("reference", "current")
    name, group, targets = None, [], []
    if reference == "previous_entity":
        try:
            name, group, targets = _previous_group(answer, rows, previous)
        except InvalidData:
            if (selector is not None and isinstance(proposal.get("entity_evidence"), str)
                    and proposal["entity_evidence"] in original_question):
                parent = _parent_observation(answer, provenance_parent or previous)
                parent.pop("questions")
                repair = {
                    "code": "unbound_previous_relic_reference", "fields": ["reference"],
                    "detail": "真实前轮没有可沿用的已交付藏品对象。当前提案已经给出对象名与本轮引文，"
                              "请由模型保留该对象、主题和有效条件，复核reference并重新提交来源查询。"
                              "失败轮或未交付的暂存答案不能充当前轮已绑定对象；不要要求用户重复已经提供的名称。",
                    "actual_parent": parent, "submitted_selector": selector,
                    "submitted_entity_evidence": proposal.get("entity_evidence"),
                    "current_theme_id": theme, "current_theme_scope": scope,
                    "proposal_mutated": False, "source_object_selected_automatically": False,
                }
                dialogue["proposal_repair_request"] = repair
                return finish(repair["detail"], "needs_clarification")
            return finish("前面对话尚未确定所问藏品。请告诉我藏品名称，我再按你补充的主题查效果。", "needs_clarification")
        if selector is not None and not any(selector in {r["name"], r["native_id"], r.get("source_id")} for r in group):
            return finish("这次给出的藏品名称与前轮所问对象不同，请确认这次要看哪一件藏品。", "needs_clarification")
        conflict = _source_identity_theme_conflict(group, selector, theme, scope)
        if conflict is not None:
            return reject_identity_theme(conflict)
        dialogue["context_used"] = True
    elif reference == "current":
        matches = [r for r in relics if selector is not None and selector in {
            r["name"], r["native_id"], r.get("source_id")}]
        names = {r["name"] for r in matches}
        if len(names) == 1:
            conflict = _source_identity_theme_conflict(matches, selector, theme, scope)
            if conflict is not None:
                return reject_identity_theme(conflict)
            name = next(iter(names))
            group, targets = _source_group(answer, rows, name)
    else:
        return finish("藏品效果需要明确藏品名称或沿用前轮所问藏品，不能把事件按钮或候选序号当作藏品。", "needs_clarification")
    if name is None:
        return finish("当前固定资料未查到这件藏品的效果条目。请核对藏品名称或补充它的描述；这不代表游戏中不存在。", "needs_verification")
    dialogue["entity_name"] = name
    answer.result["named_relic_context"] = {
        "release_id": release.id, "manifest_hash": release.manifest_hash,
        "name": name, "targets": targets, "source_kind": "roguelike_relic",
        "verification": "complete_raw_relic_catalog_and_objects", "is_selected_single_object": False,
    }
    require_inventory = (partial_allowed and previous is not None
                         and _retains_condition_scope(proposal, previous, theme, scope))
    if proposal.get("condition_resolutions") or require_inventory:
        # The model explicitly links a real earlier unknown to the current
        # user's explanation. The service checks lineage, source scope and
        # typed values; it never interprets explanation/withdrawal wording.
        from arknights_agent.roguelike_interpretation import _conditions, _proposal

        def validate_replacement(condition):
            typed = _proposal({"intent": "clarify", "conditions": [condition]})
            return _conditions(typed, [original_question], rows, theme)[0]

        resolved = apply_condition_resolutions(
            session, release, original_question, previous, proposal.get("condition_resolutions", []),
            inherited_conditions=old_conditions, current_conditions=current_conditions,
            scope_binding={"theme_id": theme, "theme_scope": scope, "entity_name": name},
            validate_replacement=validate_replacement,
            acknowledged_unresolved=unresolved,
            require_explicit_unknown_inventory=require_inventory,
        )
        pending = resolved["conditions"]
        dialogue["user_reported_conditions"] = copy.deepcopy(pending)
        dialogue["pending_user_reported_conditions"] = pending
        dialogue["pending_conditions_theme_id"] = theme if pending else None
        if resolved["provenance"]:
            dialogue["condition_resolution_provenance"] = resolved["provenance"]
        if require_inventory:
            dialogue["condition_inventory_verification"] = resolved["inventory_verification"]
        if resolved["proposal_repair_request"] is not None:
            dialogue["proposal_repair_request"] = resolved["proposal_repair_request"]
            return finish(resolved["proposal_repair_request"]["detail"], "needs_clarification")
    unknown = [c["raw_text"] for c in pending if c["type"] == "unknown"]
    unknown_message = None
    if unknown or unresolved:
        quotes = list(dict.fromkeys([*unresolved, *unknown]))
        labels = []
        for quote in quotes:
            condition = next((c for c in pending
                              if c["type"] == "unknown" and c["raw_text"] == quote), {})
            label = condition.get("name")
            if not isinstance(label, str) or not label.strip() or label not in quote:
                label = quote
            labels.append(label)
        # Model-proposed literal labels shorten the message only. Exact label
        # de-duplication never merges or removes the underlying quote inventory.
        dialogue["unknown_condition_display"] = [
            {"unknown_quote": quote, "label": label, "state_mutated": False}
            for quote, label in zip(quotes, labels, strict=True)
        ]
        unknown_message = ("关于" + "、".join("「" + q + "」" for q in dict.fromkeys(labels))
                           + "，我还不清楚具体含义，不能把它当作已满足的游戏条件。请补充这一点。")
        if not partial_allowed:
            return finish(unknown_message, "needs_clarification")
    if scope == "single" and theme is None:
        message = f"你想看「{name}」在哪个主题的效果？也可以比较当前资料收录的各主题。"
        if unknown_message is not None:
            message += "\n" + unknown_message
        return finish(message, "needs_clarification")
    requested = list(THEME_NAMES) if scope == "all" else [theme]
    coverage, lines, available = [], [], 0
    for ident in requested:
        matching = [r for r in group if r["parent_native_id"] == ident]
        item = {"theme_id": ident, "theme_name": THEME_NAMES[ident], "status": "not_in_fixed_catalog", "items": []}
        if not matching:
            lines.append(f"{THEME_NAMES[ident]}：当前固定资料未收录「{name}」的藏品条目，无法核实该主题效果，不能套用其他主题。")
        elif len(matching) > 1:
            item["status"] = "same_kind_source_ambiguity"
            lines.append(f"{THEME_NAMES[ident]}：资料里有多条同名藏品，尚不能确定你所指的具体效果。")
        else:
            row = matching[0]
            record = answer.record(row["id"])
            usage = record["data"].get("usage")
            if not isinstance(usage, str) or not usage.strip():
                item["status"] = "usage_not_recorded"
                lines.append(f"{THEME_NAMES[ident]}：收录了「{name}」，但原始效果文案缺失，不能补写。")
            else:
                cid = answer.cite(record, "/usage", usage)
                answer.result["records"].append(record)
                answer.result["claims"].append({
                    "id": f"f{len(answer.result['claims']) + 1}", "record_id": record["id"],
                    "label": "藏品效果原文", "value": usage, "citation_ids": [cid],
                    "verification_status": "fixed_client_source_document",
                })
                item["status"] = "available"
                item["items"].append({"record_id": row["id"], "native_id": row["native_id"],
                                      "name": name, "usage": usage, "usage_text": _display(usage), "citation_ids": [cid]})
                lines.append(f"{THEME_NAMES[ident]}：{_display(usage)}。[{cid}]")
                available += 1
                if scope == "single":
                    dialogue["entity_native_id"] = row["native_id"]
        coverage.append(item)
    answer.result["relic_effects"] = {
        "name": name, "theme_scope": scope, "themes": coverage, "available_theme_count": available,
        "requested_theme_count": len(requested), "all_requested_effects_available": available == len(requested),
        "catalog_proofs": proofs,
        "absence_scope": "this_fixed_source_catalog_only_not_in_game_nonexistence",
    }
    answer.result["tool_trace"].append({"tool": "typed_relic_effect_source", "complete_raw_catalog_verified": True,
        "runtime_calculation": False, "buff_rows_not_additional_relic_entities": True})
    lines.insert(0, f"「{name}」在当前固定资料中的效果：")
    lines.append("以上是资料原文；未收录不代表游戏中不存在，也未核验当前线上数值或实战触发。")
    if pending:
        lines.append(_state_intro(pending).strip())
        lines.append("这里仅说明原文效果，没有据此推算实际触发结果。")
    withdrawals = list(dict.fromkeys(
        p["unknown_quote"] for p in dialogue.get("condition_resolution_provenance", [])
        if p.get("disposition") == "retract"
    ))
    if withdrawals:
        lines.append("已按你的更正撤回这些条件：" + "、".join(f"「{q}」" for q in withdrawals) + "。")
    if unknown_message is not None:
        lines.append(unknown_message)
        lines.append("以上仅是能核实的基础效果；尚未判断这些条件下是否适用或效果是否变化。")
        answer.result["partial_source_answer"] = {
            "requested_by_model": True, "source_facts_delivered": available > 0,
            "answer_completion": "partial_source_documentation",
            "unresolved_user_condition_quotes": list(dict.fromkeys([*unresolved, *unknown])),
            "all_user_conditions_preserved": True,
            "condition_application_evaluated": False,
        }
        return finish("\n".join(lines), "needs_clarification")
    return finish("\n".join(lines), "answered" if available else "needs_verification")
