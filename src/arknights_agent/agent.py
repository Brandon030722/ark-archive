"""Bounded question orchestration, deterministic tools and replayable audit."""

import re

from arknights_agent import qa, services
from arknights_agent.errors import Conflict, InvalidData
from arknights_agent.models import QaRun, new_id
from arknights_agent.planner import plan_with_model


def _refusal(release, question, message, status="needs_clarification"):
    revisions = {
        module: entry["source_revision"]
        for module, entry in release.manifest.get("datasets", {}).items()
    }
    distinct = set(revisions.values())
    return {
        "question": question,
        "status": status,
        "answer": message,
        "release_id": release.id,
        "manifest_hash": release.manifest_hash,
        "version": {
            "release_id": release.id,
            "manifest_hash": release.manifest_hash,
            "source_revision": next(iter(distinct)) if len(distinct) == 1 else None,
            "source_revisions": revisions,
        },
        "claims": [],
        "citations": [],
        "tool_trace": [],
    }


def _recruitment_tags(session, release, question):
    from unicodedata import normalize

    from arknights_agent.recruitment import ALIASES

    catalog = services.list_records(
        session, release, module="recruitment", kind="recruitment_tag", limit=200
    )
    spellings = {row["name"]: row["name"] for row in catalog}
    spellings.update(ALIASES)
    question = normalize("NFKC", question)
    # An explicit list is an exact input, not a bag of recognized substrings.
    # Dropping one unknown token can produce a plausible but wrong intersection.
    # "最多选3个标签" is a request about selection size, not a tag declaration.
    # Recognize the earlier input clause (including "刷到了") before inspecting
    # the following duration and calculation instructions.
    declaration = r"(?:给定|提供|已有|当前(?:的)?|可选|屏幕上(?:的)?)?标签(?:组合)?(?:分别是|包括|是|为|有)?[:\s]*"
    drawn = r"(?:刷到(?:了|的是)?|出现(?:了|的是)?)(?:的)?(?:标签)?(?:分别是|包括|是|为|有)?[:\s]*"
    explicit = re.search(
        r"(?:公招|公开招募|招募)[:\s,]*(?:" + declaration + "|" + drawn + r")?"
        r"|"
        + drawn
        + r"|(?:给定|提供|已有|当前(?:的)?|可选|屏幕上(?:的)?)"
        + declaration
        + r"|标签(?:组合)?(?:分别是|包括|是|为|有)[:\s]*"
        + r"|标签(?:组合)?[:]\s*"
        + r"|^\s*标签(?:组合)?[:\s]*",
        question,
    )
    supplied = question[explicit.end() :] if explicit else question
    boundary = re.search(
        r"[。;!?\n]"
        r"|(?:[0-9零〇一二两三四五六七八九十百]+(?:\.[0-9]+)?\s*(?:个)?(?:小时|分钟)|[0-9]:[0-9]{2})"
        r"|(?:我(?:设(?:置|为)?|选(?:择)?)|(?:招募)?时长|(?:招募)?时间)"
        r"|(?:最多|至多)(?:可)?选(?:择)?[0-9一二两三四五六七八九十]+(?:个)?标签"
        r"|(?:能出|可以出|会出|有哪些|有什么|怎么算|能招到|帮我|请|计算|列出|组合结果|假设|按标签|按所选)",
        supplied,
    )
    if boundary:
        supplied = supplied[: boundary.start()]
    supplied = supplied.strip(" ，,、;。!?：:\"'“”‘’[]【】()（）")
    if not supplied:
        return []
    tokens = [
        token.strip(" \"'“”‘’[]【】()（）")
        for token in re.split(r"[、，,;/+\s]+|和|与", supplied)
        if token.strip()
    ]
    unknown = [token for token in tokens if token not in spellings]
    if unknown:
        raise InvalidData("未识别或不可选的公招标签：" + "、".join(unknown))
    return [spellings[token] for token in tokens]


