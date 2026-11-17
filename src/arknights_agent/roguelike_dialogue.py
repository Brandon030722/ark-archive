"""Bounded natural-language adaptation of release-pinned roguelike answers.

The caller may supply only an answer recovered from its own question audit.  A
user-provided transcript is never an authority for a menu, record or citation.
Player state is a report by the user, not a verified game observation.  This
module rewrites supported requests, then replays the existing source verifier.
"""

from __future__ import annotations

import copy
import re

from arknights_agent.errors import DomainError, InvalidData
from arknights_agent.roguelike import (
    ALIASES,
    KINDS,
    _Answer,
    _spans,
    _strip_query,
    answer_roguelike,
)
from arknights_agent.roguelike_guides import _option_lines, guide_spec
from arknights_agent.roguelike_sources import THEME_NAMES
from arknights_agent.wiki_text import plain

_NUMBERS = {word: i for i, word in enumerate("零一二三四五六七八九", 0)}
_NUMBERS.update({"两": 2, "十": 10, "Ⅰ": 1, "Ⅱ": 2, "Ⅲ": 3, "Ⅳ": 4,
                 "Ⅴ": 5, "Ⅵ": 6, "Ⅶ": 7, "Ⅷ": 8, "Ⅸ": 9})
_NUMBER = r"(?:[0-9]{1,2}|[一二三四五六七八九十两]|Ⅷ|Ⅶ|Ⅵ|Ⅳ|Ⅲ|Ⅱ|Ⅰ|Ⅸ|Ⅴ)"
_ENDING_ORDINAL = re.compile(
    rf"第?({_NUMBER})\s*(?:个)?\s*(?:结局|结)|结局\s*第?({_NUMBER})(?![0-9])"
)
_SELECTION = re.compile(rf"第\s*({_NUMBER})\s*(?:个|项|条)(?:选项|按钮|候选|场景)?")
_POSSESSION = re.compile(
    r"(?P<negative>没有|没拿到|没拿|没获得|未持有|不持有|失去了?|丢了)"
    r"|(?P<positive>持有|拿到了?|拿了|获得了|有了|有)(?:藏品|收藏品)?[「『【“\"]?"
)
_ACQUIRE = re.compile(
    r"怎么(?:拿|得|获取|获得|入手)|如何(?:拿|获取|获得|入手)|哪里(?:拿|得|刷|有)|"
    r"哪(?:个|些)?事件|获取(?:方式|途径)?|获得(?:方式|途径)?|入手|想(?:要)?(?:拿|得|获取|获得)|"
    r"从哪|怎么弄|咋(?:拿|得|获取)|要走哪个|弄到|搞到手|搞到|获取步骤"
)
_ROUTE = re.compile(
    r"结局|路线|目标|想打|要打|想进|要进|后面|后续|怎么(?:走|开|进|解锁|触发|打)|如何(?:走|开|进|解锁)|"
    r"下一步|接下来|然后呢|继续|还能(?:进|走|打)|能不能(?:进|走|打)|还来得及"
)
_MENU = re.compile(r"选项|按钮|菜单|奖励|怎么选|选哪个|有哪些|会给什么|给啥")
_PROBABILITY = re.compile(r"概率|掉率|几率|出现率|必出|必定|保证|百分之|百分比|等概率")
_CONTEXTUAL_THEMES = {"岁": "rogue_5"}
_COLLOQUIAL = (
    "我想要", "我想", "我要", "想要", "我玩", "我在玩", "我打", "我正在玩", "玩的是", "玩的",
    "想走", "要走", "想打", "要打", "走哪个事件", "要走哪个", "哪个事件", "哪一个事件",
    "怎么拿", "怎么得", "怎么弄", "咋拿", "咋得", "怎么获取", "怎么获得", "怎么入手",
    "如何入手", "获取方式", "获取途径", "获得方式", "获得途径", "入手", "从哪", "哪里拿",
    "哪里得", "哪里刷", "哪里有", "怎么走", "怎么开", "怎么进", "怎么解锁", "如何解锁",
    "如何走", "如何进", "触发条件", "路线", "攻略", "解锁", "条件", "下一步", "接下来",
    "然后呢", "然后", "继续", "这把", "这局", "本局", "我", "已经", "已", "现在", "目前",
    "这次", "手上", "背包里", "还能进", "还能走", "还能打", "能不能进", "能不能走",
    "能不能打", "还来得及", "可以进", "可以走", "可不可以", "行不行", "够不够", "怎么办",
    "哪个", "咋选", "怎么选", "选哪个", "选项", "按钮", "菜单", "奖励", "会给什么", "给啥",
    "可选", "所有可选", "完整奖励", "这个藏品", "这个收藏品", "这个事件", "这个", "它",
    "那", "呢", "能", "走", "打", "进", "开", "拿", "得", "吗", "要", "想", "接着",
    "掉落概率", "概率", "掉率", "几率", "多少", "必出", "必定", "保证", "百分之", "等概率",
    "效果", "用途", "什么意思", "是什么", "条件是什么", "有什么", "有啥", "帮我看看",
    "展开", "是哪条", "完整原始配置", "的条件和奖励", "这个东西", "这个道具",
    "再说一下", "给我说一下", "说一下", "再确认一下", "按我刚说的情况", "咋走", "咋进",
    "后面还缺什么", "后面", "要注意什么", "去哪儿获取", "去哪获取", "百分之几", "到底",
    "是不是就走不了了", "这页", "每个", "列下", "我在", "碰到", "把", "看下", "就",
    "里", "啊", "吧", "了", "也", "才", "到达", "到了", "到",
    "这藏品", "这收藏品", "有百分之几", "有多少", "拿到", "获取", "这", "有",
    "在哪里", "在哪儿", "在哪", "哪些步骤", "还有哪些步骤", "接下来做什么",
    "弄到", "搞到手", "搞到", "该", "这主题", "这次", "获取步骤", "再确认下", "再核对下",
    "确认", "核对", "前面说的那个", "前面说的", "那个", "后续", "具体", "目标", "我的是",
    "我的", "我是", "后面还有哪几步", "哪几步", "还要", "步骤", "要求", "限制",
    "我上一条说的", "上一条说的", "上一条", "情况还算数", "和情况还算数", "还算数",
    "都有些什么", "有些什么", "都有", "分别", "看一下", "出现率", "准确百分比",
    "百分比", "报个", "准确", "确定的", "获取路线", "再", "还", "下",
    "怎么", "如何", "咋",
)


