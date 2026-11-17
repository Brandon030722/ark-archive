"""Evidence-bounded guide answers layered on top of raw client documents."""

import hashlib
import json
import re
from pathlib import Path

from arknights_agent import rag, services
from arknights_agent.errors import Conflict
from arknights_agent.ingestion.roguelike_wiki import KIND, policy
from arknights_agent.roguelike_sources import THEME_NAMES
from arknights_agent.wiki_text import parameters, plain, scenes, templates


def _norm(text):
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    return re.sub(r"\s+", "", plain(text)).replace("……", "......")


def _add_passage(answer, page, start, end, label):
    c = rag.citation(page, start, end, f"c{len(answer.result['citations']) + 1}")
    answer.result["citations"].append(c)
    answer.result["claims"].append(
        {
            "id": f"f{len(answer.result['claims']) + 1}",
            "record_id": page["record"]["id"],
            "label": label,
            "value": c["field_value"],
            "citation_ids": [c["id"]],
            "verification_status": "fixed_community_wiki_passage",
        }
    )
    return c["id"]


def _event_note_text(kind, source_text):
    if kind in {"shared_intro", "reviewed_event_context"}:
        return plain(source_text)
    _, positional, fields = parameters(source_text)
    if kind == "appearance_floor":
        if not fields.get("floor"):
            raise Conflict("event floor note is absent from the source scene")
        return plain(fields["floor"])
    if kind == "information_note" and positional and positional[0] == "desc":
        parts = [plain(fields.get(key, "")) for key in ("desc1", "desc2") if fields.get(key)]
        if fields.get("dest"):
            parts.append("原文关联场景编号：" + fields["dest"])
        if fields.get("subChoose"):
            parts.append("原文分支：" + fields["subChoose"])
        return "；".join(parts)
    raise Conflict("unsupported source event context note")


def _event_context_note(kind, page, start, end, citation_id):
    raw = page["text"][start:end]
    if not raw or not 0 <= start < end <= len(page["text"]):
        raise Conflict("event context is outside its verified source page")
    return {"kind": kind, "text": _event_note_text(kind, raw), "source_text": raw,
            "source_start": start, "source_end": end, "citation_ids": [citation_id],
            "rendering": "literal_source_with_display_markup_removed"}


def _finish(answer, theme, text, kind):
    answer.result.update(answer=text, theme_id=theme)
    answer.result["version"]["theme_id"] = theme
    answer.result["conditions"]["guide_kind"] = kind
    answer.result["tool_trace"].append(
        {
            "tool": "roguelike_source_document",
            "release_id": answer.release.id,
            "theme_id": theme,
            "method": "verified_wiki_passage_retrieval",
            "runtime_calculation": False,
        }
    )
    answer.result["limitations"] = [
        "路线和菜单来自固定修订的PRTS社区资料，客户端配置另行引用；不保证当前线上状态。",
        "随机选择、随机奖励不意味着等概率；没有可靠概率证据时不计算或编造数值。",
    ]
    links = {c.get("page_url") for c in answer.result["citations"] if c.get("page_url")}
    if links:
        answer.result["answer"] += "\n资料：" + "；".join(
            f"[PRTS固定修订]({u})" for u in sorted(links)
        )
    return answer.result


def _render_route_passage(answer, page, quote):
    # Resolve only literal source-owned display variables, never execute Lua.
    for variable in set(re.findall(r"\{\{#var:([a-zA-Z0-9_]+)\}\}", quote)):
        definitions = templates(page["text"], "#vardefine:" + variable)
        if len(definitions) != 1:
            continue
        a, b, raw = definitions[0]
        _, args, named = parameters(raw)
        if len(args) != 1 or named:
            continue
        value = plain(args[0])
        _add_passage(answer, page, a, b, "路线显示变量原文")
        quote = quote.replace("{{#var:" + variable + "}}", value)
    return plain(quote)