def render_recruitment_answer(result, *, scope="all_combinations"):
    """Render the model-selected source projection, retaining the calculation."""
    from arknights_agent.recruitment_display import project_recruitment_display

    display = project_recruitment_display(result, scope)
    chosen = "、".join(result["input"]["normalized_tags"]) or "无标签"
    minutes = result["input"]["duration_minutes"]
    hours, remainder = divmod(minutes, 60)
    duration = (
        f"{hours}小时" + (f"{remainder}分钟" if remainder else "")
        if hours
        else f"{minutes}分钟"
    )
    lines = [
        f"标签：{chosen}；招募时长：{duration}（{minutes}分钟）。",
        (
            f"共{result['combination_count']}个组合，以下全部列出。候选与最低星级均以所选标签保留为条件。"
            if scope == "all_combinations" else
            "仅展示这组标签同时保留时的交集。候选与最低星级均以所选标签保留为条件。"
        ),
    ]
    for combo in display["combinations"]:
        label = " + ".join(combo["selected_tags"]) or "无标签"
        if combo["candidates"]:
            by_rarity = {}
            for candidate in combo["candidates"]:
                by_rarity.setdefault(candidate["rarity_stars"], []).append(candidate["name"])
            names = "；".join(
                f"{rarity}星：{'、'.join(by_rarity[rarity])}" for rarity in sorted(by_rarity)
            )
            outcome = (
                f"最低{combo['minimum_rarity_if_tags_retained']}星，"
                f"共{combo['candidate_count']}名候选（{names}）"
            )
        else:
            outcome = "所选标签全部保留时交集为空，无候选；不能据此预测实际掉标签结果"
        protection = "；该时长稀有标签受保护" if combo.get(
            "special_tags_protected_at_this_duration"
        ) else ""
        lines.append(f"- {label}：{outcome}{protection}。")
    lines.extend(f"注意：{warning}" for warning in result["warnings"])
    sources = []
    source_names = {
        "character_table.json": "干员数据",
        "gacha_table.json": "公招池与时间规则",
        "prts:recruitment:公开招募": "公开招募规则",
        "prts:recruitment:微件:HrCalculator": "公招标签与计算规则",
    }
    for index, source in enumerate(result["sources"], 1):
        url = source.get("page_url") or source["url"]
        key = source["source_key"]
        title = source_names.get(key) or source_names.get(key.rsplit("/", 1)[-1])
        revision = source["revision"]
        if re.fullmatch(r"[0-9a-f]{40,64}", revision):
            revision = revision[:8]
        sources.append(f"[{title or f'来源{index}'}]({url})（修订 {revision}）")
    lines.append("来源：" + "；".join(sources) + "。")
    return "\n".join(lines)


def _duration(question):
    clock = re.search(r"(?<!\d)([0-9])[:：]([0-5][0-9])(?!\d)", question)
    if clock:
        return int(clock[1]) * 60 + int(clock[2])

    def number(token):
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", token):
            return float(token)
        digits = {
            "零": 0,
            "〇": 0,
            "一": 1,
            "二": 2,
            "两": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
        }
        total, current = 0, 0
        for char in token:
            if char in digits:
                current = current * 10 + digits[char]
            else:
                total += (current or 1) * {"十": 10, "百": 100}[char]
                current = 0
        return total + current

    token = r"([0-9]+(?:\.[0-9]+)?|[零〇一二两三四五六七八九十百]+)"
    hour = re.search(token + r"\s*(?:个)?小时", question)
    minutes = re.search(token + r"\s*分钟?", question)
    if hour or minutes:
        value = (number(hour[1]) * 60 if hour else 0) + (number(minutes[1]) if minutes else 0)
        if isinstance(value, float) and not value.is_integer():
            raise InvalidData("公招时长必须是整数分钟")
        return int(value)
    if "小时" in question or "分钟" in question:
        raise InvalidData("未识别招募时长，请使用例如3小时50分钟或230分钟")
    return 540


def _safe_model_plan(local_plan, model_plan):
    if local_plan.get("intent") == "unsupported":
        return None
    if set(model_plan.get("operator_ids", [])) != set(local_plan.get("operator_ids", [])):
        return None
    # A source-correct answer at the wrong conditions is still a wrong answer.
    for key in (
        "intent",
        "compare",
        "skill_index",
        "skill_name",
        "rank",
        "elite_phase",
        "level",
        "potential_rank",
        "module_query",
        "module_level",
    ):
        if model_plan.get(key) != local_plan.get(key):
            return None
    model_plan["reason"] = local_plan.get("reason")
    return model_plan


