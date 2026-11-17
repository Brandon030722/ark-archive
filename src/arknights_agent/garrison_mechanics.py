"""Source-pinned descriptions, with runtime mechanisms kept behind a refusal gate."""

from __future__ import annotations

import re
from datetime import datetime

from arknights_agent import services
from arknights_agent.errors import Conflict
from arknights_agent.ingestion.garrison import ANNOUNCEMENTS
from arknights_agent.ingestion.sample import ACTIVITY_ID

_DOCUMENTED = re.compile(r"(?:文档|固定原文|原文|原始[^，,。！？?]{0,20}描述|展示|配置|文案)")
_RUNTIME = re.compile(
    r"(?:完整|运行|实际|实战|生效|到账|结算|累计收益|总收益|免费次数|次数为|封顶|保底|"
    r"随机|伪随机|期望|上限|购买|赠送|合成|回购|重购|递归|重复|补发|跳层|跨过|"
    r"跨多个|降层|退层|重领|重置|清零|停用|失活|重新激活|非投资人|其他干员|"
    r"所有干员|5次|五次|怎么算|如何计算|计算结果|多少概率|必定|必然|保证|覆盖|叠成)"
)

# These are subfields of a single reviewed description, not permissions to
# answer extra mechanics. For example, the funding description contains both
# an interval and an amount, while its settlement time remains unverified.
_DESCRIPTION_DETAILS = {
    "miracle_documented_probability_formula": ("公式", "展示式", "表达式", "概率"),
    "miracle_activation_description": ("激活条件", "所需人数", "人数", "整备区"),
    "miracle_refresh_target_description": ("刷新目标", "哪次刷新", "下次刷新", "指"),
    "miracle_funding_description": ("层数间隔", "层数阈值", "金额", "资金数额"),
    "investor_activation_description": ("激活条件", "所需人数", "人数", "整备区"),
    "investor_trigger_description": ("层数阈值", "阈值", "触发次数", "次数"),
}


def _documented_query(question, field):
    """Keep scope qualifiers on a known bundle without swallowing new requests.

    A second clause is removable only when it has an explicit restriction or
    listing prefix and its entire body consists of that bundle's subfields and
    documentation words. Unknown words, calculations and independent questions
    stay visible to the strict multiple-request/remainder guards below.
    """
    question = re.sub(r"(?:怎么写|怎样写|如何写|如何表述|怎么描述)", "是什么", question)
    kept = []
    for clause in re.split(r"[，,；;。.!！?？：:]|(?=包括|包含)", question):
        detail = re.fullmatch(
            r"\s*(?:包括|包含|这里(?:只|仅)(?:查询|查)|(?:只|仅)(?:查询|查)|请列出|列出|请说明|说明)(.+)",
            clause,
        )
        if detail:
            body = detail.group(1)
            words = [
                "文档",
                "固定原文",
                "原文",
                "原始",
                "配置",
                "文案",
                "描述",
                "表达",
                "内容",
                "中",
                "的",
                "和",
                "与",
                "及",
                *_DESCRIPTION_DETAILS[field],
            ]
            for word in sorted(set(words), key=lambda item: (len(item), item), reverse=True):
                body = re.sub(re.escape(word), "", body, flags=re.I)
            body = re.sub(r"[\s、（）()【】\[\]\-_/\"“”]+", "", body)
            if not body:
                continue
        if clause.strip():
            kept.append(clause)
    return "；".join(kept)


def _multiple_requests(question):
    """Do not concatenate separate questions into a successful keyword match.

    Interrogatives catch two questions even without punctuation. Clause cores
    catch coordinated nouns with just one trailing interrogative. Leading
    context and polite prefixes may be separate clauses; substantive words in
    later clauses must remain visible instead of becoming discarded fillers.
    """
    if (
        len(re.findall(r"(?:是什么|什么|多少|几|吗|是否|能否|会不会|能不能|怎么|如何)", question))
        > 1
    ):
        return True
    filler = [
        "固定原文",
        "原始描述",
        "文档",
        "原文",
        "原始",
        "展示",
        "配置",
        "文案",
        "描述",
        "请问",
        "帮我",
        "告诉我",
        "查询",
        "查一下",
        "查",
        "请",
        "解释",
        "是什么",
        "什么",
        "多少",
        "写了",
        "如何写",
        "内容",
        "中",
        "的",
        "是",
        "呢",
        "吗",
    ]
    context = [
        "奇迹",
        "投资人",
        "卫戍协议",
        "盟约",
        "下半期",
        "act2autochess",
        "2026年",
        "活动",
        "V2_1",
        "V2_2",
        "第一阶段",
        "第二阶段",
        "初始阶段",
        "第1阶段",
        "第2阶段",
        "一阶段",
        "二阶段",
        "1阶段",
        "2阶段",
    ]
    semantic_clauses = 0
    for clause in re.split(r"[，,；;。.!！?？、：:]", question):
        words = filler + context if semantic_clauses == 0 else filler
        core = clause
        for word in sorted(set(words), key=lambda item: (len(item), item), reverse=True):
            core = re.sub(re.escape(word), "", core, flags=re.I)
        core = re.sub(r"[\s（）()【】\[\]\-_/\"“”]+", "", core)
        if core:
            semantic_clauses += 1
        if semantic_clauses > 1:
            return True
    return False


