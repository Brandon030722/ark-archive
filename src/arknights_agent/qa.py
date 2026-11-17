"""Evidence-bound operator questions, without a model or an unscoped lookup.

A planner selects records and conditions; this module alone builds factual output.
Client descriptions are display templates, not an implementation of combat rules.
"""

from __future__ import annotations

import html
import json
import re
from decimal import Decimal, InvalidOperation

from arknights_agent import services
from arknights_agent.errors import Conflict, InvalidData

INTENTS = {
    "operator_overview",
    "operator_skill",
    "operator_talent",
    "operator_module",
    "operator_attributes",
    "unsupported",
}
PLAN_KEYS = {
    "intent",
    "operator_ids",
    "skill_index",
    "skill_name",
    "rank",
    "elite_phase",
    "level",
    "potential_rank",
    "module_query",
    "module_level",
    "compare",
    "reason",
}
PROFESSIONS = {
    "PIONEER": "先锋",
    "WARRIOR": "近卫",
    "SNIPER": "狙击",
    "CASTER": "术师",
    "MEDIC": "医疗",
    "SUPPORT": "辅助",
    "TANK": "重装",
    "SPECIAL": "特种",
}
POSITIONS = {"MELEE": "近战位", "RANGED": "远程位", "ALL": "全部位置"}
SP_TYPES = {
    "INCREASE_WITH_TIME": "自动回复",
    "INCREASE_WHEN_ATTACK": "攻击回复",
    "INCREASE_WHEN_TAKEN_DAMAGE": "受击回复",
    "NONE": "无",
    None: "来源未指定",
}
SKILL_TYPES = {"MANUAL": "手动触发", "AUTO": "自动触发", "PASSIVE": "被动"}
ATTRIBUTES = {
    "maxHp": "生命值",
    "atk": "攻击力",
    "def": "防御力",
    "magicResistance": "法术抗性",
    "cost": "部署费用",
    "blockCnt": "阻挡数",
    "baseAttackTime": "基础攻击间隔",
    "respawnTime": "再部署时间",
}
UNSUPPORTED = re.compile(
    r"(?:伤害计算|算.{0,6}伤害|总伤害|伤害(?:是|为|有)?多少|dps|DPS|期望伤害|概率|叠加顺序|伤害公式|"
    r"总(?:治疗|回复|恢复)(?:量|效果)?|(?:实际|实战).{0,12}(?:治疗|回复|恢复)(?:量|值)|治疗(?:计算|公式)|(?i:hps)|"
    r"最强|哪个强|谁更强|值得练|强度排行|怎么用|配队|阵容|剧情|故事|生日|身高|档案|声优|配音|皮肤|时装|抽卡|卡池|职业分支|当前有效|现在有效|最新版本|今日|今天|"
    r"信赖.*(?:满|\d)|满信赖|(?:满潜|潜能).*(?:面板|属性)|(?:实际|实战|最终|总).{0,8}属性|"
    r"(?:模组|模块).*(?:面板|(?:后|计入|加上|装备|开启|生效).{0,8}属性))"
)


def _records(session, release, kind):
    """Pagination always retains the supplied immutable release."""
    rows, offset = [], 0
    while True:
        page = services.list_records(
            session, release, module="operator", kind=kind, limit=200, offset=offset
        )
        rows.extend(page)
        if len(page) < 200:
            return rows
        offset += 200


def _plan(**overrides):
    result = {
        "intent": "operator_overview",
        "operator_ids": [],
        "skill_index": None,
        "skill_name": None,
        "rank": None,
        "elite_phase": None,
        "level": None,
        "potential_rank": None,
        "module_query": None,
        "module_level": None,
        "compare": False,
        "reason": None,
    }
    result.update(overrides)
    return result


def _number_token(value):
    return {
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }.get(value, int(value) if value.isdigit() else None)


MODULE_WORD = r"(?:模组|模块|(?i:module))"
MODULE_LETTER = r"[A-Za-zΑ-Ωα-ω]"
COMBAT_STATS = r"(?:属性|面板|攻击力|防御力|生命值|法术抗性|部署费用|阻挡数|攻击间隔)"


def _module_request_text(question):
    """Negative training qualifiers do not request module data.

    Remove only the local negated phrase; a subsequent positive module query
    remains visible. This never resolves module effects or combat applicability.
    """
    return re.sub(
        rf"(?:未(?:装(?:备)?|开启|解锁)|没(?:有)?(?:装(?:备)?|开启|解锁)|"
        rf"不(?:装(?:备)?|带|用|计入)|无|没有)\s*{MODULE_WORD}",
        "",
        question,
    )


def _unsupported_question(question):
    # A negated training qualifier is not a request to calculate module effects.
    # Plain module attributes are source blackboards; actual/combined stats still
    # require a combat implementation and remain outside the verified fields.
    text = _module_request_text(question)
    return (
        UNSUPPORTED.search(text)
        or re.search(
            rf"{MODULE_WORD}.*(?:普通作战|适用|能否生效|是否生效|生效吗|生效么|能用吗|能用么)",
            text,
        )
        or re.search(
            rf"{MODULE_WORD}.*(?:后|计入|加上|装备|开启|生效|实际|实战|最终|总).{{0,8}}"
            r"(?:攻击力|防御力|生命值|法术抗性|部署费用|阻挡数|攻击间隔)",
            text,
        )
        or re.search(
            r"(?:(?i:buff)|增益|加成|叠加).{0,12}" + COMBAT_STATS,
            text,
        )
        or re.search(
            rf"(?:装备|装配|带|装上|开启|计入|加上)\s*(?:{MODULE_LETTER}\s*)?"
            rf"{MODULE_WORD}.*{COMBAT_STATS}",
            text,
        )
        or re.search(rf"{COMBAT_STATS}.{{0,8}}(?:加|计入|叠加).{{0,8}}{COMBAT_STATS}", text)
    )


def _split_subject_connectors(text, names=()):
    # A pinned full name such as 和弦 owns its span before a connector is split.
    protected = sorted(
        (name for name in names if len(name) > 1 and re.search(r"和|与|、", name)),
        key=lambda name: (-len(name), name),
    )
    pattern = (
        "(?:" + "|".join(re.escape(name) for name in protected) + r")|(?P<connector>[和与、])"
        if protected
        else r"(?P<connector>[和与、])"
    )
    parts, start = [], 0
    for match in re.finditer(pattern, text):
        if match.group("connector"):
            parts.append(text[start : match.start()])
            start = match.end()
    return [*parts, text[start:]]


def _requested_subjects(question, names=()):
    """Recognize literal requested subjects, without resolving their identities.

    Only leading subjects followed by supported field syntax qualify. Multiple
    subjects must all match before their fields can be answered. Pronouns and
    alias requests remain ambiguous.
    This function never consults another release or supplies a native ID.
    """
    text = re.sub(
        r"^(?:(?:请问|请|帮我|帮忙|查询|查一下|查|介绍一下|关于|比较|对比|"
        r"我把|把|我想练|想练|我想培养|刚抽到)\s*)+",
        "",
        question.strip(),
    )
    known = "|".join(re.escape(name) for name in sorted(names, key=lambda name: (-len(name), name)))
    subject = r"[\u4e00-\u9fffA-Za-z·・][\u4e00-\u9fffA-Za-z0-9·・\-\s、]{0,79}?"
    if known:
        subject = rf"(?:{known}|{subject})"
    match = re.match(
        rf"({subject})"
        rf"(?=\s*(?:的(?=.{{0,40}}(?:{MODULE_WORD}|技能|天赋|属性|面板))|"
        rf"(?:的\s*)?(?:{MODULE_LETTER}\s*{MODULE_WORD}|{MODULE_WORD}|"
        r"(?:第)?[一二三123](?:技能|技)|(?:10|[1-9])\s*级\s*技能|技能|天赋|精(?:英)?[零一二012]|潜(?:能|力)?|满潜|基础(?:属性|面板)|属性|面板|概况|简介)))",
        text,
    )
    if match is None:
        return []
    return [subject.strip() for subject in _split_subject_connectors(match[1], names)]