def recognizes_dialogue(question: str) -> bool:
    """Conservative public routing; a prior audited answer is routed by caller."""
    if not isinstance(question, str) or re.search(r"公招|公开招募|招募标签|卫戍|盟约|技能|天赋", question):
        return False
    if any(name in question for name in THEME_NAMES.values()) or re.search(r"肉鸽|集成战略|rogue_[1-6]", question):
        return True
    if any(re.search(re.escape(alias) + r"(?:这|这个|的)?主题", question) for alias in _CONTEXTUAL_THEMES):
        return True
    if _ENDING_ORDINAL.search(question) or (_SELECTION.search(question) and re.search(r"选项|按钮|候选|场景", question)):
        return True
    if any(alias in question for alias in ALIASES) and (
        _ROUTE.search(question) or _MENU.search(question) or _ACQUIRE.search(question)
        or _PROBABILITY.search(question) or "主题" in question
    ):
        return True
    # A name alone is never enough to steal an operator question. These explicit
    # requests are confirmed against the pinned catalog inside the wrapper.
    return bool(_ACQUIRE.search(question) and re.search(
        r"藏品|收藏品|我想(?:拿|弄到|搞到手)|想要(?:拿|弄到|搞到手)|哪个事件", question
    ))


def _number(value):
    return int(value) if value.isdecimal() else _NUMBERS.get(value)


def _name_forms(name):
    """Typography-only display aliases; ambiguity still requires exact identity."""
    bare = re.sub(r'[“”‘’「」『』\"]', "", name)
    return {name, bare}


def _mask(text, spans):
    chars = list(text)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    return "".join(chars)


def _prior(previous, release):
    if previous is None:
        return None
    if not isinstance(previous, dict) or (
        previous.get("module") != "roguelike"
        or previous.get("release_id") != release.id
        or previous.get("manifest_hash") != release.manifest_hash
    ):
        raise InvalidData("previous answer must be a same-release authoritative roguelike result")
    return previous