def _runtime_topic(label, question):
    if label == "投资人":
        return "investor_runtime_trigger"
    funds_question = question.replace("下次刷新不消耗资金", "")
    if re.search(r"(?:资金|收益|到账|结算|发钱|补发|跳层|降层|退层|重领)", funds_question):
        return "miracle_fund_settlement"
    if re.search(r"(?:概率|公式|刷新|免费|随机|封顶|保底)", question):
        return "miracle_probability_formula"
    return None


def _request(question):
    """Own documented bundles and runtime questions; leave old scalar intents alone."""
    labels = [label for label in ("奇迹", "投资人") if label in question]
    if not labels:
        return False, None, None, None
    if len(labels) != 1 or any(label in question for label in ("商业包装方案", "奥术法阵")):
        return True, None, None, "问题包含多个对象；请分别指定一个原始描述包与阶段。"
    label = labels[0]
    topic = _runtime_topic(label, question)
    documented = bool(_DOCUMENTED.search(question))
    if _RUNTIME.search(question):
        return (
            True,
            None,
            topic,
            "完整运行机制仍缺少执行证据；不能用配置文案代替实际结算或触发结果。",
        )
    if not documented:
        if re.search(
            r"(?:公式|规则|机制|玩法|怎么|条件|触发次数|触发几次|触发多少次|激活|刷新目标)",
            question,
        ):
            return True, None, topic, "请区分原始文档描述与实际运行规则；后者尚未核验。"
        return False, None, None, None
    fields = []
    if "激活" in question:
        fields.append(
            "miracle_activation_description"
            if label == "奇迹"
            else "investor_activation_description"
        )
    if label == "奇迹":
        if re.search(r"(?:公式|展示式|表达式)", question):
            fields.append("miracle_documented_probability_formula")
        if re.search(r"(?:刷新目标|刷新影响|下次刷新.*(?:描述|文案)|刷新.*原始描述)", question):
            fields.append("miracle_refresh_target_description")
        if re.search(
            r"(?:叠层资金|资金配置文案|资金原始描述|资金描述|叠层.*(?:资金|发钱).*(?:文案|描述))",
            question,
        ):
            fields.append("miracle_funding_description")
    elif re.search(
        r"(?:触发次数|触发描述|触发条件.*(?:描述|文案)|获得时.*(?:描述|文案))", question
    ):
        fields.append("investor_trigger_description")
    fields = list(dict.fromkeys(fields))
    if not fields:
        # E.g. "基础概率配置值" remains a stage-six scalar request.
        return False, None, None, None
    if len(fields) != 1:
        return True, None, topic, "问题包含多个描述包；请分别查询，不能省略同时询问的条件。"
    field = fields[0]
    parsed_question = _documented_query(question, field)
    if _multiple_requests(parsed_question):
        return True, None, topic, "问题包含并列片段或多个问句；请一次只查询一个原始描述包。"
    extra_words = {
        "miracle_documented_probability_formula": [
            "概率公式",
            "免费刷新",
            "展示式",
            "表达式",
            "公式",
        ],
        "miracle_activation_description": [
            "激活条件",
            "激活所需人数",
            "所需人数",
            "激活人数",
            "激活",
        ],
        "miracle_refresh_target_description": [
            "刷新目标",
            "刷新影响",
            "下次刷新不消耗资金",
            "刷新",
        ],
        "miracle_funding_description": ["叠层资金", "叠层", "资金"],
        "investor_activation_description": [
            "激活条件",
            "激活所需人数",
            "所需人数",
            "激活人数",
            "激活",
        ],
        "investor_trigger_description": [
            "获得时",
            "触发次数",
            "触发条件",
            "触发描述",
            "类特质",
            "特质",
            "触发",
        ],
    }[field]
    words = [
        label,
        "卫戍协议",
        "盟约",
        "下半期",
        "act2autochess",
        "2026年",
        "活动",
        "V2_1",
        "V2_2",
        "第一阶段",
        "第二阶段",
        "初始阶段",
        "第1阶段",
        "第2阶段",
        "一阶段",
        "二阶段",
        "1阶段",
        "2阶段",
        "固定原文",
        "原始描述",
        "文档",
        "原文",
        "原始",
        "展示",
        "配置",
        "文案",
        "描述",
        "请问",
        "帮我",
        "告诉我",
        "查询",
        "查一下",
        "查",
        "请",
        "解释",
        "是什么",
        "什么",
        "多少",
        "写了",
        "如何写",
        "内容",
        "中",
        "的",
        "是",
        "呢",
        "吗",
        *extra_words,
    ]
    remainder = parsed_question
    for word in sorted(set(words), key=lambda item: (len(item), item), reverse=True):
        remainder = re.sub(re.escape(word), "", remainder, flags=re.I)
    remainder = re.sub(r"[\s，,；;。.!！?？：:、（）()【】\[\]\-_/\"“”]+", "", remainder)
    if remainder:
        return True, None, topic, "问题还包含未核验的字段、条件或计算；请只查询一个原始描述包。"
    return True, field, None, None


