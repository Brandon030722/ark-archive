"""Typed semantic requests over existing release-bound source builders.

This boundary validates data and evidence. It does not decide what an utterance
means with a second language parser, and accepts no model-written game facts.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from arknights_agent import agent, qa, services
from arknights_agent.errors import Conflict, InvalidData
from arknights_agent.models import KnowledgeRecord

# Executable selector capabilities, shared by source validation and model tool
# documentation. This contract describes typed fields, never user phrasing.
SOURCE_QUERY_FIELDS = {
    "catalog": frozenset({"entity_names", "rarity", "limit", "offset"}),
    "operator_overview": frozenset({"entity_names", "rarity", "compare"}),
    "operator_skill": frozenset({
        "entity_names", "rarity", "compare", "skill_index", "skill_name", "rank", "include_blackboard",
    }),
    "operator_talent": frozenset({
        "entity_names", "rarity", "compare", "elite_phase", "level", "potential_rank", "include_blackboard",
    }),
    "operator_module": frozenset({"entity_names", "rarity", "compare", "module_query", "module_level"}),
    "operator_attributes": frozenset({"entity_names", "rarity", "compare", "elite_phase", "level"}),
    "recruitment_calculation": frozenset({"tags", "duration_minutes", "rarity", "recruitment_scope"}),
    "recruitment_tag_overview": frozenset({"duration_minutes", "rarity"}),
    "garrison_metadata": frozenset({"phase_id", "activity_id"}),
    "garrison_field": frozenset({"phase_id", "activity_id", "field_key"}),
    "garrison_document": frozenset({"phase_id", "activity_id", "field_key"}),
    "garrison_regulation_record": frozenset({"phase_id", "activity_id", "regulation_kind", "entity_names"}),
    "garrison_bond_catalog": frozenset({"phase_id", "activity_id"}),
    "garrison_regulation_catalog": frozenset({"phase_id", "activity_id", "regulation_kind", "limit", "offset"}),
}
SOURCE_QUERY_MODULE_INTENTS = {
    "operator": frozenset({
        "catalog", "operator_overview", "operator_skill", "operator_talent", "operator_module", "operator_attributes",
    }),
    "recruitment": frozenset({"catalog", "recruitment_calculation", "recruitment_tag_overview"}),
    "garrison": frozenset({
        "garrison_metadata", "garrison_field", "garrison_document", "garrison_regulation_record", "garrison_bond_catalog", "garrison_regulation_catalog",
    }),
}
SOURCE_QUERY_COMMON_FIELDS = frozenset({"module", "intent", "unresolved"})


class SourceQueryRepair(InvalidData):
    def __init__(self, fields, reason="selectors_not_supported_by_intent", instruction=None, *, allowed_fields=None):
        self.fields = fields
        self.reason = reason
        self.instruction = instruction or "按当前用户请求更正intent或参数；不能丢弃用户实际指定条件。"
        self.allowed_fields = allowed_fields
        super().__init__("source query requires interpretation repair")


def _empty(context, module, status="needs_clarification", message=None):
    result = agent._refusal(
        context.release,
        context.original_question,
        message or "尚未确定所需对象或查询条件，请补充具体对象、字段或范围。",
        status,
    )
    result["module"] = module
    return result


def _records(context, module, kind):
    rows, offset = [], 0
    while True:
        page = services.list_records(
            context.session, context.release, module=module, kind=kind, limit=200, offset=offset
        )
        rows.extend(page)
        if len(page) < 200:
            return rows
        offset += 200


def _catalog_pagination(query):
    return (30 if query.limit is None else query.limit, 0 if query.offset is None else query.offset)


def _operator(context, query):
    plan = qa.validate_plan({
        key: getattr(query, key)
        for key in (
            "intent", "skill_index", "skill_name", "rank", "elite_phase", "level",
            "potential_rank", "module_query", "module_level", "compare",
        )
    } | {"intent": "operator_overview" if query.intent == "catalog" else query.intent})
    rows = _records(context, "operator", "operator")
    coverage = context.release.manifest["datasets"]["operator"].get("coverage", {})
    source_ids = coverage.get("operator_ids")
    if source_ids is not None and set(source_ids) != {row["native_id"] for row in rows}:
        raise Conflict("operator catalog differs from the published selection")
    if coverage.get("operator_count", len(rows)) != len(rows):
        raise Conflict("operator catalog count differs from the published selection")
    probe = qa._Answer(context.session, context.release, context.original_question, context.store, plan)
    for row in rows:
        # Filtering, object lookup and page totals all depend on these fields,
        # including rows outside the requested rarity and visible page.
        probe.cite(row, "/name", row["name"])
        probe.cite(row, "/rarity", row["data"].get("rarity"))
        if not any(
            services.get_evidence(context.session, context.release, eid)["pointer"].rsplit("/", 1)[-1]
            .replace("~1", "/").replace("~0", "~") == row["native_id"]
            for eid in row["evidence_ids"]
        ):
            raise Conflict("operator catalog identity differs from original source pointer")
    selector_replay = {
        "tool": "verify_complete_operator_catalog_selectors", "record_count": len(rows),
        "fields": ["native_id", "name", "rarity"],
        "source_snapshot_hashes": sorted({c["snapshot_hash"] for c in probe.result["citations"]}),
        "selectors_sha256": services.content_digest([
            [r["native_id"], r["name"], r["data"]["rarity"]] for r in rows
        ]),
    }
    if query.rarity is not None:
        rows = [r for r in rows if r["data"].get("rarity") == f"TIER_{query.rarity}"]
    selected = []
    for name in query.entity_names:
        matches = [r for r in rows if name in (r["name"], r["native_id"])]
        if len(matches) != 1:
            return _empty(context, "operator", message="指定对象在本发布资料中不能唯一匹配，请补充完整来源名称。")
        selected.append(matches[0])
    if query.intent == "catalog":
        answer = qa._Answer(context.session, context.release, context.original_question, context.store, plan)
        all_matches = selected or rows
        limit, offset = _catalog_pagination(query)
        chosen = all_matches[offset:offset + limit]
        if not chosen:
            return _empty(context, "operator", "unsupported", "本发布没有满足这些来源筛选条件的干员条目。")
        # This is a catalog query, not a comparison or an inferred recommendation.
        # All rows are source-resolved and each displayed field has its own proof.
        qa._overview(answer, chosen, plan)
        answer.result["tool_trace"].append(selector_replay)
        answer.result["module"] = "operator"
        answer.result["catalog"] = {
            "rarity": query.rarity, "count": len(chosen), "total_count": len(all_matches),
            "offset": offset, "limit": limit,
            "items": [{"native_id": r["native_id"], "name": r["name"]} for r in chosen],
            "complete_for_published_scope": offset == 0 and len(chosen) == len(all_matches),
            "has_more": offset + len(chosen) < len(all_matches),
        }
        answer.result["answer"] = f"本发布匹配{len(all_matches)}条，本页展示{len(chosen)}条：\n" + answer.result["answer"]
        return answer.result
    plan["operator_ids"] = [r["native_id"] for r in selected]
    result = qa.answer_question(
        context.session, context.release, context.original_question,
        store=context.store, plan=plan, structured_query=True,
        include_blackboard=query.include_blackboard,
    )
    result["module"] = "operator"
    result["tool_trace"].append(selector_replay)
    return result


def _recruitment_catalog(context, query):
    from arknights_agent import recruitment

    # Reuse the reviewed full-pool extractor and its byte replay. The baseline
    # computation is only an internal integrity check, never the catalog answer.
    checked = recruitment.calculate(context.session, context.release, [], 540, store=context.store)
    entry = context.release.manifest["datasets"]["recruitment"]
    records = list(context.session.scalars(select(KnowledgeRecord).where(
        KnowledgeRecord.dataset_id == entry["dataset_id"], KnowledgeRecord.module == "recruitment"
    ).order_by(KnowledgeRecord.kind, KnowledgeRecord.native_id)))
    members = [r for r in records if r.kind == "recruitment_member"]
    if query.rarity is not None:
        members = [r for r in members if r.payload["rarity_stars"] == query.rarity]
    if query.entity_names:
        matches = []
        for name in query.entity_names:
            found = [r for r in members if name in (r.name, r.native_id)]
            if len(found) != 1:
                return _empty(context, "recruitment", message="指定干员不在本发布公招池中或不能唯一匹配。")
            matches.extend(found)
        members = matches
    all_members = members
    limit, offset = _catalog_pagination(query)
    members = all_members[offset:offset + limit]
    tag_names = sorted({name for r in all_members for name in r.payload["tag_names"]})
    tags = [r for r in records if r.kind == "recruitment_tag" and r.name in tag_names]
    special = next(r for r in records if r.native_id == "specialTagRarityTable")
    rare_ids = {
        int(tag_id) for tag_id, stars in special.payload.items()
        if query.rarity is not None and stars == [query.rarity - 1]
    }
    rare_tags = [r.name for r in tags if r.payload["tagId"] in rare_ids]
    cited_ids = {r.id for r in [*all_members, *tags, special]}
    # The published membership and UI mapping are part of every derived tag
    # association, so retain their already replayed provenance as well.
    cited_ids.update(checked["calculation_inputs"]["rule_record_ids"].values())
    citations = [c for c in checked["citations"] if c["record_id"] in cited_ids]
    evidence = {}
    for citation in citations:
        evidence.setdefault(citation["record_id"], []).append(citation["evidence_id"])
    claims = []
    for record in all_members:
        claims.append({
            "id": f"f{len(claims) + 1}", "record_id": record.id,
            "label": "公招池干员与来源标签", "value": {
                "name": record.name, "rarity_stars": record.payload["rarity_stars"],
                "tag_names": record.payload["tag_names"],
            }, "evidence_ids": evidence[record.id],
            "derived": {"operation": "reviewed_recruitment_selector_mapping"},
        })
    for record in tags:
        claims.append({"id": f"f{len(claims) + 1}", "record_id": record.id,
                       "label": "公招可选标签", "value": record.name,
                       "evidence_ids": evidence[record.id]})
    scope = f"{query.rarity}星" if query.rarity else "当前发布公招池"
    lines = [f"{scope}相关词条（固定来源修订）："]
    if rare_tags:
        lines.append("对应稀有标签：" + "、".join(rare_tags) + "。")
    lines.append("相关可选标签：" + ("、".join(tag_names) or "本发布没有匹配条目") + "。")
    lines.append(f"本发布公招池匹配{len(all_members)}名干员，以下本页展示{len(members)}名。")
    lines.extend(f"- {r.name}：{r.payload['rarity_stars']}星；" + "、".join(r.payload["tag_names"]) for r in members)
    lines.append("以上是公招池条目与标签关联；普通标签并不单独保证该星级。本轮未计算概率或掉标签结果。")
    if len(query.entity_names) == 1 and len(members) == 1:
        member = members[0]
        names = "、".join(member.payload["tag_names"])
        lines = [f"**{member.name}**在本发布的公招池中，是{member.payload['rarity_stars']}星干员。"
                 f"公招标签：{names}。"]
    return {
        "question": context.original_question, "module": "recruitment",
        "status": "answered" if all_members else "unsupported",
        "answer": "\n".join(lines), "release_id": context.release.id,
        "manifest_hash": context.release.manifest_hash, "version": checked["version"],
        "coverage": checked["coverage"], "claims": claims, "citations": citations,
        "sources": checked["sources"], "tool_trace": [{"tool": "recruitment_source_catalog",
            "full_pool_and_mapping_replayed": True, "probabilities_supported": False}],
        "catalog": {"rarity": query.rarity, "count": len(members), "total_count": len(all_members),
                    "offset": offset, "limit": limit,
                    "complete_for_published_scope": offset == 0 and len(members) == len(all_members),
                    "has_more": offset + len(members) < len(all_members), "tags": tag_names,
                    "rarity_specific_tags": rare_tags,
                    "members": [{"name": r.name, "native_id": r.native_id,
                                 "rarity_stars": r.payload["rarity_stars"],
                                 "tag_names": r.payload["tag_names"]} for r in members]},
    }


def _recruitment_tag_overview(context, query):
    from arknights_agent.recruitment import calculate

    duration = query.duration_minutes if query.duration_minutes is not None else 540
    result = calculate(context.session, context.release, [], duration, store=context.store,
                       single_tag_overview=True)
    tags = [{key: row[key] for key in ('selected_tags', 'candidate_count',
             'minimum_rarity_if_tags_retained', 'condition', 'ordinary_tags_may_drop',
             'special_tags_protected_at_this_duration')} for row in result['combinations']]
    threshold = query.rarity or 1
    matched = [row for row in tags if (row['minimum_rarity_if_tags_retained'] or 0) >= threshold]
    result['tag_overview'] = {
        'scope': 'all_selectable_single_tags', 'duration_minutes': duration,
        'duration_basis': 'explicit_model_parameter' if query.duration_minutes is not None
                          else 'disclosed_9_hour_reference_not_user_supplied',
        'minimum_rarity_threshold': query.rarity, 'tags': tags,
        'matching_tags': [row['selected_tags'][0] for row in matched],
        'complete_for_published_scope': True, 'multi_tag_combinations_included': False,
    }
    lines = [f'以**{duration // 60}小时{duration % 60}分钟**、**所选标签保留**为条件，单标签保底如下：']
    for stars in sorted({row['minimum_rarity_if_tags_retained'] for row in matched}):
        names = '、'.join(row['selected_tags'][0] for row in matched
                         if row['minimum_rarity_if_tags_retained'] == stars)
        lines.append(f'- 至少**{stars}星**：{names}。')
    if not matched:
        lines.append('本次固定来源中没有符合此最低星级条件的单标签。')
    lines.append('普通标签可能被划掉；这里列的是单标签结果，不包含多标签组合，也不代表抽取概率。')
    result.update(question=context.original_question, module='recruitment', answer='\n'.join(lines),
                  answer_scope={'kind': 'single_tag_guarantee_overview',
                                'conditional_on_tags_retained': True})
    return result


def _recruitment(context, query):
    if query.intent == "catalog":
        return _recruitment_catalog(context, query)
    if query.intent == 'recruitment_tag_overview':
        return _recruitment_tag_overview(context, query)
    if query.tags is None or query.duration_minutes is None:
        return _empty(context, "recruitment", message="请提供公招标签和招募时长；无标签计算可明确使用空标签列表。")
    from arknights_agent.recruitment import calculate

    result = calculate(context.session, context.release, query.tags, query.duration_minutes, store=context.store)
    from arknights_agent.recruitment_display import project_recruitment_display

    scope = query.recruitment_scope or "all_combinations"
    if scope == "exact_selected_tags" and len(result["input"]["normalized_tags"]) > 3:
        raise SourceQueryRepair(
            ["tags", "recruitment_scope"], "exact_selected_tags_exceeds_selectable_limit",
            "当前来源计算最多同时选择3个标签。请按真实请求更正所选标签或选择all_combinations展示合法组合；"
            "不能截断用户标签、伪造空交集或由服务器改选范围。",
        )
    result["recruitment_display"] = project_recruitment_display(result, scope)
    result.update(question=context.original_question, module="recruitment")
    result["answer_scope"] = {
        "kind": "tag_combination_candidates",
        "can_determine_pool_membership_by_absence": False,
        "named_operator_lookup": {"intent": "catalog", "selector": "entity_names"},
    }
    result["answer"] = agent.render_recruitment_answer(result, scope=scope)
    result["tool_trace"] = result.get("trace", [])
    result["tool_trace"].append({
        "tool": "recruitment_source_display_projection", "scope": scope,
        "selected_tag_ids": result["recruitment_display"]["selected_tag_ids"],
        "complete_combinations_unchanged": True,
        **result["recruitment_display"]["proof"],
    })
    if query.rarity is not None:
        focused = []
        lines = [f"关注{query.rarity}星的来源候选视图（完整组合与原最低星级另行完整保留）："]
        displayed_ids = {tuple(row["selected_tag_ids"]) for row in result["recruitment_display"]["combinations"]}
        for combination in result["combinations"]:
            matches = [candidate for candidate in combination["candidates"]
                       if candidate["rarity_stars"] == query.rarity]
            focused.append({
                "selected_tags": list(combination["selected_tags"]),
                "original_minimum_rarity_if_tags_retained": combination["minimum_rarity_if_tags_retained"],
                "matching_candidate_count": len(matches),
                "matching_candidates": matches,
                "evidence_ids": list(combination["evidence_ids"]),
            })
            if tuple(combination["selected_tag_ids"]) in displayed_ids:
                label = " + ".join(combination["selected_tags"]) or "无标签"
                names = "、".join(candidate["name"] for candidate in matches) or "无该星级候选"
                lines.append(f"- {label}：{names}。")
        lines.append("关注星级只提供候选视图，不把完整组合筛成保证该星级；仍以所选标签保留为条件，不代表概率或必出结果。")
        result["requested_focus"] = {
            "rarity_stars": query.rarity,
            "operation": "source_candidate_rarity_view_without_modifying_combinations",
            "combinations_preserved": True, "guarantee_or_probability": False,
            "combinations": focused,
        }
        result["answer"] += "\n\n" + "\n".join(lines)
        result["tool_trace"].append({"tool": "recruitment_source_rarity_focus",
            "rarity_stars": query.rarity, "complete_combinations_unchanged": True,
            "source": "already_replayed_calculator_candidates"})
    return result


def _garrison(context, query):
    from arknights_agent import garrison
    from arknights_agent.ingestion.sample import ACTIVITY_ID

    answer = garrison._Answer(context.session, context.release, context.original_question, context.store)
    coverage = answer.result["coverage"]
    from arknights_agent.garrison_regulation import answer_regulation, selects_regulation

    if selects_regulation(answer, query):
        return answer_regulation(answer, query)
    if query.activity_id not in (None, ACTIVITY_ID):
        return answer.refuse("指定活动不在本发布已核验范围。", "out_of_scope")
    if not coverage.get("metadata_qa_enabled") or coverage.get("mapping_status") != "partial_verified":
        return answer.refuse("本发布未完成卫戍阶段和来源映射核验。")
    if query.phase_id == "V3_1":
        return answer.refuse("该字段属于历史盟约查询，规程序列 I 请使用规程资料查询。", "out_of_scope")
    if query.intent in {"garrison_field", "garrison_document"}:
        if query.phase_id is None or query.field_key is None:
            return answer.refuse("请指定来源阶段与已核验字段。", "needs_clarification")
        from arknights_agent.garrison_expansion import _field_answer
        from arknights_agent.garrison_expansion_policy import EXPANSION_FIELD_SPECS
        from arknights_agent.garrison_mechanics import _description_answer
        from arknights_agent.garrison_mechanics_policy import DESCRIPTION_SPECS
        from arknights_agent.garrison_policy import FIELD_SPECS

        spec_key = f"{query.phase_id}:{query.field_key}"
        if query.intent == "garrison_field":
            if spec_key in FIELD_SPECS:
                return garrison._verified_field_answer(answer, query.phase_id, query.field_key)
            if spec_key in EXPANSION_FIELD_SPECS:
                return _field_answer(answer, query.phase_id, query.field_key)
            return answer.refuse("本发布没有该阶段已核验的独立标量字段。")
        spec = DESCRIPTION_SPECS.get(spec_key)
        if (
            spec is None or not coverage.get("documented_mechanics_qa_enabled")
            or spec_key not in coverage.get("documented_mechanics_fields", [])
        ):
            return answer.refuse("本发布没有该阶段已核验的原始描述包。")
        if context.store is None:
            return answer.refuse("原始描述需要重放来源快照；本次未提供快照存储。")
        return _description_answer(answer, query.phase_id, spec_key, spec)
    phases = {r["native_id"]: r for r in _records(context, "garrison", "verified_phase")}
    if set(phases) != set(coverage.get("verified_phase_ids", [])):
        raise Conflict("garrison metadata coverage differs from source records")
    if query.phase_id is not None and query.phase_id not in phases:
        return answer.refuse("指定阶段没有本发布核验记录。", "out_of_scope")
    announcements = {r["native_id"]: r for r in _records(context, "garrison", "official_phase_announcement")}
    selected = [phases[query.phase_id]] if query.phase_id else list(phases.values())
    activities = _records(context, "garrison", "verified_activity")
    if len(activities) != 1 or activities[0]["native_id"] != ACTIVITY_ID:
        raise Conflict("garrison metadata lacks its unique published activity")
    activity = activities[0]
    lines = [f"活动：{ACTIVITY_ID}，保留历史客户端配置与阶段公告。"]
    for label, field, source_field in (
        ("活动配置开始", "client_start_time", "startTime"),
        ("活动配置结束", "client_end_time", "endTime"),
        ("奖励配置截止", "client_reward_end_time", "rewardEndTime"),
    ):
        value = activity["data"][field]
        stamp = int(datetime.fromisoformat(value).timestamp())
        citation = answer.cite(activity, f"/{source_field}", stamp)
        answer.claim(label, value, [citation], "retained_client_configuration", {
            "operation": "unix_timestamp_to_iso8601", "timezone": "Asia/Shanghai",
        })
        lines.append(f"{label}：{value}（保留客户端配置）。")
    for phase in selected:
        article = phase["data"].get("official_announcement_record")
        if article not in announcements:
            raise Conflict("garrison phase is missing its official announcement")
        lines.append(garrison._phase_claims(answer, phase, announcements[article]))
    answer.result["answer"] = "\n".join(lines)
    return answer.result


def answer_source_query(context, query):
    """Dispatch typed selectors. Scope and source checks do not parse language."""
    from arknights_agent.runtime.dialogue_context import previous_questions

    actual_user_questions = [context.original_question, *previous_questions(context.previous_answer)]
    if any(not any(quote in question for question in actual_user_questions) for quote in query.unresolved):
        raise SourceQueryRepair(["unresolved"], "quote_not_in_verified_user_questions")
    selectors = query.model_dump()
    relevant = (
        set(selectors) if query.intent in {"clarify", "unsupported"}
        else SOURCE_QUERY_FIELDS[query.intent] | SOURCE_QUERY_COMMON_FIELDS
    )
    defaults = type(query)(module=query.module, intent=query.intent).model_dump()
    conflicts = [key for key, value in selectors.items() if key not in relevant and value != defaults[key]]
    if conflicts:
        # Refuse a typed parameter the selected source handler cannot preserve;
        # never silently drop a stale condition from a previous model proposal.
        raise SourceQueryRepair(
            conflicts, allowed_fields=sorted(relevant),
            instruction="按当前请求和字段适用范围更正intent或参数。仅不适用且未指定的可选字段可省略；"
            "不能丢弃真实条件，不可把非空布尔/数组改成null。",
        )
    if query.unresolved and query.intent not in {"clarify", "unsupported"}:
        # An executable intent and an unresolved restriction are inconsistent.
        # Ask the model to resolve that choice; never inspect the utterance or
        # discard an unknown condition to manufacture a successful query.
        raise SourceQueryRepair(
            ["intent", "unresolved"], "unresolved_requires_explicit_clarification",
            "可执行intent与unresolved冲突。请复核原问：普通请求或已由参数表达的内容不重复列为未知；"
            "若确有无法确定的对象或条件，必须保留其逐字引文并用intent=clarify；"
            "若缺少已核验能力用unsupported。不能仅为通过校验删除真实条件，服务器不会替你解释或改参。",
        )
    if query.intent in {"clarify", "unsupported"}:
        clarification = (
            "尚未确定以下对象或查询条件：" + "、".join(f"「{quote}」" for quote in query.unresolved)
            + "。请补充这些内容的具体含义或范围。"
            if query.unresolved else None
        )
        result = _empty(context, query.module, "unsupported" if query.intent == "unsupported" else "needs_clarification",
                        "本发布没有该问题所需的已核验来源或计算能力，无法给出可靠结论。" if query.intent == "unsupported" else clarification)
    elif query.intent not in SOURCE_QUERY_MODULE_INTENTS[query.module]:
        raise SourceQueryRepair(["module", "intent"], "intent_not_supported_by_module")
    else:
        result = {"operator": _operator, "recruitment": _recruitment, "garrison": _garrison}[query.module](context, query)
    if result.get("status") in {"answered", "computed"} and not result.get("citations"):
        raise Conflict("successful source query requires release-pinned evidence")
    result.setdefault("tool_trace", []).append({
        "tool": "model_source_query", "proposal": query.model_dump(),
        "original_question_preserved": True, "game_facts_origin": "pinned_server_sources",
        "semantic_parser": "model", "source_validation": "server",
    })
    from arknights_agent.runtime.dialogue_context import attach_state

    result["query_interpretation"] = {"origin": "model_query_proposal", "proposal": query.model_dump()}
    attach_state(result, context.original_question, context.previous_answer, context.previous_run_id)
    return result