def _condition_key(condition):
    if condition["type"] == "unknown":
        # Independent literal unknowns are separate restrictions. Treating
        # every nameless unknown as one key silently erased earlier state.
        # A repeated identical quote still deduplicates; known typed fields
        # retain their existing correction keys.
        return "unknown", condition.get("raw_text", condition.get("quote", ""))
    return condition["type"], condition.get("name", "")


def _merge_conditions(previous, current):
    merged = {_condition_key(c): copy.deepcopy(c) for c in previous}
    for condition in current:
        merged[_condition_key(condition)] = condition
    return list(merged.values())


def _user_conditions(question, spans, rows):
    """Recognize literal state assertions; leave every other condition unresolved."""
    conditions, consumed = [], []
    relics = {r["name"] for r in rows if r["kind"] == "roguelike_relic"}
    for start, end, name in spans:
        if name not in relics:
            continue
        clause_start = max(question.rfind(char, 0, start) for char in "，,。；;？?\n") + 1
        prefix = question[clause_start:start]
        lexical_prefix = _mask(prefix, [(a - clause_start, b - clause_start) for a, b, _ in spans
                                        if clause_start <= a and b <= start])
        marker = list(_POSSESSION.finditer(lexical_prefix))
        postfix = re.match(r'[」』】”\"]?(?P<negative>没拿到|没拿|没有|失去了?|丢了)|'
                           r'[」』】”\"]?(?P<positive>有了|到手了|拿到了|已获得|已持有)',
                           question[end:])
        if postfix and not re.search(r"想(?:要)?|打算|如果|假如|假设|要是", prefix):
            state = "not_owned" if postfix.group("negative") else "owned"
            conditions.append({"type": "relic_possession", "name": name, "value": state,
                               "raw_text": question[clause_start:end + len(postfix[0])],
                               "verification_status": "user_reported", "citation_ids": []})
            consumed.append((clause_start, end + len(postfix[0])))
            continue
        if not marker:
            continue
        match = marker[-1]
        if re.search(r"想(?:要)?|打算|如果|假如|假设|要是", prefix[:match.start()]):
            continue
        # No intervening prose may be erased as if it were a possession report.
        suffix = prefix[match.end():]
        for relic in sorted(relics, key=len, reverse=True):
            suffix = suffix.replace(relic, "")
        suffix = re.sub(r"和|与|以及|还有|及|跟|、", "", suffix)
        if suffix.strip(' 「『【“"」』】”藏品收藏品'):
            continue
        state = "not_owned" if match.group("negative") else "owned"
        conditions.append({"type": "relic_possession", "name": name, "value": state,
                           "raw_text": question[clause_start:end],
                           "verification_status": "user_reported", "citation_ids": []})
        tail = re.match(r'[」』】”\"]?了?', question[end:])
        consumed.append((clause_start, end + len(tail[0])))
    state_patterns = (
        ("floor", re.compile(rf"(?:第)?({_NUMBER})\s*(?:层|楼)")),
        ("difficulty", re.compile(r"(?:难度(?:等级)?\s*|[Nn]\s*)([0-9]{1,2})(?![0-9])")),
        ("knight_alive", re.compile(r"(?:猎潮的)?骑士(?:已经|刚刚|在第?" + _NUMBER
                                     + r"层)?(?:给|被)?(?:弄死了|打死了|死了|死亡|死掉了?|阵亡|击败|被击败)")),
        ("knight_alive", re.compile(r"(?:猎潮的)?骑士(?:仍然|依然|还|一直)?(?:活着|存活)")),
        ("resources", re.compile(r"(?:只有|有|剩|还剩)\s*([0-9]+)\s*(?:点|个|名|缕)?"
                                  r"(希望|源石锭|目标生命(?:上限)?|构想|伺烛客)")),
        ("mode", re.compile(r"(?:难度(?:是|为)?|在|我玩)(波涛迭起|正式调查|整装出勤|苦难之路|"
                              r"挑战自然(?:·[0-9]+)?|深入调查)")),
    )
    for kind, pattern in state_patterns:
        for match in pattern.finditer(question):
            if any(start <= match.start() and match.end() <= end for start, end, _ in spans):
                continue
            value = (_number(match[1]) if kind == "floor" else int(match[1])
                     if kind in {"difficulty", "resources"} else
                     bool(re.search(r"活着|存活", match[0])) if kind == "knight_alive" else match[1])
            condition = {"type": kind, "value": value, "raw_text": match[0],
                         "verification_status": "user_reported", "citation_ids": []}
            if kind == "resources":
                condition["name"] = match[2]
            conditions.append(condition)
            consumed.append(match.span())
    return conditions, consumed