def answer_garrison_mechanics(answer, selected_phase, question):
    owned, field_key, topic, error = _request(question)
    if not owned:
        return None
    if selected_phase is None:
        return answer.refuse(
            "请明确盟约下半期 V2_1 或 V2_2；文档与运行审查不能借用另一阶段。",
            "needs_clarification",
        )
    if error:
        result = answer.refuse(error)
        _attach_review(answer, selected_phase, topic)
        return result
    from arknights_agent.garrison_mechanics_policy import DESCRIPTION_SPECS

    spec_key = f"{selected_phase}:{field_key}"
    spec = DESCRIPTION_SPECS.get(spec_key)
    coverage = answer.result["coverage"]
    if (
        spec is None
        or not coverage.get("documented_mechanics_qa_enabled")
        or spec_key not in coverage.get("documented_mechanics_fields", [])
    ):
        return answer.refuse("该发布版本未核验此阶段的原始描述；不能借用新发布或另一阶段的权限。")
    if answer.store is None:
        return answer.refuse("原始描述需要重放来源快照；本次查询没有快照存储，暂不回答。")
    return _description_answer(answer, selected_phase, spec_key, spec)


def _attach_review(answer, phase, topic):
    from arknights_agent.garrison_mechanics_policy import MECHANICS_REVIEWS

    if topic is None:
        return
    ident = f"{phase}:{topic}"
    coverage = answer.result["coverage"]
    if (
        not coverage.get("documented_mechanics_qa_enabled")
        or not coverage.get("mechanics_review_available")
        or ident not in coverage.get("mechanics_reviews", [])
    ):
        return
    policy = MECHANICS_REVIEWS.get(ident)
    if policy is None:
        return
    record = _unique(_rows(answer, "mechanics_review"), ident, "mechanism review")
    if not services.same_json_value(record["data"], policy):
        raise Conflict("garrison incomplete mechanism review differs from compiled policy")
    answer.result["verification_review"] = {
        "record_id": record["id"],
        "phase_id": phase,
        "topic": topic,
        "verification_status": policy["verification_status"],
        "missing_evidence": policy["missing_evidence"],
    }