def _explicit_unmatched_subject(question, names=()):
    subjects = _requested_subjects(question, names)
    if len(subjects) != 1:
        return None
    subject = subjects[0]
    if (
        subject
        in {
            "我",
            "你",
            "您",
            "我们",
            "你们",
            "他们",
            "她们",
            "他",
            "她",
            "它",
            "这",
            "那",
            "这个",
            "那个",
            "该",
            "某",
            "干员",
            "角色",
        }
        or re.search(
            r"这|那|哪个|什么|谁|某|别名|昵称|简称|比较|对比|还是|资料|文章|记录|来源|说明|"
            r"干员|角色|近卫|先锋|狙击|术师|医疗|辅助|重装|特种|医生|我的|你的|他的|她的|其",
            subject,
        )
        or re.search(r"想|把|练|培养|抽到|告诉|给|帮|查看|说|问|一下", subject)
        or re.search(MODULE_WORD, subject)
        or (re.fullmatch(MODULE_LETTER, subject) and re.search(MODULE_WORD, question))
        or re.search(
            r"\b(?:that|this|he|she|it|they|his|her|its|which|the\s+operator)\b", subject, re.I
        )
        or re.fullmatch(r"[一二三四五六七八九十0-9]+", subject)
    ):
        return None
    return subject


def _field_selector_spans(question, operators, modules=(), skills=()):
    """Pinned field titles own their spans, while literal subjects stay intact.

    A module title may contain another operator's name or a boundary word such
    as "story". A skill title may also equal, or be a prefix of, the requested
    operator's full name. These spans are syntax, never evidence of identities
    or a request for the corresponding unsupported topic.
    """
    names = {operator["name"] for operator in operators}
    protected = []
    for clause in _split_subject_connectors(question, names):
        for subject in _requested_subjects(clause, names):
            if subject in names:
                # Locate the same literal subject in the unmodified question.
                start = question.find(subject)
                protected.append((start, start + len(subject)))
    spans = []
    for kind, records in (("module", modules), ("skill", skills)):
        for name in {record["name"] for record in records if record["name"]}:
            start = question.find(name)
            while start != -1:
                end = start + len(name)
                if not any(start < right and end > left for left, right in protected):
                    spans.append((start, end, kind, name))
                start = question.find(name, end)
    # A complete field title owns any shorter title embedded inside it.
    chosen = []
    for span in sorted(spans, key=lambda value: (-(value[1] - value[0]), value[0], value[2])):
        if not any(span[0] < other[1] and span[1] > other[0] for other in chosen):
            chosen.append(span)
    return sorted(chosen)


def _field_selector_text(question, operators, modules=(), skills=()):
    text = question
    for start, end, kind, _name in reversed(
        _field_selector_spans(question, operators, modules, skills)
    ):
        text = text[:start] + ("模组" if kind == "module" else "技能") + text[end:]
    return text


def _literal_operator_subject(question, operators, modules=(), skills=()):
    text = _field_selector_text(question, operators, modules, skills)
    return _explicit_unmatched_subject(text, {operator["name"] for operator in operators})


def _complete_operator_catalog(release, operators):
    coverage = release.manifest.get("datasets", {}).get("operator", {}).get("coverage", {})
    ids = [operator["native_id"] for operator in operators]
    declared = coverage.get("operator_ids")
    return (
        coverage.get("status") == "source_complete"
        and coverage.get("operator_count") == len(ids)
        and isinstance(declared, list)
        and len(declared) == len(ids)
        and set(declared) == set(ids)
    )


def _unresolved_multi_request(question, operators):
    names = {operator["name"] for operator in operators}
    subjects = _requested_subjects(question, names)
    if len(subjects) > 1 and any(subject not in names for subject in subjects):
        return True
    # Also examine subjects after a connector when each has its own field.
    # "name's skill and attribute blackboard" has only one named subject;
    # "name's skill and another name's skill" must resolve both identities.
    clauses = _split_subject_connectors(question, names)
    separately_named = [
        subject for clause in clauses for subject in _requested_subjects(clause, names)
    ]
    return len(separately_named) > 1 and any(subject not in names for subject in separately_named)


def _module_code_pattern(modules):
    """Use the pinned release's codes, with a syntax fallback for unknown codes.

    Recognizing an unknown letter is deliberate: it must select no module rather
    than silently answering another module belonging to the same operator.
    """
    codes = {
        str(module["data"].get("typeName2", "")).upper()
        for module in modules
        if module["data"].get("typeName2")
    }
    parts = [re.escape(code) for code in sorted(codes, key=lambda code: (-len(code), code))]
    return "(?:" + "|".join([*parts, MODULE_LETTER]) + ")"


def _module_code(question, modules):
    code = _module_code_pattern(modules)
    # ASCII/Greek letter boundaries prevent "Max module" selecting its last X.
    after = re.search(rf"{MODULE_WORD}\s*({code})(?![A-Za-zΑ-Ωα-ω])", question, re.I)
    before = re.search(rf"(?<![A-Za-zΑ-Ωα-ω])({code})\s*{MODULE_WORD}", question, re.I)
    match = after or before
    return match[1].upper() if match else None


def _normalize_module_query(query):
    # Named modules and native IDs stay exact; letter codes are case-insensitive.
    return query.upper() if query and re.fullmatch(MODULE_LETTER, query) else query


def _mentions(question, operators, modules=()):
    question = _field_selector_text(question, operators, modules)
    code = _module_code_pattern(modules)
    matches = []
    for operator in operators:
        name = operator["name"]
        spans = []
        start = question.find(name)
        while start != -1:
            spans.append((start, start + len(name)))
            start = question.find(name, start + len(name))
        for start, end in spans:
            if len(name) == 1:
                rest = question[end:]
                if rest and not re.match(
                    rf"(?:的|是|和|与|、|，|,|。|？|\?|\s|技能|天赋|{MODULE_WORD}|(?i:{code})\s*{MODULE_WORD}|精|潜|满潜|属性|面板|基础(?:属性|面板)|几星|星级|职业|定位|概况|简介|基本信息|部署位置|(?:第)?[一二三123]技能|专)",
                    rest,
                ):
                    continue
                prefix = question[:start]
                if prefix and not re.search(
                    r"(?:^|的|问|看|查|给|知道|告诉我|关于|介绍|介绍一下|介绍下|说说|讲讲|了解|比较|对比|我把|把|我想练|想练|我想培养|刚抽到|和|与|、|\s|，|,)$",
                    prefix,
                ):
                    continue
            matches.append((start, end, operator["native_id"]))
    # An alter's longer name owns its span; do not also resolve its base name.
    selected = [
        m
        for m in matches
        if not any(
            other[0] <= m[0] and other[1] >= m[1] and other[1] - other[0] > m[1] - m[0]
            for other in matches
        )
    ]
    return list(dict.fromkeys(m[2] for m in sorted(selected)))