def _decorate(result, question, parsed):
    result["question"] = question
    result["dialogue"] = parsed
    result.setdefault("conditions", {})["player_state_verification"] = "user_reported_only"
    result["conditions"]["user_reported_conditions"] = copy.deepcopy(parsed["user_reported_conditions"])
    result.setdefault("tool_trace", []).append({
        "tool": "roguelike_dialogue_adapter", "method": "bounded_literal_request_adapter",
        "canonical_question": parsed.get("canonical_question"),
        "context_used": parsed["context_used"], "game_state_observed": False,
    })
    return result


def _refusal(answer, question, parsed, message, status="needs_clarification"):
    return _decorate(answer.refuse(message, status), question, parsed)


def _canonical(theme, target, intent):
    name = target["native_id"]
    noun, request = {
        "relic_acquisition": ("藏品", "获取方式和图鉴解锁条件"),
        "unknown_probability": ("藏品", "掉落概率是多少"),
        "ending_route": ("结局", "解锁路线"),
        "event_menu": ("事件", "所有可选按钮和完整奖励是什么"),
        "document": (target["kind"].removeprefix("roguelike_"), "完整原始配置是什么"),
    }[intent]
    if intent == "document":
        noun = KINDS.get(noun, (noun,))[0]
    return f"肉鸽{theme}，{noun}「{name}」的{request}？"


def _source_ending_ordinal(answer, rows, theme, ordinal):
    """Source ID and priority must agree; never include fail/special endings."""
    matches = [r for r in rows if r["kind"] == "roguelike_ending"
               and r["parent_native_id"] == theme
               and re.search(r"/endings/[^/]*_ending_" + str(ordinal) + r"$", r["native_id"])]
    if len(matches) != 1:
        return None
    answer.verify_catalog(theme, "ending", rows)
    record = answer.record(matches[0]["id"])
    answer.cite(record, "", record["data"])
    if record["data"].get("priority") != ordinal - 1:
        return None
    return matches[0]


def _state_intro(conditions):
    if not conditions:
        return ""
    descriptions = []
    for c in conditions:
        if c["type"] == "relic_possession":
            descriptions.append(("持有" if c["value"] == "owned" else "未持有") + "「" + c["name"] + "」")
        elif c["type"] == "floor":
            descriptions.append(f"第{c['value']}层")
        elif c["type"] == "difficulty":
            descriptions.append(f"难度{c['value']}")
        else:
            descriptions.append(c["raw_text"])
    return "你提供的局内条件（用户自述，未观测游戏状态）：" + "；".join(descriptions) + "。\n"


def _merge_evidence(base, extra):
    """Copy source-owned evidence while assigning independent reference IDs."""
    mapping = {}
    for citation in extra.get("citations", []):
        new = copy.deepcopy(citation)
        new["id"] = f"c{len(base['citations']) + 1}"
        mapping[citation["id"]] = new["id"]
        base["citations"].append(new)
    for claim in extra.get("claims", []):
        new = copy.deepcopy(claim)
        new["id"] = f"f{len(base['claims']) + 1}"
        new["citation_ids"] = [mapping[x] for x in new.get("citation_ids", [])]
        base["claims"].append(new)
    present = {r["id"] for r in base.get("records", [])}
    base["records"].extend(copy.deepcopy(r) for r in extra.get("records", []) if r["id"] not in present)
    return [mapping[c["id"]] for c in extra.get("citations", [])]