def _legacy_options(raw):
    result = []
    blocks = templates(raw, "集成战略事件/选项")
    for a, b, block in blocks:
        if any(x < a and b <= y for x, y, _ in blocks):
            continue
        _, _, fields = parameters(block)
        result.append(
            {
                "label": plain(fields.get("行动", "说明")),
                "reward_and_cost": plain(fields.get("选项", "未提供数值说明")),
                "condition": plain(fields.get("条件", "")),
                "destination": None,
                "branches": None,
                "source_details": {
                    k: plain(v) for k, v in fields.items() if k in ("收藏品", "道具", "条件")
                },
                "linked_entries": re.findall(r"\[\[([^|\]\n]+)", fields.get("选项", "")),
                "outcomes": {k: plain(v) for k, v in fields.items() if k.startswith("结果")},
                "child_options": _legacy_options(fields.get("子选项", "")),
                "raw": block,
            }
        )
    return result


def guide_spec(scope):
    expected = scope.get("guide_policy_sha256")
    if not expected:
        return policy()
    raw = (Path(__file__).parent / "data" / "roguelike_guides_v2.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise Conflict("published guide policy differs")
    return json.loads(raw)


def _option_lines(options, prefix="", *, start_index=1):
    lines = []
    for i, opt in enumerate(options, start_index):
        lines.append(
            f"{prefix}{i}. {opt['label']}：{opt['reward_and_cost']}"
            + ("；条件：" + opt["condition"] if opt["condition"] else "")
            + ("；后续场景编号：" + str(opt["destination"]) if opt.get("destination") else "")
            + ("；分支（名称;场景编号）：" + str(opt["branches"]) if opt.get("branches") else "")
        )
        lines.extend(_option_details_lines(opt, prefix))
    return lines


def _option_details_lines(option, prefix=""):
    """Show source-backed meaning while retaining IDs only in the audit."""
    lines = []
    details = option.get("source_details", {})
    if details.get("条件") and details["条件"] != option.get("condition"):
        lines.append(prefix + "   补充条件：" + details["条件"])
    if details.get("道具"):
        lines.append(prefix + "   原文道具说明：" + details["道具"])
    described = option.get("reward_and_cost", "") + option.get("condition", "")
    rewards = [name for name in option.get("reward_items", []) if name not in described]
    if rewards:
        lines.append(prefix + "   对应藏品：" + "、".join(rewards))
    elif option.get("reward_mapping_status") == "needs_verification":
        lines.append(prefix + "   原文藏品标识未能唯一对应图鉴名称，待核验。")
    related = [name for name in option.get("linked_entries", [])
               if "/" not in name and "#" not in name and name not in described]
    if related:
        lines.append(prefix + "   关联条目：" + "；".join(related))
    lines.extend(prefix + "   结果：" + value for value in option.get("outcomes", {}).values())
    if option.get("child_options"):
        lines.append(prefix + "   选择后才出现：")
        lines.extend(_option_lines(option["child_options"], prefix + "   "))
    return lines


def render_selected_option(result, number):
    """Render one already verified choice, without reinterpreting a question."""
    options = result.get("event_menu", {}).get("options", [])
    selected = result.get("selected_option")
    if (type(number) is not int or not 1 <= number <= len(options)
            or not isinstance(selected, dict) or selected.get("number") != number
            or not isinstance(selected.get("raw"), str)
            or any(not services.same_json_value(selected.get(key), value)
                   for key, value in options[number - 1].items())):
        raise Conflict("selected option differs from its verified primary menu")
    citations = {citation["id"]: citation for citation in result.get("citations", [])}
    if (not selected.get("citation_ids")
            or any(ident not in citations for ident in selected["citation_ids"])):
        raise Conflict("selected option has no verified source citation")
    lines = [f"第{number}个按钮「{selected['label']}」", "原文奖励与代价：" + selected["reward_and_cost"]]
    lines.append("该选项的条件：" + selected["condition"] if selected.get("condition")
                 else ("资料未列专属条件，仍须满足下面的共同条件。"
                       if result.get("event_context_notes") else "资料未列专属条件。"))
    lines.append("资料未另列额外代价，无法据此确认是否还需消耗其他资源。")
    if selected.get("destination"):
        lines.append("后续场景编号：" + str(selected["destination"]))
    if selected.get("branches"):
        lines.append("后续分支：" + str(selected["branches"]))
    lines.extend(_option_details_lines(selected))
    notes = result.get("event_context_notes", [])
    if not isinstance(notes, list):
        raise Conflict("event context notes are not a source-backed list")
    if notes:
        lines.append("共同条件与说明（资料原文）：")
    for note in notes:
        if (not isinstance(note, dict) or not isinstance(note.get("text"), str)
                or not isinstance(note.get("source_text"), str) or not note["source_text"]
                or not note.get("citation_ids")
                or any(ident not in citations for ident in note["citation_ids"])
                or note["text"] != _event_note_text(note.get("kind"), note["source_text"])
                or not any(isinstance(citations[ident].get("field_value"), str)
                           and type(note.get("source_start")) is int
                           and type(note.get("source_end")) is int
                           and type(citations[ident].get("start")) is int
                           and citations[ident]["start"] <= note["source_start"] < note["source_end"] <= citations[ident]["end"]
                           and citations[ident]["field_value"][note["source_start"] - citations[ident]["start"]:
                                                               note["source_end"] - citations[ident]["start"]] == note["source_text"]
                           for ident in note["citation_ids"])):
            raise Conflict("event context note differs from its existing source citation")
        label = "资料所列出现层数：" if note.get("kind") == "appearance_floor" else ""
        lines.append(label + note["text"])
    lines.append("编号沿用资料菜单；未据此推断当前局内状态或随机结果。")
    links = {citation.get("page_url") for citation in citations.values() if citation.get("page_url")}
    if links:
        lines.append("资料：" + "；".join(f"[PRTS固定修订]({url})" for url in sorted(links)))
    return "\n".join(lines)


def _menu_lines(scene):
    """Render source order without turning explanatory notes into buttons."""
    if "menu_sequence" not in scene:
        return _option_lines(scene["options"])
    lines = []
    for item in scene["menu_sequence"]:
        if item["kind"] == "option":
            index = item["index"]
            lines.extend(_option_lines([scene["options"][index]], start_index=index + 1))
        else:
            note = scene["information_notes"][item["index"]]
            line = "说明（原文，非可选按钮）：" + note["text"]
            if note.get("additional_text"):
                line += "；" + note["additional_text"]
            if note.get("destination"):
                line += "；原文关联场景编号：" + str(note["destination"])
            if note.get("branches"):
                line += "；原文分支：" + str(note["branches"])
            lines.append(line)
    return lines


def _resolve_event_rewards(answer, source_pages, theme, options, cited=None):
    """Name only explicit relic identities, with one unique same-theme source."""
    cited = {} if cited is None else cited
    for opt in options:
        details = opt.get("source_details", {})
        ident, icon = details.get("收藏品"), details.get("iconId")
        if (ident and re.fullmatch(r"\d+", ident)) or icon:
            found = []
            for source in source_pages:
                if (
                    source["pin"]["category"] != "relics"
                    or source["pin"].get("theme_id", theme) != theme
                ):
                    continue
                for a, b, raw in templates(source["text"], "收藏品"):
                    fields = parameters(raw)[2]
                    if ident and fields.get("ID") != ident:
                        continue
                    if icon and fields.get("iconId") != icon:
                        continue
                    if fields.get("名称"):
                        found.append((source, a, b, plain(fields["名称"])))
            if len(found) == 1:
                source, a, b, name = found[0]
                opt["reward_items"] = [name]
                key = (source["pin"].get("revision"), a, b)
                if key not in cited:
                    label = "事件藏品标识对应名称" if icon else "事件收藏品编号对应名称"
                    cited[key] = _add_passage(answer, source, a, b, label)
            else:
                opt["reward_mapping_status"] = "needs_verification"
        _resolve_event_rewards(answer, source_pages, theme, opt.get("child_options", []), cited)
    return list(cited.values())


def _acquisition_method(page, start, end, fields):
    """Resolve literal ditto fields only across an explicit adjacent relic sequence."""
    method = fields.get("获取") or fields.get("获取方式")
    if not method or plain(method) != "同上":
        return plain(method) if method else None, []
    if len({plain(fields[k]) for k in ("获取", "获取方式") if fields.get(k)}) != 1:
        return None, []
    entries = [(a, b, parameters(raw)[2]) for a, b, raw in templates(page["text"], "收藏品")]
    index = next((i for i, (a, b, _) in enumerate(entries) if (a, b) == (start, end)), None)
    references = []
    while index is not None and index > 0:
        a, _, current = entries[index]
        previous_start, previous_end, previous = entries[index - 1]
        current_id, previous_id = current.get("ID", ""), previous.get("ID", "")
        # A heading, prose, another template or a missing number breaks ownership.
        # Never search back to the nearest populated field across such a break.
        gap = re.sub(r"<!--.*?-->", "", page["text"][previous_end:a], flags=re.S)
        if (
            previous_end > a
            or gap.strip()
            or not current.get("主题")
            or current["主题"] != previous.get("主题")
            or not re.fullmatch(r"\d+", current_id)
            or not re.fullmatch(r"\d+", previous_id)
            or int(current_id) != int(previous_id) + 1
            or any(
                sum(f.get("ID") == ident for _, _, f in entries) != 1
                for ident in (current_id, previous_id)
            )
        ):
            break
        values = [previous[k] for k in ("获取", "获取方式") if previous.get(k)]
        if not values or len({plain(value) for value in values}) != 1:
            break
        references.append((previous_start, previous_end, previous))
        method = plain(values[0])
        if method != "同上":
            return method, references
        index -= 1
    # An unresolved cross-reference is not itself a usable acquisition method.
    return None, []


def _acquisition(answer, record, source_pages, theme):
    found = []
    for page in source_pages:
        if page["pin"]["category"] != "relics" or page["pin"].get("theme_id", theme) != theme:
            continue
        for a, b, raw in templates(page["text"], "收藏品"):
            _, _, fields = parameters(raw)
            identity = fields.get("iconId") or (
                "relic_" + fields.get("ID", "") if theme == "act12d6" else None
            )
            if not identity or identity != record["data"].get("iconId"):
                continue
            # Same icon variants need an exact name as well, never title-only joins.
            if plain(fields.get("名称", "")) != record["name"]:
                continue
            found.append((page, a, b, fields))
    if len(found) != 1:
        return answer.refuse(
            "固定图鉴未能唯一对应此藏品的获取说明，不能借用其他同名藏品。", "needs_verification"
        )
    page, a, b, fields = found[0]
    source_method = fields.get("获取") or fields.get("获取方式")
    acquisition, references = _acquisition_method(page, a, b, fields)
    answer.emit(record)
    cid = _add_passage(answer, page, a, b, "藏品获取和图鉴解锁原文")
    cids = [cid] + [
        _add_passage(answer, page, start, end, "藏品获取“同上”的前项原文")
        for start, end, _ in references
    ]
    unlock = plain(fields["条件"]) if fields.get("条件") else None
    answer.result["acquisition"] = {
        "method": acquisition,
        "unlock_condition": unlock,
        "probability": None,
        "exhaustive": False,
        "citation_ids": cids,
    }
    if source_method and plain(source_method) == "同上":
        answer.result["acquisition"]["method_reference"] = {
            "literal": "同上",
            "status": "resolved" if references else "needs_verification",
            "antecedent_id": references[-1][2].get("ID") if references else None,
            "antecedent_name": plain(references[-1][2].get("名称", "")) if references else None,
            "citation_ids": cids,
        }
    lines = [f"{THEME_NAMES[theme]} · {record['name']}"]
    if acquisition:
        lines.append("资料记载的本局获取方式：" + acquisition)
        if references:
            lines.append(
                "获取字段原文为“同上”；依据同页连续编号藏品的前项说明展开，引用保留完整链条。"
            )
    else:
        answer.result["status"] = "needs_verification"
        lines.append(
            "本局获取方式：固定资料的“同上”未能唯一解析，不能推断具体途径。"
            if source_method and plain(source_method) == "同上"
            else "本局获取方式：固定资料未明确记载，不能从售价、效果或图鉴解锁条件推断。"
        )
    lines.append("图鉴解锁条件（不等于本局立即获得）：" + (unlock or "该条目未记载"))
    lines.append(
        "以上只列资料明确记录的途径，不保证穷尽。具体掉落概率未知；“随机”不代表必得或等概率。"
    )
    return _finish(answer, theme, "\n".join(lines), "relic_acquisition")


def _menu(page, record):
    text = page["text"]
    data = record["data"]
    desc = _norm(data.get("description", ""))
    matches = [
        s
        for s in scenes(text)
        if s["title"] == record["name"]
        and _norm(s.get("raw_description", s["description"])) == desc
    ]
    if len(matches) == 1:
        current = matches[0]
        # Only scenes in the same explicit wiki event parameter belong together.
        starts = [m.start() for m in re.finditer(r"(?m)^\|事件[^=\n]+=", text)]
        preceding = [s for s in starts if s <= current["start"]]
        if preceding:
            start = preceding[-1]
            end = next((s for s in starts if s > current["start"]), len(text))
        else:
            # Older ISEvent pages use one outer template per event. Bound by
            # its closing braces, never group another same-name event.
            owners = [
                (a, b) for a, b, raw in templates(text, "ISEvent") if a <= current["start"] < b
            ]
            start, end = (
                min(owners, key=lambda ab: ab[1] - ab[0])
                if owners
                else (current["start"], current["end"])
            )
        follow = [s for s in scenes(text[start:end]) if s["title"] == record["name"]]
        return start, end, current, follow
    # Old template format owns options directly, description must match.
    old = []
    for a, b, raw in templates(text, "集成战略事件"):
        _, _, fields = parameters(raw)
        if _norm(fields.get("描述", "")) == desc:
            old.append((a, b, raw))
    if len(old) == 1:
        a, b, raw = old[0]
        # Action bonuses' prerequisites and random subset are adjacent prose.
        if record["name"] == "行动奖励":
            heading = text.rfind("'''", 0, a - 4)
            a = max(text.rfind("若上一次", 0, a), heading) if "若上一次" in text[:a] else a
        current = {
            "scene_label": "开始",
            "options": _legacy_options(raw),
            "floor": None,
            "event_type": None,
        }
        return a, b, current, []
    # IS3 action-bonus Lua list has explicit source-owned rows.
    if record["name"] == "行动奖励" and record["parent_native_id"] == "rogue_2":
        for a, b, raw in templates(text, "#invoke:ISEvent"):
            _, pos, fields = parameters(raw)
            if fields.get("eventId") != "choice_ro2_startbuff":
                continue
            options = []
            for line in fields["item1"].splitlines():
                cells = line.split(";;")
                if len(cells) < 4:
                    raise Conflict("wiki start bonus row is incomplete")
                options.append(
                    {
                        "label": plain(cells[1]),
                        "reward_and_cost": plain(cells[3]),
                        "condition": "",
                        "destination": None,
                        "branches": None,
                        "raw": line,
                    }
                )
            return (
                text.rfind("若上一次", 0, a),
                b,
                {"scene_label": "开始", "options": options, "floor": None, "event_type": None},
                [],
            )
    return None


def try_guide(answer, catalog, question):
    """Only published wiki releases opt in. Old release behavior stays intact."""
    if not answer.entry.get("scope", {}).get("wiki_revisions"):
        return None
    from arknights_agent.roguelike import ALIASES, _quoted_literals, _strip_query

    themes = {t for t, n in THEME_NAMES.items() if t in question or n in question}
    if not themes:
        themes = {t for a, t in ALIASES.items() if a in question}
    if len(themes) != 1:
        return None
    theme = next(iter(themes))
    literals = [next(v for v in m.groups() if v is not None) for m in _quoted_literals(question)]
    rows = [r for r in catalog if r["parent_native_id"] == theme and r["kind"] != KIND]
    if len(literals) == 1:
        target = literals[0]
        matches = [r for r in rows if target in (r["name"], r["native_id"], r["source_id"])]
    elif not literals:
        values = sorted({r["name"] for r in rows if r["name"] in question}, key=len, reverse=True)
        if not values:
            return None
        target = values[0]
        matches = [r for r in rows if r["name"] == target]
    else:
        return None
    ending = (
        bool(re.search(r"路线|触发条件|如何解锁|怎么解锁|怎样解锁|攻略", question))
        and "结局" in question
    )
    menu = bool(re.search(r"按钮|选项|奖励|菜单", question)) and "事件" in question
    acquisition = (
        bool(re.search(r"获取|获得|哪里拿|怎么拿|哪里得", question)) and "藏品" in question
    )
    expanded = bool(answer.entry.get("scope", {}).get("guide_policy_sha256"))
    if acquisition and not expanded:
        return None
    probability = bool(re.search(r"概率|掉率|几率", question)) and "藏品" in question
    kind = (
        "ending"
        if ending
        else "event"
        if "事件" in question
        else "relic"
        if probability or acquisition
        else None
    )
    if not kind:
        return None
    matches = [r for r in matches if r["kind"] == "roguelike_" + kind]
    semantic = question.replace(target, "")
    for value in [theme, THEME_NAMES[theme], *[a for a, t in ALIASES.items() if t == theme]]:
        semantic = semantic.replace(value, "")
    # Never erase arbitrary conditions or a second question to obtain a route.
    extra = (
        "获取方式",
        "获取途径",
        "怎么获取",
        "如何获取",
        "怎么获得",
        "如何获得",
        "哪里拿",
        "怎么拿",
        "哪里得",
        "获取",
        "获得",
        "本局",
        "图鉴解锁条件",
        "解锁路线",
        "触发条件",
        "可选按钮",
        "按钮",
        "奖励",
        "菜单",
        "掉落概率",
        "掉率",
        "概率",
        "几率",
        "多少",
        "路线",
        "攻略",
        "如何解锁",
        "怎么解锁",
        "怎样解锁",
        "可选",
        "选项",
        "是多少",
    )
    if _strip_query(semantic, extra):
        return None
    if not matches:
        return None
    answer.verify_catalog(theme, kind, rows)
    if len(matches) > 1:
        if kind != "event":
            return None
        choices = []
        lines = [f"找到 {len(matches)} 个同名场景，请按描述选择："]
        for i, row in enumerate(matches, 1):
            record = answer.record(row["id"])
            data = record["data"]
            cid = answer.cite(record, "", data)
            next_q = (
                f"肉鸽{theme}，事件「{record['native_id']}」的所有可选按钮和完整奖励是什么？"
                if menu
                else f"肉鸽{theme}，事件「{record['native_id']}」的完整原始配置是什么？"
            )
            choices.append(
                {
                    "number": i,
                    "name": record["name"],
                    "native_id": record["native_id"],
                    "description": data.get("description", ""),
                    "question": next_q,
                    "citation_ids": [cid],
                }
            )
            lines.append(f"{i}. {plain(data.get('description', '无描述'))}\n   查询：{next_q}")
        answer.result.update(status="needs_clarification", candidates=choices)
        answer.result["conditions"] = {
            "candidate_count": len(choices),
            "candidate_list_complete": True,
        }
        return _finish(answer, theme, "\n".join(lines), "disambiguation")
    record = answer.record(matches[0]["id"])
    if probability:
        answer.emit(record)
        answer.result["status"] = "unsupported"
        answer.result["conditions"] = {
            "probability": None,
            "reason": "no_verified_drop_distribution",
        }
        return _finish(
            answer,
            theme,
            f"「{record['name']}」的掉落概率：未知。现有已核验资料不能确定抽取池、权重、解锁状态和抽取条件；不能用藏品数量倒算概率。已附该藏品的原始配置供核对。",
            "unknown_probability",
        )
    if not ending and not menu and not acquisition:
        return None
    source_pages = list(rag.pages(answer.session, answer.release, answer.store, theme))
    spec = guide_spec(answer.entry.get("scope", {}))
    if acquisition:
        return _acquisition(answer, record, source_pages, theme)
    if ending:
        cards = [
            c
            for c in spec["ending_routes"]
            if c["theme_id"] == theme and c["name"] == record["name"]
        ]
        if len(cards) != 1:
            return None
        card = cards[0]
        passages = card.get("passages", [card])
        answer.emit(record)
        quotes = []
        for part in passages:
            page = next((p for p in source_pages if p["pin"]["revision"] == part["revision"]), None)
            if page is None or page["text"][part["start"] : part["end"]] != part["quote"]:
                raise Conflict("ending route passage differs")
            _add_passage(answer, page, part["start"], part["end"], "结局进入与达成条件")
            quotes.append(_render_route_passage(answer, page, part["quote"]))
        answer.result["conditions"].update(ending_route_documented=True, community_source=True)
        return _finish(
            answer,
            theme,
            f"{THEME_NAMES[theme]} · {record['name']}\n" + "\n".join(quotes),
            "ending_route",
        )
    menus = [
        (page, value)
        for page in source_pages
        if page["pin"]["category"] in ("events", "discovery")
        and (value := _menu(page, record)) is not None
    ]
    if len(menus) != 1:
        return answer.refuse(
            "已识别场景，但固定资料未能唯一核验其完整菜单；不能借用同名场景的奖励。",
            "needs_verification",
        )
    page, (start, end, current, follow) = menus[0]
    answer.emit(record)
    cid = _add_passage(answer, page, start, end, "事件完整分支原文")
    context_notes = []
    if current.get("floor"):
        context_notes.append(_event_context_note(
            "appearance_floor", page, current["start"], current["end"], cid,
        ))
    for information in current.get("information_notes", []):
        note_start, note_end = information["source_start"], information["source_end"]
        if page["text"][note_start:note_end] != information["raw"]:
            raise Conflict("event information note differs from the complete source page")
        context_notes.append(_event_context_note(
            "information_note", page, note_start, note_end, cid,
        ))
    cited_rewards = {}
    reward_citations = _resolve_event_rewards(
        answer, source_pages, theme, current["options"], cited_rewards
    )
    for scene in follow:
        reward_citations = _resolve_event_rewards(
            answer, source_pages, theme, scene["options"], cited_rewards
        )
    answer.result["event_menu"] = {
        **current,
        "followup_scenes": follow,
        "citation_ids": [cid, *reward_citations],
    }
    answer.result["conditions"].update(
        originating_event_menu_verified=True,
        menu_completeness="complete_within_fixed_wiki_scene",
        community_source=True,
    )
    lines = [f"{THEME_NAMES[theme]} · {record['name']} · {current['scene_label']}"]
    if current.get("floor"):
        lines.append("资料所列出现层数：" + current["floor"])
    if record["name"] == "行动奖励":
        intro = page["text"][start:end].split("{{", 1)[0]
        lines.append(plain(intro))
        if plain(intro):
            context_notes.append(_event_context_note(
                "shared_intro", page, start, start + len(intro), cid,
            ))
    lines.extend(_menu_lines(current))
    if not current["options"]:
        lines.append("此资料场景没有可选按钮；它可能是结果场景。")
    seen = set()
    for scene in follow:
        if scene["scene_label"] == current["scene_label"] and scene["description"] == record[
            "data"
        ].get("description"):
            continue
        key = (scene["scene_label"], tuple(o["raw"] for o in scene["options"]),
               tuple(n["raw"] for n in scene.get("information_notes", [])))
        if key in seen or not (scene["options"] or scene.get("information_notes")):
            continue
        seen.add(key)
        lines.append("后续场景「" + scene["scene_label"] + "」：")
        lines.extend(_menu_lines(scene))
    for context in spec.get("event_context", []):
        if context["theme_id"] != theme or context["native_scene_id"] != record["data"].get("id"):
            continue
        source = next(
            (p for p in source_pages if p["pin"]["revision"] == context["revision"]), None
        )
        if source is None or source["text"][context["start"] : context["end"]] != context["quote"]:
            raise Conflict("event context passage differs")
        context_cid = _add_passage(answer, source, context["start"], context["end"], "事件前置条件与回归奖励")
        context_notes.append(_event_context_note(
            "reviewed_event_context", source, context["start"], context["end"], context_cid,
        ))
        lines.append("相关节点规则：\n" + plain(context["quote"]))
    answer.result["event_context_notes"] = context_notes
    lines.append("按上述条件显示的选项与后续分支并非同时领取；随机奖励的具体结果未作推测。")
    return _finish(answer, theme, "\n".join(lines), "event_menu")