def _description_answer(answer, phase, ident, spec):
    from arknights_agent.garrison import _policy_cite
    from arknights_agent.garrison_mechanics_policy import MAPPING_BASIS, VERIFICATION_STATUS

    record = _unique(_rows(answer, "documented_mechanics_field"), ident, "documented field")
    if not services.same_json_value(
        record["data"], {"field_spec": spec, "value_hash": spec["value_hash"]}
    ):
        raise Conflict("garrison documented field projection differs from compiled policy")
    if services.content_digest(spec["value"]) != spec["value_hash"]:
        raise Conflict("garrison documented value hash differs from compiled policy")
    citations = [
        _policy_cite(answer, record, "/bondId", spec["bond_id"], spec, spec["evidence_pointer"])
    ]
    for proof in spec["client_evidence"]:
        related = _unique(
            _rows(answer, proof["record_kind"]), proof["native_id"], "documented native"
        )
        if services.content_digest(related["data"]) != proof["value_hash"]:
            raise Conflict("garrison documented native object differs from audited raw hash")
        # Carry the reviewed native object as well as individual fields. This
        # exposes exact evidence for a derived bundle without inferring runtime
        # semantics from its parameters or widening the verified field set.
        citations.append(_policy_cite(answer, related, "", related["data"], spec, proof["pointer"]))
        for selector, value in proof["checks"]:
            if not services.same_json_value(services.json_pointer(related["data"], selector), value):
                raise Conflict("garrison documented native selector differs from policy")
            citations.append(_policy_cite(answer, related, selector, value, spec, proof["pointer"]))
    official = ANNOUNCEMENTS[spec["official_article_id"]]
    native_phase = _unique(
        _rows(answer, "historical_phase"), f"{spec['revision']}:phase:{phase}", "documented phase"
    )
    phase_fields = {
        "activityId": ACTIVITY_ID,
        "seasonName": "盟约",
        "startTime": int(datetime.fromisoformat(official["maintenance_start"]).timestamp()),
    }
    for key, value in phase_fields.items():
        if not services.same_json_value(native_phase["data"].get(key), value):
            raise Conflict("garrison documented phase projection differs from audited identity")
        citations.append(
            _policy_cite(
                answer,
                native_phase,
                f"/{key}",
                value,
                spec,
                f"/autoChessData/versionInfoDict/{phase}",
            )
        )
    announcement = _unique(
        _rows(answer, "official_phase_announcement"),
        spec["official_article_id"],
        "documented official phase",
    )
    if (
        announcement["data"].get("phase_id") != phase
        or announcement["data"].get("activity_id") != ACTIVITY_ID
    ):
        raise Conflict("garrison documented announcement belongs to another phase")
    for selector, value in (
        (announcement["data"]["schedule_selector"], official["lead"]),
        (announcement["data"]["publication_date_selector"], official["publication_date"]),
    ):
        citations.append(
            _policy_cite(answer, announcement, selector, value, spec, "", official=True)
        )
    wiki_ids = []
    for proof in spec["wiki_evidence"]:
        citations.append(_wiki_citation(answer, proof))
        wiki_ids.append(str(proof["revision"]))
    citations = list(dict.fromkeys(citations))
    answer.claim(
        f"{phase} {spec['title']}",
        spec["value"],
        citations,
        VERIFICATION_STATUS,
        derived={
            "method": "audited_configuration_and_original_description",
            "runtime_calculation": False,
        },
    )
    version = answer.result["version"]
    version.update(
        base_metadata_source_revision=version["source_revision"],
        source_revision=spec["revision"],
        phase_id=phase,
        activity_id=ACTIVITY_ID,
        verified_field_key=spec["field_key"],
        documented_field_key=spec["field_key"],
        mapping_basis=MAPPING_BASIS,
        namespace_phase_mapping="unverified",
        gameplay_application_verified=False,
        fixed_wiki_revisions=wiki_ids,
    )
    message = _display_value(spec)
    links = "；".join(
        f"[PRTS 固定修订 {revision}](https://prts.wiki/index.php?oldid={revision})"
        for revision in wiki_ids
    )
    answer.result["answer"] = (
        f"{phase}，{spec['title']}：{message}\n"
        f"范围：act2autochess 盟约下半期的配置及固定原始描述。{spec['boundary']}"
        "完整实战概率公式、实际资金结算与投资人运行触发条件仍未核验；"
        "官方公告仅佐证阶段安排，不证明完整运行机制。"
        f"\n版本：{answer.release.id}；历史配置修订：{spec['revision']}。"
        f"\n来源：[固定历史配置]({spec['source_url']})；{links}；"
        f"[官方阶段公告 {spec['official_article_id']}](https://ak.hypergryph.com/news/{spec['official_article_id']})。"
        "\n证据：" + "、".join(f"[{item}]" for item in citations)
    )
    answer.result["tool_trace"].append(
        {
            "tool": "garrison_documented_mechanics_gate",
            "field_spec_key": ident,
            "phase_id": phase,
            "source_revision": spec["revision"],
            "verification_status": VERIFICATION_STATUS,
            "mapping_basis": MAPPING_BASIS,
            "fixed_wiki_revisions": wiki_ids,
            "namespace_phase_mapping": "unverified",
            "gameplay_application_verified": False,
            "snapshot_verification": "sha256_and_original_document_pointer",
            "identity_chain": "phase_to_bond_to_effect_to_named_buff_parameters",
            "wiki_template_expansion": "not_used",
            "rules_engine_enabled": False,
        }
    )
    return answer.result