def _conditional_route(result, parsed, answer, rows, session, release, store):
    conditions = parsed["user_reported_conditions"]
    if not conditions or result.get("status") != "answered":
        return
    intro = _state_intro(conditions)
    theme, target = parsed["theme_id"], parsed["entity_name"]
    # A concrete negative consequence is reported only under its literal source
    # condition. Other state does not establish readiness or a skipped step.
    if (theme == "rogue_2" and target == "静谧时代"
            and any(c["type"] == "knight_alive" and c["value"] is False for c in conditions)
            and "永久失去本结局" in result["answer"]):
        intro += "若你所说的是【猎潮的骑士】已死亡，按所附固定资料，本次探索永久失去【静谧时代】；不能继续进入该结局。\n"
    else:
        intro += "以下按你的目标列出固定资料中的路线；持有一个藏品或到达某层，不能单独证明其他前置条件已满足。\n"
    cards = guide_spec(answer.entry.get("scope", {}))["ending_routes"]
    own_card = [c for c in cards if c["theme_id"] == theme and c["name"] == target]
    source_text = "\n".join(plain(p["quote"]) for c in own_card for p in c.get("passages", [c]))
    for condition in conditions:
        if condition["type"] != "relic_possession" or condition["value"] != "owned":
            continue
        name = condition["name"]
        if name in source_text:
            intro += f"与你自述持有「{name}」相关的条件及后续步骤，见下面的完整原文；未说明的步骤仍需逐项核对。\n"
            continue
        related = [c for c in cards if c["theme_id"] == theme and c["name"] != target
                   and any(name in plain(p["quote"]) for p in c.get("passages", [c]))]
        if len(related) != 1:
            intro += f"所附「{target}」路线没有将「{name}」列为进入条件；不能据此推断已满足目标结局的条件。\n"
            continue
        row = next((r for r in rows if r["kind"] == "roguelike_ending" and r["name"] == related[0]["name"]
                    and r["parent_native_id"] == theme), None)
        if row is None:
            continue
        extra = answer_roguelike(session, release, _canonical(theme, row, "ending_route"), store=store)
        if extra.get("status") != "answered":
            continue
        cids = _merge_evidence(result, extra)
        result.setdefault("related_routes", []).append({"name": row["name"], "relic": name,
                                                       "citation_ids": cids})
        intro += f"固定资料将「{name}」写在「{row['name']}」的路线中；这不等于已满足「{target}」的进入条件。\n"
    result["answer"] = intro + result["answer"]
    result["conditions"]["route_readiness_verified"] = False