def plan_question(session, release, question: str) -> dict:
    """Public JSON planning contract; no numeric game facts come from a planner."""
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        raise InvalidData("question must contain 1 to 2000 characters")
    question = question.strip()
    if "operator" not in release.manifest.get("datasets", {}):
        return _plan(intent="unsupported", reason="该发布版本未包含干员数据。")
    operators = _records(session, release, "operator")
    module_requested = bool(re.search(MODULE_WORD, _module_request_text(question)))
    modules = _records(session, release, "module") if module_requested else []
    skills = _records(session, release, "skill")
    semantic_question = _field_selector_text(question, operators, modules, skills)
    ids = _mentions(semantic_question, operators, modules)
    if _unsupported_question(semantic_question) or any(
        t in semantic_question for t in ("卫戍", "公招", "公开招募")
    ):
        return _plan(
            intent="unsupported",
            operator_ids=ids[:2],
            reason="此问题需要另外的规则工具或有效期核验；干员字段查询无法验证。",
        )
    literal_subject = _literal_operator_subject(question, operators, modules, skills)
    unmatched_subject = bool(
        literal_subject and literal_subject not in {o["name"] for o in operators}
    )
    if unmatched_subject:
        # A known name occurring inside a different, explicit full name cannot
        # stand in for that requested identity, including an optional plan's ID.
        ids = []
    if _unresolved_multi_request(semantic_question, operators):
        return _plan(
            operator_ids=[],
            reason="问题中的多个对象未能全部匹配本发布版本；请分别提供干员全名并明确需要对比的字段。",
        )
    skill_names = {
        name
        for _start, _end, kind, name in _field_selector_spans(question, operators, modules, skills)
        if kind == "skill"
    }
    skill_matches = [skill for skill in skills if skill["name"] in skill_names]
    if not ids and skill_matches and not unmatched_subject:
        skill_ids = {s["native_id"] for s in skill_matches}
        ids = [
            o["native_id"]
            for o in operators
            if any(ref.get("skillId") in skill_ids for ref in o["data"].get("skills", []))
        ]
    if len(ids) > 2:
        return _plan(reason="识别到多个干员；请提供干员全名，或限定两个干员进行对比。")
    condition_question = semantic_question
    for operator in operators:
        if operator["native_id"] in ids:
            condition_question = condition_question.replace(operator["name"], "")
    intent = "operator_overview"
    if module_requested:
        intent = "operator_module"
    elif "天赋" in question:
        intent = "operator_talent"
    elif (
        "技能" in question
        or skill_matches
        or any(
            t in question.lower()
            for t in ("专三", "专二", "专一", "专3", "专2", "专1", "技力", "sp")
        )
    ):
        intent = "operator_skill"
    elif any(
        t in question
        for t in ("属性", "面板", "攻击力", "防御力", "生命值", "阻挡", "部署费用", "再部署")
    ):
        intent = "operator_attributes"
    else:
        overview_requested = bool(
            re.search(r"介绍|简介|概况|基本信息|职业|几星|星级|定位|部署位置|是什么干员", question)
        )
        name_only = any(
            question.strip(" \t\n，,。.!！?？:：") == operator["name"] for operator in operators
        )
        if not overview_requested and not name_only:
            return _plan(
                intent="unsupported",
                operator_ids=ids,
                reason="未识别到已支持的干员百科字段；请明确询问概况、技能、天赋、模组或基础属性。",
            )
    skill_index = None
    match = re.search(r"(?<![专精0-9])(?:第)?([一二三123])(?:技(?:能)?|技能)|技能\s*([123])(?![0-9])", condition_question)
    if match:
        skill_index = _number_token(match[1] or match[2])
    # S1/S2/S3 are common player spellings; names remain exact-release matches.
    match = re.search(r"(?i)(?:^|\W)s([123])(?:\W|$)", condition_question)
    if match:
        skill_index = int(match[1])
    mastery = re.search(r"专([一二三123])", condition_question)
    rank_question = re.sub(r"精(?:英)?[零一二012]\s*(?:等级)?\s*\d{1,3}\s*(?:级)?", "", condition_question)
    rank_question = re.sub(r"(?:干员|角色)\s*(?:等级|级别)\s*\d+(?:\s*级)?", "", rank_question)
    rank_match = re.search(r"(?<![\d])([1-9]|10)\s*级(?!别)(?:技能)?", rank_question)
    rank = 7 + _number_token(mastery[1]) if mastery else None
    if rank is None and rank_match and intent == "operator_skill":
        rank = int(rank_match[1])
    if "专精三" in condition_question:
        rank = 10
    elif "专精二" in condition_question:
        rank = 9
    elif "专精一" in condition_question:
        rank = 8
    if rank is None and intent == "operator_skill":
        prefixed_rank = re.search(
            r"(?:技能\s*(?:[123](?![0-9]))?|(?i:\bs[123]))\s*(?:的\s*)?"
            r"(?:等级|级别)\s*(-?[0-9]+(?:\.[0-9]+)?|[零一二三四五六七八九十百]+)",
            rank_question,
        )
        if prefixed_rank:
            rank = _number_token(prefixed_rank[1])
            if rank is None or not 1 <= rank <= 10:
                return _plan(intent="unsupported", operator_ids=ids,
                             reason="技能等级必须是 1 至 10；无法忽略或截断明确指定的来源范围外条件。")
    phase = re.search(r"精(?:英)?([零一二012])", condition_question)
    phase_number = {"零": 0, "一": 1, "二": 2}.get(phase[1], None) if phase else None
    if phase and phase_number is None:
        phase_number = int(phase[1])
    level_match = re.search(r"精(?:英)?[零一二012]\s*(?:等级)?\s*(\d{1,3})\s*(?:级)?", condition_question)
    level = int(level_match[1]) if level_match else ("max" if "满级" in condition_question else None)
    potential_token = r"(-?[0-9]+(?:\.[0-9]+)?|[零一二三四五六七八九十百]+)"
    potential = re.search(r"(?:潜能?|潜力)\s*" + potential_token, condition_question)
    if potential is None:
        potential = re.search(potential_token + r"\s*潜", condition_question)
    potential_number = _number_token(potential[1]) if potential else None
    if potential and (potential_number is None or not 1 <= potential_number <= 6):
        return _plan(intent="unsupported", operator_ids=ids,
                     reason="潜能条件必须是 1 至 6；无法忽略或截断明确指定的来源范围外条件。")
    potential_rank = potential_number - 1 if potential else None
    if "满潜" in condition_question:
        potential_rank = 5
    module_matches = [
        m["name"]
        for m in modules
        if m["name"] and m["name"] in question and (not ids or m["parent_native_id"] in ids)
    ]
    module_matches = [
        name
        for name in module_matches
        if not any(name != other and name in other for other in module_matches)
    ]
    module_query = module_matches[0] if len(set(module_matches)) == 1 else None
    # A real, exact module name owns its span, even if it contains a code token.
    if not module_matches:
        module_query = _module_code(question, modules)
    module_match = re.search(r"(?<![0-9])([一二三123])\s*级", rank_question) if module_requested else None
    if module_match is None and intent == "operator_module":
        module_match = re.search(r"(?i)(?:level|lv\.?)\s*([123])(?![0-9])", rank_question)
    result = _plan(
        intent=intent,
        operator_ids=ids,
        skill_index=skill_index,
        skill_name=skill_matches[0]["name"]
        if len({s["name"] for s in skill_matches}) == 1
        else None,
        rank=rank,
        elite_phase=f"PHASE_{phase_number}" if phase else None,
        level=level,
        potential_rank=potential_rank,
        module_query=module_query,
        module_level=_number_token(module_match[1]) if module_match else None,
        compare=bool(re.search(r"对比|比较|区别|差异|相比", semantic_question)),
    )
    if not ids and unmatched_subject:
        if _complete_operator_catalog(release, operators):
            result.update(
                intent="unsupported",
                reason="明确指定的名称未匹配本发布版本的完整干员目录；无法提供该版本的所请求字段。请核对全名或选择其他版本。",
            )
        else:
            result["reason"] = (
                "名称未匹配本发布版本目录，且目录覆盖尚不完整；请核对干员全名或补充来源。"
            )
    return result