def _display_value(spec):
    value, key = spec["value"], spec["field_key"]
    if key == "miracle_documented_probability_formula":
        return f"{value['expression']}，L 表示层数。这是展示表达式，不计算任意层数的实战概率。"
    if key in ("miracle_activation_description", "investor_activation_description"):
        return (
            f"激活所需人数配置为 {value['required_count']}；"
            f"原文写明整备区的【{spec['label']}】干员也可用于激活盟约。"
        )
    if key == "miracle_refresh_target_description":
        return "刷新调度中心时，有概率使下次刷新不消耗资金。"
    if key == "miracle_funding_description":
        return f"每叠加 {value['layer_interval']:g} 层获得 {value['fund_amount']:g} 资金。"
    if key == "investor_trigger_description":
        return (
            f"干员的“获得时”类特质每次触发 {value['base_trigger_count']:g} 次；"
            f"达到 {value['triple_trigger_layer']:g} 层时每次触发 {value['triple_trigger_count']:g} 次。"
            "SERVER_GAIN 是配置中的事件标识，不据此判定具体获得行为。"
        )
    raise Conflict("garrison documented field has no bounded rendering")


def _wiki_citation(answer, proof):
    from arknights_agent.garrison_mechanics_policy import CONTENT_POINTER, WIKI_PINS

    pin = WIKI_PINS.get(proof["revision"])
    if (
        pin is None
        or proof["content_pointer"] != CONTENT_POINTER
        or proof["quote_hash"] != services.content_digest(proof["quote"])
        or proof["context_kind"] != "named_bond_description"
        or proof["template_expansion"] != "not_used"
        or proof["end_anchor"] != "|干员="
    ):
        raise Conflict("garrison documented wiki witness differs from audited policy")
    record = _unique(_rows(answer, pin["record_kind"]), str(pin["revision"]), "documented wiki")
    text = record["data"]
    if (
        not isinstance(text, str)
        or services.content_digest(text) != pin["content_hash"]
        or text.count(proof["quote"]) != 1
        or text.count(proof["context_quote"]) != 1
    ):
        raise Conflict("garrison documented wiki text or named bond is missing or ambiguous")
    context_offset, quote_offset = text.index(proof["context_quote"]), text.index(proof["quote"])
    end = text.find(proof["end_anchor"], context_offset)
    if (
        end < 0
        or quote_offset < context_offset
        or quote_offset + len(proof["quote"]) > end
        or "\n" in text[context_offset:end]
    ):
        raise Conflict("garrison documented wiki quote crosses a named-bond description boundary")
    citation_id = answer.cite(record, "", text)
    citation = next(row for row in answer.result["citations"] if row["id"] == citation_id)
    snapshot = services.get_snapshot(answer.session, answer.release, citation["snapshot_id"])
    expected = {
        "source_key": pin["source_key"],
        "source_type": "community_wiki_revision",
        "url": pin["url"],
        "revision": str(pin["revision"]),
        "content_hash": pin["snapshot_hash"],
        "byte_size": pin["byte_size"],
        "media_type": "application/json",
    }
    if (
        any(not services.same_json_value(snapshot.get(key), value) for key, value in expected.items())
        or citation["evidence_pointer"] != CONTENT_POINTER
        or citation["pointer"] != CONTENT_POINTER
    ):
        raise Conflict("garrison documented wiki source identity differs from the original pin")
    citation["fixed_wiki_witness"] = {
        "quote": proof["quote"],
        "quote_hash": proof["quote_hash"],
        "quote_offset": quote_offset,
        "context_quote": proof["context_quote"],
        "context_offset": context_offset,
        "context_end_offset": end,
        "context_kind": proof["context_kind"],
        "end_anchor": proof["end_anchor"],
        "page_url": pin["page_url"],
        "template_expansion": "not_used",
        "offset_semantics": "unicode_codepoints_in_original_content_field",
    }
    return citation_id


def _rows(answer, kind):
    from arknights_agent.garrison import _rows as query_rows

    return query_rows(answer.session, answer.release, kind)


def _unique(rows, ident, label):
    matches = [row for row in rows if row["native_id"] == ident]
    if len(matches) != 1:
        raise Conflict(f"garrison {label} record is missing or ambiguous")
    return matches[0]