def answer_roguelike_dialogue(session, release, question: str, *, store=None, previous_answer=None):
    """Adapt one question and an optional same-release, server-owned prior answer.

    No conversation state supplied by the HTTP client may be passed directly as
    ``previous_answer``. The caller must retrieve it from a verified stored run.
    """
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        raise InvalidData("question must contain 1 to 2000 characters")
    previous = _prior(previous_answer, release)
    answer = _Answer(session, release, question, store)
    parsed = {"intent": None, "theme_id": None, "entity_name": None, "entity_native_id": None,
              "canonical_question": None, "context_used": False, "user_reported_conditions": [],
              "unresolved_text": "", "state_observation": "user_reported_only"}
    if not answer.entry or store is None:
        return _decorate(answer_roguelike(session, release, question, store=store), question, parsed)
    if re.search(r"公招|公开招募|招募标签|卫戍|盟约|技能|天赋", question):
        return _refusal(answer, question, parsed, "这是其他模式或干员的问题，请另起该模式的查询，不能沿用肉鸽的对象和局内条件。")
    release_literals = re.findall(
        r"(?:发布(?:版本)?|release(?:_id)?|版本)\s*[:：]?\s*([a-fA-F0-9]{32})(?![a-fA-F0-9])",
        question, flags=re.I,
    )
    if any(value.lower() != release.id.lower() for value in release_literals):
        return _refusal(answer, question, parsed, "问题指定的发布版本与本次固定发布不一致，请明确使用哪一版本；不能跨发布沿用路线或对话条件。")
    try:
        rows = answer.catalog()
        objects = [r for r in rows if r["kind"] not in {"roguelike_theme", "roguelike_wiki", "roguelike_buff"}]
        forms = {}
        for r in objects:
            if re.fullmatch(r"[0-9]+", r["name"]):
                continue
            for form in _name_forms(r["name"]):
                forms.setdefault(form, set()).add(r["name"])
        literal_spans = _spans(question, [*forms, *[r["native_id"] for r in objects]])
        spans = [(a, b, next(iter(forms[v])) if v in forms and len(forms[v]) == 1 else v)
                 for a, b, v in literal_spans]
        reserved = _spans(question, [*THEME_NAMES.values(), *THEME_NAMES, *ALIASES,
                                     *(word for words in KINDS.values() for word in words),
                                     *[alias for alias in _CONTEXTUAL_THEMES
                                       if re.search(re.escape(alias) + r"(?:这|这个|的)?主题", question)]])
        spans = [(a, b, v) for a, b, v in spans if not any(c <= a and b <= d and len(label) >= len(v)
                                                         for c, d, label in reserved)]
        current, state_spans = _user_conditions(question, spans, rows)
        parsed["user_reported_conditions"] = current
        # Existing literal document/catalog queries keep their complete related
        # records, null fields and selector semantics. Adaptation is unnecessary
        # when the original verifier already answers an unconditional first turn.
        if previous is None and not current and not _ENDING_ORDINAL.search(question):
            direct = answer_roguelike(session, release, question, store=store)
            if direct.get("status") == "answered" or direct.get("candidates"):
                base_records = [r for r in direct.get("records", [])
                                if r["kind"] not in {"roguelike_buff", "roguelike_wiki"}]
                kind = direct.get("conditions", {}).get("guide_kind", "document")
                parsed.update(theme_id=direct.get("theme_id"), intent=kind,
                              canonical_question=question)
                if len(base_records) == 1:
                    parsed.update(entity_name=base_records[0]["name"],
                                  entity_native_id=base_records[0]["native_id"])
                elif direct.get("candidates"):
                    names = {c["name"] for c in direct["candidates"]}
                    if len(names) == 1:
                        parsed["entity_name"] = next(iter(names))
                return _decorate(direct, question, parsed)
        grouped = {}
        for condition in current:
            grouped.setdefault(_condition_key(condition), set()).add(str(condition["value"]))
        if any(len(values) > 1 for values in grouped.values()):
            return _refusal(answer, question, parsed, "你同时提供了相互冲突的局内条件，请确认这局实际持有什么、所在层数和难度。")
        explicit_themes = {theme for theme, name in THEME_NAMES.items() if theme in question or name in question}
        explicit_themes.update(theme for alias, theme in ALIASES.items() if alias in question and (
            re.search(r"肉鸽|集成战略|我玩|我在玩|我正在玩", question)
            or re.search(re.escape(alias) + r"\s*(?:第?[一二三四五六123456]|结局|的?事件)", question)
            or not any(a <= question.find(alias) and question.find(alias) + len(alias) <= b
                       for a, b, _ in spans)))
        explicit_themes.update(theme for alias, theme in _CONTEXTUAL_THEMES.items()
                               if re.search(re.escape(alias) + r"(?:这|这个|的)?主题", question))
        if len(explicit_themes) > 1:
            return _refusal(answer, question, parsed, "问题包含多个肉鸽主题，请一次指定一个主题。")
        theme = next(iter(explicit_themes), None)
        request_text = _mask(question, state_spans)
        ordinals = {_number(next(g for g in m.groups() if g is not None)) for m in _ENDING_ORDINAL.finditer(request_text)}
        if len(ordinals) > 1:
            return _refusal(answer, question, parsed, "你指定了多个目标结局，请明确这局要走的一个结局。")
        selected_number = _SELECTION.search(request_text)
        if selected_number and any(m.start() <= selected_number.start() and selected_number.end() <= m.end()
                                   for m in _ENDING_ORDINAL.finditer(request_text)):
            selected_number = None
        candidate_selection = None
        menu_selection = None
        if selected_number and previous and (not explicit_themes or theme == previous.get("theme_id")):
            prior_dialogue = previous.get("dialogue", {})
            parsed.update(theme_id=previous.get("theme_id"),
                          entity_name=prior_dialogue.get("entity_name"),
                          entity_native_id=prior_dialogue.get("entity_native_id"),
                          intent=prior_dialogue.get("intent"), context_used=True)
            parsed["user_reported_conditions"] = _merge_conditions(
                prior_dialogue.get("user_reported_conditions", []), current
            )
            number = _number(selected_number[1])
            candidates = previous.get("candidates", [])
            menu = previous.get("event_menu", {}).get("options", [])
            if candidates:
                selected = [c for c in candidates if c.get("number") == number]
                if len(selected) == 1:
                    candidate_selection = selected[0]
                else:
                    return _refusal(answer, question, parsed, "该序号不在上一轮已核验的候选列表中，请按列表重新选择。")
            elif menu:
                if number is None or number < 1 or number > len(menu):
                    return _refusal(answer, question, parsed, "该序号不在上一轮场景的第一层菜单中，请明确场景与选项。")
                menu_selection = number
            else:
                return _refusal(answer, question, parsed, "上一轮没有可供该序号选择的候选或菜单，请提供完整名称。")
            request_text = _mask(request_text, [selected_number.span()])
            parsed["context_used"] = True
        elif selected_number:
            return _refusal(answer, question, parsed, "没有已核验的上一轮候选或菜单，无法确定你说的第几个，请提供完整名称。")
        kind = None
        intent = None
        if ordinals or "结局" in request_text:
            kind, intent = "roguelike_ending", "ending_route"
        elif _PROBABILITY.search(request_text):
            kind, intent = "roguelike_relic", "unknown_probability"
        elif _ACQUIRE.search(request_text):
            kind, intent = "roguelike_relic", "relic_acquisition"
        elif _MENU.search(request_text):
            kind, intent = "roguelike_event", "event_menu"
        elif "事件" in request_text:
            kind, intent = "roguelike_event", "document"
        matched_values = {value for a, b, value in spans if not any(c <= a and b <= d for c, d in state_spans)}
        targets = [r for r in objects if any(v in (r["name"], r["native_id"]) for v in matched_values)
                   and (not theme or r["parent_native_id"] == theme) and (not kind or r["kind"] == kind)]
        if not theme:
            inferred = {r["parent_native_id"] for r in targets}
            if len(inferred) == 1:
                theme = next(iter(inferred))
        if not theme and previous:
            theme = previous.get("theme_id")
            parsed["context_used"] = bool(theme)
        if previous and theme == previous.get("theme_id"):
            old = previous.get("dialogue", {}).get("user_reported_conditions", [])
            if old:
                parsed["user_reported_conditions"] = _merge_conditions(old, current)
                parsed["context_used"] = True
        parsed["theme_id"] = theme
        incompatible = [c["name"] for c in parsed["user_reported_conditions"]
                        if c["type"] == "relic_possession" and theme and not any(
                            r["kind"] == "roguelike_relic" and r["name"] == c["name"]
                            and r["parent_native_id"] == theme for r in rows)]
        if incompatible:
            return _refusal(answer, question, parsed, "该主题的固定藏品目录没有对应你说的「"
                            + "、".join(incompatible) + "」，请核对主题或完整名称；不能代入其他主题的条件。")
        if candidate_selection:
            target = next((r for r in objects if r["native_id"] == candidate_selection.get("native_id")), None)
            intent = ("document" if re.search(r"原始配置|完整配置|原文|配置", request_text) else
                      "event_menu" if "按钮" in candidate_selection.get("question", "") else "document")
            targets = [target] if target else []
        elif menu_selection or (not targets and not ordinals and previous and re.search(
                r"这个|那个|它|下一步|接下来|然后|继续|路线|刚说|前面说|上一条|获取步骤", request_text)):
            prior_records = previous.get("records", [])
            prior_entity = previous.get("dialogue", {}).get("entity_native_id")
            eligible = [r for r in objects if r["parent_native_id"] == theme and
                        (r["native_id"] == prior_entity if prior_entity else
                         any(r["id"] == p.get("id") for p in prior_records))]
            if kind:
                eligible = [r for r in eligible if r["kind"] == kind]
            if len(eligible) == 1:
                targets = eligible
                intent = intent or previous.get("dialogue", {}).get("intent") or (
                    "ending_route" if eligible[0]["kind"] == "roguelike_ending" else "document")
                if menu_selection:
                    intent = "event_menu"
                parsed["context_used"] = True
        if theme and ordinals:
            ordinal = next(iter(ordinals))
            ordinal_target = _source_ending_ordinal(answer, rows, theme, ordinal)
            if ordinal_target is None:
                return _refusal(answer, question, parsed, "该发布的正常结局编号与排序未能唯一核验，请提供结局完整名称。", "needs_verification")
            if targets and any(r["id"] != ordinal_target["id"] for r in targets):
                return _refusal(answer, question, parsed, "结局名称与所写序号不一致，请确认目标结局。")
            targets = [ordinal_target]
            parsed["ending_ordinal"] = {"number": ordinal, "source_id": ordinal_target["source_id"],
                                        "verification": "exact_normal_ending_id_and_priority"}
        if not targets or not theme:
            direct = answer_roguelike(session, release, question, store=store)
            if direct.get("status") == "answered" or direct.get("candidates"):
                return _decorate(direct, question, parsed)
            return _refusal(answer, question, parsed, "请补充肉鸽主题及目标藏品、事件或结局完整名称；查不到的对象不会借用相似名称作答。")
        target_names = {r["name"] for r in targets}
        if len(target_names) > 1 or (len(targets) > 1 and any(r["kind"] != "roguelike_event" for r in targets)):
            return _refusal(answer, question, parsed, "对象存在多个匹配，请补充主题或唯一原始ID，不能混用同名资料。")
        target = targets[0]
        if intent is None:
            intent = "ending_route" if target["kind"] == "roguelike_ending" and _ROUTE.search(request_text) else "document"
        stray_values = matched_values - {target["name"], target["native_id"]}
        if stray_values and not candidate_selection:
            return _refusal(answer, question, parsed, "问题还指定了其他对象（" + "、".join(sorted(stray_values))
                            + "），请明确目标；不能删掉额外对象后只答其中一个。")
        # A referent does not convert a new unsupported request into the old
        # answer. Preserve all unrecognized prose and ask for clarification.
        consumed = [*state_spans, *[(a, b) for a, b, _ in spans]]
        consumed.extend(m.span() for m in _ENDING_ORDINAL.finditer(question))
        if selected_number:
            consumed.append(selected_number.span())
        semantic = _mask(question, consumed)
        extras = [*_COLLOQUIAL, *THEME_NAMES.values(), *THEME_NAMES, *ALIASES,
                  *[alias for alias in _CONTEXTUAL_THEMES
                    if re.search(re.escape(alias) + r"(?:这|这个|的)?主题", question)]]
        remaining = _strip_query(semantic, extras)
        parsed.update(intent=intent, entity_name=target["name"], entity_native_id=target["native_id"],
                      unresolved_text=remaining)
        if remaining:
            return _refusal(answer, question, parsed, "问题还有未能核验的附加条件或另一项请求（" + remaining
                            + "），请说明其含义；这些条件不会被删掉后当作已满足。")
        if len(targets) > 1:
            request = "所有可选按钮和完整奖励是什么" if intent == "event_menu" else "完整原始配置是什么"
            canonical = f"肉鸽{theme}，事件「{target['name']}」的{request}？"
        else:
            canonical = _canonical(theme, target, intent)
        parsed["canonical_question"] = canonical
        result = answer_roguelike(session, release, canonical, store=store)
        if menu_selection and result.get("event_menu"):
            options = result["event_menu"]["options"]
            if menu_selection > len(options):
                return _refusal(answer, question, parsed, "重新核验的菜单与上一轮序号不一致，本次不回答。", "needs_verification")
            chosen = copy.deepcopy(options[menu_selection - 1])
            parsed["selected_option_number"] = menu_selection
            result["selected_option"] = {"number": menu_selection, **chosen,
                                         "citation_ids": result["event_menu"]["citation_ids"]}
            result["answer"] = (f"第{menu_selection}个第一层按钮「{chosen['label']}」：\n"
                                + "\n".join(_option_lines([chosen])) + "\n\n" + result["answer"])
        if intent == "ending_route":
            _conditional_route(result, parsed, answer, rows, session, release, store)
        elif parsed["user_reported_conditions"]:
            result["answer"] = _state_intro(parsed["user_reported_conditions"]) + "以下为固定资料，不能仅据你的局内条件保证出现、获取或满足门槛。\n" + result["answer"]
        return _decorate(result, question, parsed)
    except DomainError:
        return _refusal(answer, question, parsed, "固定来源或对话中的原始对象核验失败，本次不推断游戏事实。", "needs_verification")