def validate_plan(plan: dict) -> dict:
    if not isinstance(plan, dict) or set(plan) - PLAN_KEYS:
        raise InvalidData("question plan has unknown fields")
    result = _plan(**plan)
    if result["intent"] not in INTENTS:
        raise InvalidData("unsupported question plan intent")
    if (
        not isinstance(result["operator_ids"], list)
        or any(not isinstance(i, str) for i in result["operator_ids"])
        or len(result["operator_ids"]) > 2
        or len(set(result["operator_ids"])) != len(result["operator_ids"])
    ):
        raise InvalidData("question plan requires at most two distinct operator IDs")
    for key, minimum, maximum in (
        ("skill_index", 1, 3),
        ("rank", 1, 10),
        ("potential_rank", 0, 5),
        ("module_level", 1, 3),
    ):
        value = result[key]
        if value is not None and (type(value) is not int or not minimum <= value <= maximum):
            raise InvalidData(f"invalid plan {key}")
    if result["elite_phase"] not in (None, "PHASE_0", "PHASE_1", "PHASE_2"):
        raise InvalidData("invalid plan elite_phase")
    level = result["level"]
    if level not in (None, "max") and (type(level) is not int or not 1 <= level <= 100):
        raise InvalidData("invalid plan level")
    for key in ("skill_name", "module_query", "reason"):
        if result[key] is not None and (not isinstance(result[key], str) or len(result[key]) > 500):
            raise InvalidData(f"invalid plan {key}")
    result["module_query"] = _normalize_module_query(result["module_query"])
    if type(result["compare"]) is not bool:
        raise InvalidData("invalid plan compare")
    return result


def render_description(template: str, blackboard: list) -> dict:
    """Allow-listed display substitutions only; unknown tokens remain visible."""
    if not isinstance(template, str) or not isinstance(blackboard, list):
        raise InvalidData("description and blackboard have invalid shapes")
    values, ambiguous = {}, set()
    for entry in blackboard:
        key = entry.get("key") if isinstance(entry, dict) else None
        if not isinstance(key, str):
            continue
        if key.lower() in values:
            ambiguous.add(key.lower())
        values[key.lower()] = entry.get("value")
    unresolved, used = [], []

    def replace(match):
        token = match[1]
        grammar = re.fullmatch(r"(-?)([A-Za-z_][A-Za-z0-9_.]*)(?::(0(?:\.0{1,4})?%?))?", token)
        if grammar is None:
            unresolved.append(match[0])
            return match[0]
        minus, key, fmt = grammar.groups()
        value = values.get(key.lower())
        if (
            key.lower() in ambiguous
            or not isinstance(value, (int, float))
            or isinstance(value, bool)
        ):
            unresolved.append(match[0])
            return match[0]
        try:
            number = Decimal(str(value))
            if not number.is_finite():
                raise InvalidOperation
            if minus:
                number = -number
            if fmt and fmt.endswith("%"):
                number *= 100
            if fmt:
                precision = len(fmt.rstrip("%").split(".")[1]) if "." in fmt else 0
                text = f"{number:.{precision}f}" + ("%" if fmt.endswith("%") else "")
            else:
                text = format(number.normalize(), "f")
        except (InvalidOperation, ValueError):
            unresolved.append(match[0])
            return match[0]
        used.append({"key": key, "value": value, "format": fmt, "negated": bool(minus)})
        return text

    rendered = re.sub(r"\{([^{}]+)\}", replace, template)
    # These are client rich-text spans. Do not render arbitrary HTML in answers.
    rendered = re.sub(r"<(?:[@$][A-Za-z0-9_.]+|/)>", "", rendered).replace("\\n", "\n")
    return {
        "text": html.escape(rendered, quote=False),
        "raw_template": template,
        "unresolved_templates": list(dict.fromkeys(unresolved)),
        "substitutions": used,
        "interpretation": "client_description_display_only",
    }


class _Answer:
    def __init__(self, session, release, question, store, plan):
        self.session, self.release, self.store = session, release, store
        self.result = {
            "status": "answered",
            "question": question,
            "answer": "",
            "claims": [],
            "citations": [],
            "tool_trace": [{"tool": "plan_question", "plan": plan}],
            "release_id": release.id,
            "manifest_hash": release.manifest_hash,
            "version": {
                "release_id": release.id,
                "manifest_hash": release.manifest_hash,
                "source_revision": release.manifest.get("datasets", {})
                .get("operator", {})
                .get("source_revision"),
                "effective_date_verified": False,
            },
            "scope": release.manifest.get("datasets", {}).get("operator", {}).get("scope", {}),
            "coverage": release.manifest.get("datasets", {})
            .get("operator", {})
            .get("coverage", {}),
            "limitations": ["回答适用于指定来源修订；未确认它是当前游戏生效版本。"],
        }
        self.cache = {}
        self.documents = {}

    def refuse(self, message, status="needs_clarification"):
        self.result.update(status=status, answer=message, claims=[], citations=[])
        return self.result

    def rows(self, kind):
        if kind not in self.cache:
            self.cache[kind] = _records(self.session, self.release, kind)
            self.result["tool_trace"].append(
                {
                    "tool": "list_records",
                    "release_id": self.release.id,
                    "module": "operator",
                    "kind": kind,
                    "count": len(self.cache[kind]),
                }
            )
        return self.cache[kind]

    def cite(self, record, selector, expected):
        for evidence_id in record["evidence_ids"]:
            evidence = services.get_evidence(self.session, self.release, evidence_id)
            if services.content_digest(evidence["excerpt"]) != evidence["excerpt_hash"]:
                raise Conflict("answer evidence failed excerpt hash verification")
            try:
                value = services.json_pointer(evidence["excerpt"], selector)
            except InvalidData:
                continue
            if not services.same_json_value(value, expected):
                continue
            snapshot = services.get_snapshot(self.session, self.release, evidence["snapshot_id"])
            full_pointer = evidence["pointer"] + selector
            if self.store is not None:
                digest = snapshot["content_hash"]
                if digest not in self.documents:
                    raw = self.store.read_verified(digest)
                    if len(raw) != snapshot["byte_size"]:
                        raise Conflict("answer source snapshot size mismatch")
                    self.documents[digest] = json.loads(raw)
                if not services.same_json_value(
                    services.json_pointer(self.documents[digest], evidence["pointer"]),
                    evidence["excerpt"],
                ):
                    raise Conflict("answer evidence excerpt does not match original source")
                if not services.same_json_value(
                    services.json_pointer(self.documents[digest], full_pointer), expected
                ):
                    raise Conflict("answer field does not match original source")
            citation = {
                "id": f"c{len(self.result['citations']) + 1}",
                "record_id": record["id"],
                "evidence_id": evidence_id,
                "dataset_id": record["dataset_id"],
                "snapshot_id": evidence["snapshot_id"],
                "source_url": evidence["source_url"],
                "source_revision": evidence["source_revision"],
                "snapshot_hash": snapshot["content_hash"],
                "pointer": full_pointer,
                "evidence_pointer": evidence["pointer"],
                "excerpt_hash": evidence["excerpt_hash"],
                "field_value": expected,
                "field_hash": services.content_digest(expected),
            }
            for existing in self.result["citations"]:
                if (
                    existing["snapshot_id"] == citation["snapshot_id"]
                    and existing["pointer"] == full_pointer
                ):
                    return existing["id"]
            self.result["citations"].append(citation)
            return citation["id"]
        raise Conflict("answer fact has no matching field-level source evidence")

    def claim(self, record, label, selector, value=None, derived=None):
        raw_value = services.json_pointer(record["data"], selector)
        citation_id = self.cite(record, selector, raw_value)
        claim = {
            "id": f"f{len(self.result['claims']) + 1}",
            "record_id": record["id"],
            "label": label,
            "value": raw_value if value is None else value,
            "citation_ids": [citation_id],
        }
        if derived:
            claim["derived"] = derived
        self.result["claims"].append(claim)
        return claim

    def description(self, record, selector, blackboard_selector, label):
        template = services.json_pointer(record["data"], selector)
        blackboard = services.json_pointer(record["data"], blackboard_selector)
        if template is None:
            self.claim(record, label, selector)
            self.result["limitations"].append("部分来源描述为空，未补写技能或天赋效果。")
            return "来源未提供可读原描述"
        rendered = render_description(template, blackboard)
        claim = self.claim(
            record,
            label,
            selector,
            rendered["text"],
            {
                "operation": "allowlisted_client_template_display",
                "substitutions": rendered["substitutions"],
                "unresolved_templates": rendered["unresolved_templates"],
            },
        )
        claim["citation_ids"].append(self.cite(record, blackboard_selector, blackboard))
        if rendered["unresolved_templates"]:
            self.result["limitations"].append(
                "描述包含未识别模板，已保留原文；该字段未被完整渲染。"
            )
        return rendered["text"]