def dispatch_question(
    session,
    release,
    question,
    store,
    settings,
    *,
    module="auto",
    planner="local",
    tags=None,
    duration_minutes=None,
):
    from arknights_agent.runtime.security import contains_secret

    if contains_secret(question) or contains_secret(tags):
        raise InvalidData("invalid or sensitive question")
    if not question.strip():
        raise InvalidData("question must not be empty")
    if module == "auto":
        from arknights_agent.roguelike import is_roguelike_question

        has_roguelike = is_roguelike_question(question)
        routing_question = re.sub(r'[“"「『《][^”"」』》]+[”"」』》]', " ", question)
        has_garrison = bool(
            re.search(
                r"卫戍|盟约|V2_[12]|act2autochess"
                if has_roguelike else
                r"卫戍|盟约|V2_[12]|act2autochess|商业包装方案|奥术法阵|奇迹|投资人",
                routing_question, re.I,
            )
        )
        has_recruitment = tags is not None or bool(
            re.search(r"公招|公开招募|招募标签|标签组合", question)
        )
        if sum((has_garrison, has_recruitment, has_roguelike)) > 1:
            return _refusal(release, question, "请分别提出各模式的问题，以明确规则适用范围。")
        module = (
            "roguelike" if has_roguelike else "garrison" if has_garrison
            else "recruitment" if has_recruitment else "operator"
        )
    if module == "roguelike":
        from arknights_agent.roguelike import answer_roguelike

        result = answer_roguelike(session, release, question, store=store)
    elif module == "garrison":
        from arknights_agent.garrison import answer_garrison

        result = answer_garrison(session, release, question, store=store)
    elif module == "recruitment":
        from arknights_agent.recruitment import calculate

        if re.search(r"概率|几率|机率|可能性|百分之|等概率", question):
            return _refusal(
                release,
                question,
                "本版本没有已核验的招募概率或掉标签概率数据；只能计算所选标签保留条件下的候选交集。",
                "unsupported",
            )
        try:
            chosen = tags if tags is not None else _recruitment_tags(session, release, question)
        except InvalidData as exc:
            return _refusal(release, question, f"{exc}。请核对屏幕上的完整标签后重新提问。")
        if tags is None and not chosen:
            return _refusal(
                release, question, "请提供屏幕上的公招标签及招募时长；无标签计算请使用 tags=[]。"
            )
        duration = duration_minutes if duration_minutes is not None else _duration(question)
        result = calculate(session, release, chosen, duration, store=store)
        result["question"] = question
        result["module"] = module
        result["answer"] = render_recruitment_answer(result)
        result["tool_trace"] = result.get("trace", [])
        if duration_minutes is None and not re.search(r"小时|分钟|[:：][0-9]{2}", question):
            result["warnings"].append("未指定招募时长，本次显式采用9小时；可提供时长重新计算。")
    elif module == "operator":
        local = qa.plan_question(session, release, question)
        selected = local
        trace = {"requested": planner, "used": "local"}
        if planner == "openai":
            candidate, trace = plan_with_model(session, release, question, settings)
            if candidate is not None:
                candidate = _safe_model_plan(local, candidate)
                if candidate is None:
                    trace.update(used="local", reason="model_conditions_conflict_or_missing")
                else:
                    selected = candidate
        result = qa.answer_question(session, release, question, store=store, plan=selected)
        result["module"] = module
        result["tool_trace"].insert(0, {"tool": "planner", **trace})
    else:
        raise InvalidData("unsupported question module")
    if result["status"] in ("answered", "computed") and not result.get("citations"):
        raise Conflict("successful factual output requires release-pinned evidence")
    from arknights_agent.runtime.security import redact

    if contains_secret(result) or redact(result) != result:
        raise InvalidData("sensitive answer output")
    return result


def record_run(session, release, question, result):
    from arknights_agent.runtime.security import contains_secret, redact

    if contains_secret(question) or contains_secret(result) or redact(result) != result:
        raise InvalidData("sensitive audit input or output")
    ident = new_id()
    result["run_id"] = ident
    session.add(
        QaRun(
            id=ident,
            release_id=release.id,
            question=question,
            trace={
                "result": result,
                "response_hash": services.content_digest(result),
                "runtime_implementation_hash": services.implementation_hash(),
            },
        )
    )
    session.flush()
    return result
