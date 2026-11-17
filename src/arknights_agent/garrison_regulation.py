"""Release-bound Regulation records for the model's typed source queries."""

from arknights_agent import services
from arknights_agent.errors import Conflict
from arknights_agent.ingestion.garrison_regulation import ACTIVITY, PHASE


def selects_regulation(answer, query):
    if query.intent in {"garrison_regulation_record", "garrison_bond_catalog", "garrison_regulation_catalog"}:
        return True
    if query.intent != "garrison_metadata":
        return False
    if query.activity_id or query.phase_id:
        return query.activity_id == ACTIVITY or query.phase_id == PHASE
    return bool(answer.result["scope"].get("latest_documented_season"))


def _metadata_rows(answer):
    rows = {}
    for kind in ("season", "activity", "notice"):
        for row in services.list_records(
            answer.session, answer.release, module="garrison", kind=f"regulation_{kind}",
        ):
            rows[(kind, row["native_id"])] = row
    keys = [
        ("season", PHASE), ("activity", ACTIVITY),
        *(("notice", name) for name in (
            "launch", "maintenance", "unlock", "currency", "season_structure",
            "open_period", "revision_period", "end_period", "content_update",
        )),
    ]
    if any(key not in rows for key in keys):
        raise Conflict("regulation metadata lacks required release-pinned evidence")
    return [rows[key] for key in keys]


def _selected_rows(answer, kind, names):
    selected = {}
    for offset in range(0, answer.result["coverage"]["regulation_sequence_1"]["record_counts"][kind], 200):
        for row in services.list_records(
            answer.session, answer.release, module="garrison", kind=kind, limit=200, offset=offset,
        ):
            for name in names:
                if name in (row["native_id"], row["name"]):
                    selected.setdefault(name, []).append(row)
    if any(len(selected.get(name, [])) != 1 for name in names):
        return None
    return list({selected[name][0]["id"]: selected[name][0] for name in names}.values())


def answer_regulation(answer, query):
    current = answer.result["scope"].get("latest_documented_season", {})
    published = answer.result["coverage"].get("regulation_sequence_1", {})
    if (current.get("activity_id"), current.get("phase_id"), published.get("phase_id")) != (ACTIVITY, PHASE, PHASE):
        return answer.refuse("本发布未收录规程序列 I。", "out_of_scope")
    if query.activity_id not in (None, ACTIVITY) or query.phase_id not in (None, PHASE):
        return answer.refuse("活动与阶段不属于同一份规程序列 I 来源。", "out_of_scope")
    if answer.store is None:
        return answer.refuse("规程资料需要重放固定原始来源。")
    if query.intent == "garrison_metadata":
        rows = _metadata_rows(answer)
    elif query.intent == "garrison_bond_catalog":
        expected = published["record_counts"].get("regulation_bond", 0)
        if not 1 <= expected <= 200:
            return answer.refuse("本发布的盟约目录无法在一次查询内完整核验。")
        rows = services.list_records(
            answer.session, answer.release, module="garrison", kind="regulation_bond", limit=200,
        )
        if len(rows) != expected:
            raise Conflict("regulation bond catalogue differs from the published count")
    elif query.intent == "garrison_regulation_catalog":
        kind = query.regulation_kind
        expected = published["record_counts"].get(kind)
        if not kind or not kind.startswith("regulation_") or type(expected) is not int or expected < 1:
            return answer.refuse("本发布没有这一类目录，请按收录类型更正查询。", "needs_clarification")
        limit, offset = query.limit or min(expected, 200), query.offset or 0
        rows = services.list_records(answer.session, answer.release, module="garrison",
                                     kind=kind, limit=limit, offset=offset)
        if len(rows) != min(limit, max(0, expected - offset)):
            raise Conflict("regulation catalogue differs from the published count")
    else:
        if query.regulation_kind not in published["record_counts"] or not query.entity_names:
            return answer.refuse("请先查规程目录，选择条目类型及来源中的名称或标识。", "needs_clarification")
        rows = _selected_rows(answer, query.regulation_kind, query.entity_names)
        if not rows:
            return answer.refuse("该规程目录中没有唯一匹配的条目，请核对候选。", "needs_clarification")
    answer.result.update(
        scope=current, coverage=published,
        limitations=[
            "官方公告确认赛季开启与各时期规则。实际开服分钟、后续更新时间和当前服务器实时状态未核验。",
            "客户端原始配置按 V3_1 保存。计划时间、预载和默认值不能证明服务器实际开放、结算或活动截止。",
        ],
    )
    answer.result["version"]["source_revision"] = current["client_revision"]
    lines = [current["title"]]
    catalog = []
    for row in rows:
        if row["parent_native_id"] != PHASE:
            raise Conflict("regulation record belongs to another season")
        raw = row["data"]["source_value"]
        ref = answer.cite(row, "", raw)
        authority = ("official_announcement" if row["kind"] == "regulation_notice"
                     else "retained_client_configuration")
        answer.claim(row["name"], raw, [ref], authority)
        if query.intent == "garrison_regulation_catalog":
            description = (raw.get("bandDesc") or raw.get("desc") or raw.get("description") or "") if isinstance(raw, dict) else raw
            catalog.append({"name": row["name"], "description": description if isinstance(description, str) else "",
                            "citation_id": ref})
        elif isinstance(raw, str) and row["kind"] == "regulation_notice":
            lines.append(raw)
        elif query.intent == "garrison_regulation_record":
            lines.append(f"{row['name']}\n{services.canonical_json(raw)}")
        elif query.intent == "garrison_bond_catalog":
            if (raw["name"] != row["name"] or raw["bondId"] != row["native_id"]
                    or type(raw["activeCount"]) is not int or not isinstance(raw["desc"], str)):
                raise Conflict("regulation bond identity or description differs")
            catalog.append({"name": raw["name"], "activation_count": raw["activeCount"],
                            "activation_scope": raw["activeCondition"], "description": raw["desc"],
                            "citation_id": ref})
    if query.intent == "garrison_regulation_catalog":
        has_more = offset + len(rows) < expected
        answer.result["catalog"] = {"items": catalog, "total_count": expected, "offset": offset,
            "has_more": has_more, "next_offset": offset + len(rows) if has_more else None,
            "complete_for_published_scope": offset == 0 and not has_more, "record_kind": kind}
        lines.append(f"本发布此类共 {expected} 条，本页 {len(rows)} 条。")
        lines.extend(f"{row['name']}：{row['description']}" for row in catalog)
    elif catalog:
        answer.result["bond_catalog"] = catalog
        lines.append(f"本期收录 {len(catalog)} 条盟约，含特殊条目。以下为客户端配置文案。")
        lines.extend(f"{row['name']}：{row['description']}" for row in catalog)
    answer.result["answer"] = "\n".join(lines)
    return answer.result