def _phase_label(phase):
    return {"PHASE_0": "未精英", "PHASE_1": "精英一", "PHASE_2": "精英二"}.get(phase, phase)


def _skill_description_display(template, blackboard, claim):
    """Display literal same-source ``@`` keys without changing audited claims.

    This is a key alias, not attribute access or an interpretation of a skill.
    The original description claim and its independently replayed derivation
    stay untouched. Unknown or conflicting tokens never become visible facts.
    """
    keys = {}
    for index, entry in enumerate(blackboard):
        if isinstance(entry, dict) and isinstance(entry.get("key"), str):
            keys.setdefault(entry["key"].lower(), []).append((index, entry))
    occupied = set(keys)
    occupied.update(token.lower() for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", template))
    aliases = {}

    def alias_token(match):
        parsed = re.fullmatch(
            r"(-?)([A-Za-z][A-Za-z0-9_]*@[A-Za-z][A-Za-z0-9_]*)(?::(0(?:\.0{1,4})?%?))?",
            match[1],
        )
        if parsed is None:
            return match[0]
        minus, key, fmt = parsed.groups()
        candidates = keys.get(key.lower(), [])
        if "__" in key or len(candidates) != 1 or candidates[0][1]["key"] != key:
            return match[0]
        alias = "__skill_display_" + services.content_digest(key)
        if alias.lower() in occupied:
            return match[0]
        index, entry = candidates[0]
        aliases[key] = {
            "source_key": key,
            "blackboard_index": index,
            "alias": alias,
            "value": entry.get("value"),
        }
        return "{" + minus + alias + (":" + fmt if fmt else "") + "}"

    display_template = re.sub(r"\{([^{}]+)\}", alias_token, template)
    display_board = [dict(entry) if isinstance(entry, dict) else entry for entry in blackboard]
    for alias in aliases.values():
        display_board[alias["blackboard_index"]]["key"] = alias["alias"]
    rendered = render_description(display_template, display_board)
    # Alias names are an internal implementation detail; evidence retains the
    # literal unsupported source token rather than a newly invented template.
    unresolved = rendered["unresolved_templates"]
    for alias in aliases.values():
        unresolved = [token.replace(alias["alias"], alias["source_key"]) for token in unresolved]
    complete = not unresolved and not re.search(r"[{}]", rendered["text"])
    return {
        "operation": "literal_blackboard_key_alias_display",
        "source_claim_id": claim["id"],
        "citation_ids": list(claim["citation_ids"]),
        "raw_template_hash": services.content_digest(template),
        "blackboard_hash": services.content_digest(blackboard),
        "key_aliases": list(aliases.values()),
        "substitutions": rendered["substitutions"],
        "unresolved_templates": unresolved,
        "complete": complete,
        "text": (
            rendered["text"] if complete else "部分描述未展开，可查看原文及参数引用。"
        ),
    }


def _skill(answer, operators, plan, *, include_blackboard=False):
    if plan["rank"] is None:
        return answer.refuse("请补充技能等级，例如 7 级、专一、专二或专三。")
    lines = []
    for operator in operators:
        refs = operator["data"].get("skills", [])
        owned_ids = {ref.get("skillId") for ref in refs}
        skills = [s for s in answer.rows("skill") if s["native_id"] in owned_ids]
        if plan["skill_index"] is not None:
            index = plan["skill_index"] - 1
            if index >= len(refs):
                return answer.refuse("该干员在本版本没有这个技能序号。", "unsupported")
            skill_id = refs[index].get("skillId")
            skills = [s for s in skills if s["native_id"] == skill_id]
        if plan["skill_name"]:
            skills = [s for s in skills if s["name"] == plan["skill_name"]]
        if len(skills) != 1:
            if not skills:
                return answer.refuse("所选技能不在本发布版本覆盖范围中。", "unsupported")
            return answer.refuse(f"请明确 {operator['name']} 的技能序号或名称。")
        skill = skills[0]
        slot = next(i for i, ref in enumerate(refs) if ref.get("skillId") == skill["native_id"])
        answer.claim(operator, "干员技能槽关联", f"/skills/{slot}/skillId")
        index = plan["rank"] - 1
        if index >= len(skill["data"].get("levels", [])):
            return answer.refuse("来源数据没有所选技能等级。", "unsupported")
        level = skill["data"]["levels"][index]
        base = f"/levels/{index}"
        answer.claim(operator, "干员名称", "/name")
        name = answer.claim(skill, "技能名称", base + "/name")["value"]
        rank_label = {8: "专一", 9: "专二", 10: "专三"}.get(plan["rank"], f"{plan['rank']} 级")
        answer.claim(
            skill,
            "技能等级",
            "/levels",
            plan["rank"],
            {
                "operation": "one_based_level_index",
                "source_index": index,
                "display": rank_label,
                "mastery_mapping": {"8": "专一", "9": "专二", "10": "专三"},
            },
        )
        text = answer.description(skill, base + "/description", base + "/blackboard", "技能描述")
        if isinstance(level.get("description"), str):
            proof = _skill_description_display(
                level["description"], level["blackboard"], answer.result["claims"][-1]
            )
            answer.result.setdefault("display_proof", []).append(proof)
            text = proof["text"]
        board_text = ""
        if include_blackboard:
            board = answer.claim(skill, "技能原始描述黑板", base + "/blackboard")["value"]
            board_text = "\n原始黑板：" + services.canonical_json(board)
        values = {}
        for label, path in (
            ("技力消耗", "/spData/spCost"),
            ("初始技力", "/spData/initSp"),
            ("duration 原始字段", "/duration"),
        ):
            values[label] = answer.claim(skill, label, base + path)["value"]
        sp = level["spData"]["spType"]
        trigger = level["skillType"]
        sp_display = SP_TYPES.get(sp, f"未核验回复类型（来源编码 {sp}）")
        trigger_display = SKILL_TYPES.get(trigger, f"未核验触发类型（来源编码 {trigger}）")
        answer.claim(
            skill,
            "技力回复类型",
            base + "/spData/spType",
            sp_display,
            {"operation": "enum_display", "raw": sp},
        )
        answer.claim(
            skill,
            "技能触发类型",
            base + "/skillType",
            trigger_display,
            {"operation": "enum_display", "raw": trigger},
        )
        lines.append(
            f"{operator['name']} · {name} · {rank_label}\n{text}\n"
            f"{sp_display}，{trigger_display}；"
            f"技力消耗 {values['技力消耗']}，初始技力 {values['初始技力']}。{board_text}"
        )
    answer.result["limitations"].append(
        "技能描述按客户端模板展示；duration=0 未被解释成技能效果持续 0 秒。"
    )
    answer.result["answer"] = "\n\n".join(lines)
    return answer.result


def _talent_source_identity(answer, operator, record):
    """Bind an ingested candidate to its original family, not its display name."""
    parent = operator["native_id"]
    match = re.fullmatch(
        re.escape(parent) + r":talent:(0|[1-9][0-9]*):candidate:(0|[1-9][0-9]*)",
        record["native_id"],
    )
    if match is None or record["parent_native_id"] != parent:
        raise Conflict("talent candidate has an invalid source family identity")
    family, candidate = map(int, match.groups())
    talents = operator["data"].get("talents")
    try:
        original = talents[family]["candidates"][candidate]
    except (TypeError, KeyError, IndexError):
        raise Conflict("talent candidate identity is absent from the original family") from None
    if not services.same_json_value(original, record["data"]):
        raise Conflict("talent candidate differs from its original family payload")
    root = "/" + parent.replace("~", "~0").replace("/", "~1")
    pointer = f"{root}/talents/{family}/candidates/{candidate}"
    for evidence_id in record["evidence_ids"]:
        evidence = services.get_evidence(answer.session, answer.release, evidence_id)
        if evidence["pointer"] != pointer:
            continue
        if (
            services.content_digest(evidence["excerpt"]) != evidence["excerpt_hash"]
            or not services.same_json_value(evidence["excerpt"], original)
        ):
            raise Conflict("talent family identity has inconsistent original evidence")
        if answer.store is not None:
            snapshot = services.get_snapshot(
                answer.session, answer.release, evidence["snapshot_id"]
            )
            digest = snapshot["content_hash"]
            if digest not in answer.documents:
                raw = answer.store.read_verified(digest)
                if len(raw) != snapshot["byte_size"]:
                    raise Conflict("talent family source snapshot size mismatch")
                answer.documents[digest] = json.loads(raw)
            document = answer.documents[digest]
            if (
                not services.same_json_value(services.json_pointer(document, pointer), original)
                or not services.same_json_value(
                    services.json_pointer(document, root + "/talents"), talents
                )
            ):
                raise Conflict("talent family identity differs from the original snapshot")
        return family, candidate
    raise Conflict("talent candidate lacks its original source family pointer")


def _talent_description_display(template, blackboard, claim):
    """Hide only adjacent numeric client upgrade annotations, not source facts.

    Selected source candidates already contain their final values. The client
    also decorates them with potential-upgrade deltas. Keep the original claim
    and citations intact and record every removed source span for display replay.
    Non-numeric, detached or malformed spans retain their original text.
    """
    removed, pieces, cursor = [], [], 0
    for match in re.finditer(r"<@ba\.talpu>([^<>]*)</>", template):
        if not re.fullmatch(r"(?:（[+-][0-9]+(?:\.[0-9]+)?%?）|\([+-][0-9]+(?:\.[0-9]+)?%?\))", match[1]):
            continue
        if not re.search(r"(?:[0-9]%?|\})$", template[:match.start()]):
            continue
        pieces.append(template[cursor:match.start()])
        cursor = match.end()
        removed.append({"start": match.start(), "end": match.end(), "raw": match[0]})
    pieces.append(template[cursor:])
    rendered = render_description("".join(pieces), blackboard)
    return {
        "operation": "client_talent_potential_annotation_display",
        "source_claim_id": claim["id"], "citation_ids": list(claim["citation_ids"]),
        "raw_template_hash": services.content_digest(template),
        "blackboard_hash": services.content_digest(blackboard),
        "removed_annotation_spans": removed,
        "substitutions": rendered["substitutions"],
        "unresolved_templates": rendered["unresolved_templates"],
        "text": rendered["text"],
        "source_values_recomputed": False,
    }


def _talent(answer, operators, plan, *, include_blackboard=None):
    show_blackboard = (
        bool(re.search(r"黑板|(?i:blackboard)", answer.result["question"]))
        if include_blackboard is None else include_blackboard
    )
    lines = []
    for operator in operators:
        parent = operator["native_id"]
        candidates = [
            r
            for r in answer.rows("talent")
            if r["parent_native_id"] == parent or r["native_id"].startswith(parent + ":talent:")
        ]
        original_families = operator["data"].get("talents")
        legacy_families = original_families is None or (
            isinstance(original_families, list) and not original_families
        )
        # Older generic synthetic/partial formats did not store native families.
        # An actual source family or an asserted native identity must never fall
        # back to grouping by a name when its source identity is damaged.
        canonical = not legacy_families or any(
            ":talent:" in record["native_id"] for record in candidates
        )
        identities = {}
        if canonical:
            for record in candidates:
                identities[record["id"]] = _talent_source_identity(answer, operator, record)
            try:
                visible = {
                    (family, candidate)
                    for family, raw_family in enumerate(original_families)
                    for candidate, raw in enumerate(raw_family.get("candidates") or [])
                    if raw.get("isHideTalent") is not True
                }
            except (TypeError, AttributeError):
                raise Conflict("talent original family structure is invalid") from None
            if not visible.issubset(set(identities.values())):
                raise Conflict("talent original family candidate identities are incomplete")
            candidates.sort(key=lambda record: identities[record["id"]])
        if not candidates:
            return answer.refuse("本发布版本未覆盖该干员的天赋候选。", "unsupported")
        # Source gates stay distinct; choosing an eligible potential branch does
        # not infer cross-phase inheritance, module effects or combat stacking.
        if plan["elite_phase"]:
            candidates = [
                r
                for r in candidates
                if r["data"].get("unlockCondition", {}).get("phase") == plan["elite_phase"]
            ]
        if type(plan["level"]) is int:
            candidates = [
                r
                for r in candidates
                if r["data"].get("unlockCondition", {}).get("level", 0) <= plan["level"]
            ]
        if plan["potential_rank"] is not None:
            candidates = [
                r
                for r in candidates
                if r["data"].get("requiredPotentialRank") <= plan["potential_rank"]
            ]
            grouped = {}
            for record in candidates:
                data = record["data"]
                # Distinct source unlock levels/prefabs remain separate gates;
                # a potential upgrade may rename a candidate within one gate.
                key = (
                    identities[record["id"]][0] if canonical else record["name"],
                    data.get("unlockCondition", {}).get("phase"),
                    data.get("unlockCondition", {}).get("level"),
                    data.get("prefabKey"),
                )
                previous = grouped.get(key)
                if (
                    previous is None
                    or data["requiredPotentialRank"]
                    > previous[0]["data"]["requiredPotentialRank"]
                ):
                    grouped[key] = [record]
                elif (
                    canonical
                    and data["requiredPotentialRank"]
                    == previous[0]["data"]["requiredPotentialRank"]
                ):
                    grouped[key].append(record)
            if any(len(records) != 1 for records in grouped.values()):
                return answer.refuse(
                    "同一天赋家族的同一解锁条件存在多个最高潜能候选，来源不足以唯一选择分支。",
                    "needs_verification",
                )
            candidates = [records[0] for records in grouped.values()]
        if not candidates:
            return answer.refuse(
                "未找到匹配条件的天赋候选；不会据此推断天赋不存在。", "unsupported"
            )
        answer.claim(operator, "干员名称", "/name")
        requested = {key: plan[key] for key in ("elite_phase", "level", "potential_rank")}
        answer.result.setdefault("talent_selection", []).append({
            "operator_name": operator["name"], "operator_native_id": parent,
            "requested": requested, "potential_rank_basis": "zero_based",
            "display_potential": (
                plan["potential_rank"] + 1 if plan["potential_rank"] is not None else None
            ),
            "selection_policy": (
                "all_source_candidates_matching_requested_phase_and_level"
                if plan["potential_rank"] is None
                else "highest_eligible_potential_per_source_unlock_gate"
            ),
            "cross_gate_override_inferred": False,
            "selected": [{
                "record_id": record["id"], "native_id": record["native_id"],
                **({"family_index": identities[record["id"]][0],
                    "candidate_index": identities[record["id"]][1]} if canonical else {}),
                "unlock_condition": record["data"]["unlockCondition"],
                "required_potential_rank": record["data"]["requiredPotentialRank"],
            } for record in candidates],
        })
        if plan["potential_rank"] is None:
            lines.append(operator["name"] + " 的来源天赋候选：")
        else:
            labels = ([_phase_label(plan["elite_phase"])] if plan["elite_phase"] else [])
            labels.append(f"潜能 {plan['potential_rank'] + 1}")
            lines.append(operator["name"] + " · " + " · ".join(labels) + " 的天赋：")
        for record in candidates:
            data = record["data"]
            name = answer.claim(record, "天赋名称", "/name")["value"]
            if name is None:
                name = "来源未命名天赋"
            condition = answer.claim(record, "天赋解锁条件", "/unlockCondition")["value"]
            potential = answer.claim(
                record,
                "天赋潜能门槛",
                "/requiredPotentialRank",
                data["requiredPotentialRank"] + 1,
                {"operation": "zero_based_potential_rank_plus_one"},
            )["value"]
            text = answer.description(record, "/description", "/blackboard", "天赋描述")
            if canonical and plan["potential_rank"] is not None and isinstance(data.get("description"), str):
                proof = _talent_description_display(
                    data["description"], data["blackboard"], answer.result["claims"][-1]
                )
                answer.result.setdefault("display_proof", []).append(proof)
                text = proof["text"]
            lines.append(
                f"{name}（{_phase_label(condition['phase'])} {condition['level']} 级，潜能至少 {potential}）：{text}"
            )
            if show_blackboard:
                board = answer.claim(record, "天赋原始描述黑板", "/blackboard")["value"]
                lines.append("原始描述黑板：" + json.dumps(board, ensure_ascii=False))
    answer.result["limitations"].append(
        "按来源解锁条件展示；指定潜能时各解锁组仅保留不高于该潜能的最高分支。"
        "不推断模组覆盖、跨阶段继承或战斗中的叠加关系。"
    )
    answer.result["answer"] = "\n".join(lines)
    return answer.result


def _module(answer, operators, plan):
    if plan["module_level"] is None:
        return answer.refuse("请补充模组等级（1、2 或 3 级）以及模组名称或来源中的型号。")
    lines = []
    source_markers = []
    for operator in operators:
        modules = [
            r for r in answer.rows("module") if r["parent_native_id"] == operator["native_id"]
        ]
        query = plan["module_query"]
        if query:
            modules = [
                r
                for r in modules
                if query == r["name"]
                or query.upper() == str(r["data"].get("typeName2", "")).upper()
                or query == r["native_id"]
            ]
        if len(modules) != 1:
            if not modules:
                return answer.refuse("所选模组不在本发布版本覆盖范围中。", "unsupported")
            return answer.refuse("请明确模组名称或来源中的型号；该干员有多个模组。")
        module = modules[0]
        phases = [
            r
            for r in answer.rows("module_phase")
            if r["parent_native_id"] == module["native_id"]
            and r["data"].get("equipLevel") == plan["module_level"]
        ]
        if len(phases) != 1:
            return answer.refuse("来源数据未覆盖所选模组等级。", "unsupported")
        record = phases[0]
        answer.claim(operator, "干员名称", "/name")
        name = answer.claim(module, "模组名称", "/uniEquipName")["value"]
        level = answer.claim(record, "模组等级", "/equipLevel")["value"]
        lines.append(f"{operator['name']} · {name} · 模组 {level} 级")
        if "typeName1" in module["data"]:
            marker = answer.claim(module, "模组来源分支标识", "/typeName1")["value"]
            source_markers.append(
                {
                    "operator_native_id": operator["native_id"],
                    "module_native_id": module["native_id"],
                    "typeName1": marker,
                }
            )
            lines.append(f"来源分支标识：{marker}（尚未核验对应模式与适用关卡）")
        stats = answer.claim(record, "模组属性黑板", "/attributeBlackboard")["value"]
        lines.append("属性黑板：" + "，".join(f"{s['key']}={s['value']}" for s in stats))
        for pi, part in enumerate(record["data"].get("parts", [])):
            for bundle_key in ("overrideTraitDataBundle", "addOrOverrideTalentDataBundle"):
                bundle = part.get(bundle_key) or {}
                for ci, candidate in enumerate(bundle.get("candidates") or []):
                    base = f"/parts/{pi}/{bundle_key}/candidates/{ci}"
                    for key in (
                        "additionalDescription",
                        "overrideDescripton",
                        "upgradeDescription",
                        "description",
                    ):
                        if not isinstance(candidate.get(key), str) or not candidate[key]:
                            continue
                        text = answer.description(
                            record, base + "/" + key, base + "/blackboard", "模组条件描述"
                        )
                        condition = answer.claim(
                            record, "模组描述解锁条件", base + "/unlockCondition"
                        )["value"]
                        potential = answer.claim(
                            record,
                            "模组描述潜能门槛",
                            base + "/requiredPotentialRank",
                            candidate["requiredPotentialRank"] + 1,
                            {"operation": "zero_based_potential_rank_plus_one"},
                        )["value"]
                        lines.append(
                            f"{_phase_label(condition['phase'])} {condition['level']} 级，潜能至少 {potential}：{text}"
                        )
    answer.result["limitations"].append(
        "模组各条件分支分别展示；未把模组属性与干员面板相加，也未模拟实际战斗规则。"
    )
    answer.result["limitations"].append(
        "模组分支标识仅按来源原值展示，尚未核验对应模式与适用关卡；发布版本的作战范围标记不代表每个模组均在普通作战生效。"
    )
    answer.result["module_applicability"] = {
        "verified": False,
        "source_markers": source_markers,
    }
    answer.result["answer"] = "\n".join(lines)
    return answer.result


def _attributes(answer, operators, plan):
    if plan["elite_phase"] is None or plan["level"] is None:
        return answer.refuse(
            "请补充精英阶段和等级，例如“精二满级基础属性”或“精一 1 级属性”；此处查询未叠加信赖、潜能、模组的基础关键帧。"
        )
    phase_index = int(plan["elite_phase"][-1])
    lines = []
    for operator in operators:
        phases = operator["data"].get("phases", [])
        if phase_index >= len(phases):
            return answer.refuse("本发布版本没有该干员的所选精英阶段。", "unsupported")
        phase = phases[phase_index]
        level = phase["maxLevel"] if plan["level"] == "max" else plan["level"]
        frames = phase.get("attributesKeyFrames", [])
        matched = [(i, frame) for i, frame in enumerate(frames) if frame.get("level") == level]
        if len(matched) != 1:
            return answer.refuse(
                "当前仅支持来源中直接存储的基础属性关键帧（通常为 1 级或满级）；未核验中间等级的插值与取整规则。",
                "unsupported",
            )
        index, frame = matched[0]
        answer.claim(operator, "干员名称", "/name")
        answer.claim(
            operator, "基础属性等级", f"/phases/{phase_index}/attributesKeyFrames/{index}/level"
        )
        if plan["level"] == "max":
            answer.claim(operator, "精英阶段满级", f"/phases/{phase_index}/maxLevel")
        values = []
        for key, label in ATTRIBUTES.items():
            if key in frame["data"]:
                value = answer.claim(
                    operator, label, f"/phases/{phase_index}/attributesKeyFrames/{index}/data/{key}"
                )["value"]
                values.append(f"{label} {value}")
        lines.append(
            f"{operator['name']} · {_phase_label(plan['elite_phase'])} {level} 级基础关键帧："
            + "，".join(values)
        )
    answer.result["limitations"].append("基础属性未计入信赖、潜能、模组、天赋和场上增益。")
    answer.result["answer"] = "\n".join(lines)
    return answer.result


def _overview(answer, operators, plan):
    lines = []
    for operator in operators:
        raw = operator["data"]
        name = answer.claim(operator, "干员名称", "/name")["value"]
        rarity_raw = raw["rarity"]
        if not re.fullmatch(r"TIER_[1-6]", rarity_raw):
            return answer.refuse("来源星级编码尚未核验。", "unsupported")
        rarity = answer.claim(
            operator, "星级", "/rarity", int(rarity_raw[-1]), {"operation": "TIER_n_to_stars"}
        )["value"]
        profession = answer.claim(
            operator,
            "职业",
            "/profession",
            PROFESSIONS.get(raw["profession"], raw["profession"]),
            {"operation": "enum_display", "raw": raw["profession"]},
        )["value"]
        position = answer.claim(
            operator,
            "部署位置",
            "/position",
            POSITIONS.get(raw["position"], raw["position"]),
            {"operation": "enum_display", "raw": raw["position"]},
        )["value"]
        skill_names = []
        all_skills = {s["native_id"]: s for s in answer.rows("skill")}
        for index, ref in enumerate(raw.get("skills", [])):
            skill = all_skills.get(ref.get("skillId"))
            if skill is not None:
                answer.claim(operator, "干员技能槽关联", f"/skills/{index}/skillId")
                skill_names.append(answer.claim(skill, "技能名称", "/levels/0/name")["value"])
        lines.append(
            f"{name}：{rarity} 星，{profession}，{position}。技能：" + "、".join(skill_names)
        )
    answer.result["answer"] = "\n".join(lines)
    return answer.result


def answer_question(
    session, release, question: str, store=None, plan=None, *, structured_query=False,
    include_blackboard=None,
) -> dict:
    """Build a factual answer from one pinned release, with field-level citations.

    Optional model plans are validated here and can never provide answer facts.
    This function performs no network calls, state mutation or active-release reads.
    """
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        raise InvalidData("question must contain 1 to 2000 characters")
    if structured_query and plan is None:
        raise InvalidData("a structured source query requires a typed plan")
    if include_blackboard is not None and type(include_blackboard) is not bool:
        raise InvalidData("include_blackboard must be a boolean or absent")
    # Live model semantics are a query proposal, not facts. Re-parsing the raw
    # utterance here would make local language coverage a second semantic gate.
    # The common source handlers below still validate IDs, branches and proof.
    local = validate_plan(plan) if structured_query else plan_question(session, release, question)
    chosen = validate_plan(plan if plan is not None else local)
    answer = _Answer(session, release, question, store, chosen)
    requested_subject = None
    if not structured_query and not local["operator_ids"] and "operator" in release.manifest.get("datasets", {}):
        catalog = answer.rows("operator")
        requested_subject = _literal_operator_subject(
            question,
            catalog,
            answer.rows("module") if re.search(MODULE_WORD, _module_request_text(question)) else (),
            answer.rows("skill"),
        )
        if (
            requested_subject in {o["name"] for o in catalog}
            or _unresolved_multi_request(question, catalog)
            or (requested_subject and requested_subject not in question)
        ):
            requested_subject = None
    if not local["operator_ids"]:
        # Rejected planner IDs are proposals, never resolved input identities.
        answer.result["tool_trace"][0]["plan"] = local
    if not local["operator_ids"] and requested_subject:
        start = question.find(requested_subject)
        answer.result["request_context"] = {
            "operator_names": [requested_subject],
            "origin": "user_question_literal",
            "subject_spans": [[start, start + len(requested_subject)]],
            "identity_resolution": "not_matched_in_pinned_catalog",
            "identity_verified": False,
            "release_id": release.id,
        }
    if not structured_query and _unsupported_question(question) and local["intent"] == "unsupported":
        return answer.refuse(
            "该问题需要尚未核验的战斗计算或当前生效版本；无法仅凭来源字段给出可靠结论。",
            "unsupported",
        )
    if not structured_query and local["intent"] == "unsupported":
        return answer.refuse(local["reason"] or "此问题超出干员百科字段查询范围。", "unsupported")
    if not structured_query and not local["operator_ids"] and (_requested_subjects(question) or local["reason"]):
        # A planner cannot supply an identity the original request did not resolve.
        return answer.refuse(local["reason"] or "请提供本发布版本可唯一匹配的干员全名或技能名称。")
    if plan is not None and not structured_query:
        # A planner can fill ambiguity, but cannot rewrite explicit user choices.
        # Apply this at the tool boundary as well as at any model dispatcher.
        semantic_fields = (
            "rank",
            "skill_index",
            "elite_phase",
            "level",
            "potential_rank",
            "module_level",
            "skill_name",
            "module_query",
        )
        conflicts = [
            field
            for field in semantic_fields
            if local[field] is not None and chosen[field] != local[field]
        ]
        if local["intent"] != "operator_overview" and chosen["intent"] != local["intent"]:
            conflicts.append("intent")
        if local["compare"] and not chosen["compare"]:
            conflicts.append("compare")
        if conflicts:
            answer.result["tool_trace"].append(
                {"tool": "validate_explicit_question_conditions", "conflicting_fields": conflicts}
            )
            return answer.refuse(
                "规划结果与问题中明确指定的技能或培养条件不一致，请补充或修正条件。"
            )
    if chosen["intent"] == "unsupported":
        return answer.refuse(chosen["reason"] or "此问题超出干员百科字段查询范围。", "unsupported")
    if "operator" not in release.manifest.get("datasets", {}):
        return answer.refuse("该发布版本未包含干员数据。", "unsupported")
    available = {r["native_id"]: r for r in answer.rows("operator")}
    if not chosen["operator_ids"]:
        return answer.refuse(chosen["reason"] or "请提供本发布版本覆盖的干员全名或技能名称。")
    if any(ident not in available for ident in chosen["operator_ids"]):
        return answer.refuse("指定干员不在本发布版本覆盖范围中。", "unsupported")
    if plan is not None and not structured_query:
        explicit = _mentions(
            question,
            list(available.values()),
            answer.rows("module") if chosen["intent"] == "operator_module" else (),
        )
        if explicit and set(explicit) != set(chosen["operator_ids"]):
            return answer.refuse("规划结果与问题中明确指定的干员不一致，请补充或修正干员名称。")
    if len(chosen["operator_ids"]) > 1 and not chosen["compare"]:
        return answer.refuse("识别到多个干员，请指定一个干员，或明确要求对比。")
    operators = [available[ident] for ident in chosen["operator_ids"]]
    dispatch = {
        "operator_skill": _skill,
        "operator_talent": _talent,
        "operator_module": _module,
        "operator_attributes": _attributes,
        "operator_overview": _overview,
    }
    result = (
        _talent(answer, operators, chosen,
                include_blackboard=bool(include_blackboard) if structured_query else include_blackboard)
        if chosen["intent"] == "operator_talent"
        else _skill(answer, operators, chosen, include_blackboard=bool(include_blackboard))
        if chosen["intent"] == "operator_skill"
        else dispatch[chosen["intent"]](answer, operators, chosen)
    )
    if result["status"] == "answered" and (not result["claims"] or not result["citations"]):
        raise Conflict("a factual answer must have source claims and citations")
    result["tool_trace"].append(
        {
            "tool": "verify_field_evidence",
            "release_id": release.id,
            "citation_count": len(result["citations"]),
            "snapshot_verification": "sha256_and_json_pointer"
            if store
            else "sealed_excerpt_and_field",
        }
    )
    # Bind every successful user-facing answer to the version, without trusting
    # a language model to invent citation markers or a source revision.
    if result["status"] == "answered":
        result["answer"] += (
            f"\n\n版本：{release.id}；来源修订：{result['version']['source_revision']}。"
        )
        result["answer"] += "\n证据：" + "、".join(f"[{c['id']}]" for c in result["citations"])
    return result
